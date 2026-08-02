"""出力 (design.md §4.5, Phase3 Task4)。

原本を新たに open して操作リストを適用し、別ファイルへ書き出す(原本非破壊)。
(CUI向けの例外1点のみ: apply_operations に ifcopenshell.file を直接渡した場合は
再オープンせず、そのオブジェクト自体を直接変更する。design.md §5、詳細は
apply_operations の docstring 参照。)
削除は cascade.compute_delete_closure で連鎖を展開し、
`ifcopenshell.api.run("root.remove_product", ...)` で関係も含めて掃除する。
軽量化は simplify.replace_representation を使う。

keep vs 連鎖削除の優先順位 (Final Review Fix2, design.md §4.5参照):
  構造的な連鎖削除(開口の充填要素・集約の子部材)は個別の keep 指定に優先する。
  親要素(壁など)が削除されれば、その開口を充填する窓は keep されていても
  一緒に消える(窓だけ生かして壁を消す、という中間状態はIFC上維持できない)。
  ここ(export)は常にこの優先順位で動作する(closure が確定した時点で決着済み)。
  サーバ側の POST /api/ops/preview-delete は、上書きされる keep 対象を
  `keep_overridden` として事前に開示する(黙って上書きしない)。
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.geom
import ifcopenshell.util.element

from ifc_occam import __version__
from ifc_occam.core.cascade import compute_delete_closure
from ifc_occam.core.consolidate import consolidate_duplicates
from ifc_occam.core.duplicates import find_duplicates
from ifc_occam.core.extract import extract_model
from ifc_occam.core.ops import Operation, resolve_effective, validate_operations
from ifc_occam.core.paths import refers_to_same_file
from ifc_occam.core.provenance import build_provenance_lines
from ifc_occam.core.simplify import (
    _SHARED_FALLBACK_MARKER,
    bbox_mesh,
    convex_hull_mesh,
    decimate_mesh,
    replace_representation,
)
from ifc_occam.textops.gc import GcReport, gc_rewrite

_DANGLING_CHECK_REL_CLASSES = (
    "IfcRelVoidsElement",
    "IfcRelFillsElement",
    "IfcRelAggregates",
    "IfcRelContainedInSpatialStructure",
)

_MASS_DELETE_THRESHOLD = 1000
"""大量削除の高速経路(_mass_delete)が発動する閾値。

delete closure確定後の対象件数がこの値を**超えたら**(>、以下は非発動)
ifcopenshell.util.element.batch_remove_deep2/unbatch_remove_deep2 経路を使う。
Stage Aベンチ(docs/plans/2026-07-24-cui-phase1.md Task 7, docs/cui-measurements.md)で(a)per-remove
に対し17〜26%短縮・正当性(verify_no_dangling/GlobalId保存)は全項目パスした
採用方式。"""

_SIMPLIFY_BATCH_THRESHOLD = 100
"""inline掃除(geometry_cleanup="inline")で batch_remove_deep2 を使う simplify
対象数の閾値(これを**超えたら**バッチ)。2026-07-30 プローブ実測: donuts族で
バッチは 47→13.9秒/要素だが、unbatch がファイル全体のシリアライズ+再パース
(305MBで約28秒)を伴うため、対象が少ないときは固定費が逆転する。100 は
暫定値(unbatch固定費 ≈ 数十要素分の節約、に安全率を掛けた判断)。既定経路は
GC(geometry_cleanup="gc")なのでこの値がユーザー体験を左右することはない。"""


@dataclass
class SkippedItem:
    """スキップされた操作1件(gid + 理由)。"""

    global_id: str
    reason: str


@dataclass
class ExportReport:
    """apply_operations の結果報告。"""

    deleted: list[str] = field(default_factory=list)
    simplified: list[str] = field(default_factory=list)
    skipped: list[SkippedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_path: str = ""
    consolidated_groups: int = 0
    consolidated_elements: int = 0
    stage_seconds: dict[str, float] = field(default_factory=dict)


def _method_desc(method: str | None, ratio: float | None) -> str:
    """先勝ち警告に使う操作表記。ratio を持つ操作(decimate)は ratio まで
    示さないと、同一 method 同士の衝突時に「先行の decimate で処理済みのため
    decimate は適用されません」となり何が無視されたのか読めない
    (CUI共有波及フェーズ最終レビューの carry-forward)。"""
    if ratio is not None:
        return f"{method}(ratio={ratio})"
    return str(method)


def _safe_global_id(entity) -> str | None:
    """entity の GlobalId を取得する。既に破棄されたハンドル等は None を返す。"""
    try:
        return entity.GlobalId
    except Exception:  # noqa: BLE001 - dangling/無効ハンドルの防御
        return None


def verify_no_dangling(ifc_file, removed_gids: set[str]) -> list[str]:
    """IfcRelVoidsElement/FillsElement/Aggregates/ContainedInSpatialStructure に
    removed_gids への参照が残っていないか検証する。違反があれば説明文字列のリストを返す。

    読み取り専用。export.py の内部不変条件チェッカだが Task 8 でも再利用される。
    """
    violations: list[str] = []
    if not removed_gids:
        return violations

    for rel in ifc_file.by_type("IfcRelVoidsElement"):
        for attr in ("RelatingBuildingElement", "RelatedOpeningElement"):
            ref = getattr(rel, attr, None)
            gid = _safe_global_id(ref) if ref is not None else None
            if gid in removed_gids:
                violations.append(
                    f"IfcRelVoidsElement#{rel.id()}.{attr} が削除済みGlobalId={gid} を参照"
                )

    for rel in ifc_file.by_type("IfcRelFillsElement"):
        for attr in ("RelatingOpeningElement", "RelatedBuildingElement"):
            ref = getattr(rel, attr, None)
            gid = _safe_global_id(ref) if ref is not None else None
            if gid in removed_gids:
                violations.append(
                    f"IfcRelFillsElement#{rel.id()}.{attr} が削除済みGlobalId={gid} を参照"
                )

    for rel in ifc_file.by_type("IfcRelAggregates"):
        relating_gid = _safe_global_id(rel.RelatingObject)
        if relating_gid in removed_gids:
            violations.append(
                f"IfcRelAggregates#{rel.id()}.RelatingObject が削除済みGlobalId={relating_gid} を参照"
            )
        for child in rel.RelatedObjects or ():
            gid = _safe_global_id(child)
            if gid in removed_gids:
                violations.append(
                    f"IfcRelAggregates#{rel.id()}.RelatedObjects が削除済みGlobalId={gid} を参照"
                )

    for rel in ifc_file.by_type("IfcRelContainedInSpatialStructure"):
        relating_gid = _safe_global_id(rel.RelatingStructure)
        if relating_gid in removed_gids:
            violations.append(
                f"IfcRelContainedInSpatialStructure#{rel.id()}.RelatingStructure が"
                f" 削除済みGlobalId={relating_gid} を参照"
            )
        for elem in rel.RelatedElements or ():
            gid = _safe_global_id(elem)
            if gid in removed_gids:
                violations.append(
                    f"IfcRelContainedInSpatialStructure#{rel.id()}.RelatedElements が"
                    f" 削除済みGlobalId={gid} を参照"
                )

    return violations


def _sweep_dangling(ifc_file, removed_gids: set[str]) -> None:
    """verify_no_dangling が違反を検出した場合の手動掃除(安全網)。

    root.remove_product の関係掃除で取りこぼした場合に備える。
    RelatingObject/RelatingStructure が削除済みなら関係自体を削除、
    RelatedObjects/RelatedElements から削除済み要素を除去し、空になれば関係自体を削除する。
    """
    for rel in list(ifc_file.by_type("IfcRelVoidsElement")):
        relating_gid = _safe_global_id(rel.RelatingBuildingElement)
        related_gid = _safe_global_id(rel.RelatedOpeningElement)
        if relating_gid in removed_gids or related_gid in removed_gids:
            ifc_file.remove(rel)

    for rel in list(ifc_file.by_type("IfcRelFillsElement")):
        relating_gid = _safe_global_id(rel.RelatingOpeningElement)
        related_gid = _safe_global_id(rel.RelatedBuildingElement)
        if relating_gid in removed_gids or related_gid in removed_gids:
            ifc_file.remove(rel)

    for rel in list(ifc_file.by_type("IfcRelAggregates")):
        relating_gid = _safe_global_id(rel.RelatingObject)
        if relating_gid in removed_gids:
            ifc_file.remove(rel)
            continue
        remaining = [o for o in rel.RelatedObjects if _safe_global_id(o) not in removed_gids]
        if not remaining:
            ifc_file.remove(rel)
        elif len(remaining) != len(rel.RelatedObjects):
            rel.RelatedObjects = remaining

    for rel in list(ifc_file.by_type("IfcRelContainedInSpatialStructure")):
        relating_gid = _safe_global_id(rel.RelatingStructure)
        if relating_gid in removed_gids:
            ifc_file.remove(rel)
            continue
        remaining = [e for e in rel.RelatedElements if _safe_global_id(e) not in removed_gids]
        if not remaining:
            ifc_file.remove(rel)
        elif len(remaining) != len(rel.RelatedElements):
            rel.RelatedElements = remaining


def resolve_output_path(src_path, output_path) -> Path:
    """出力パスを解決する(Phase4 Task6-2)。

    絶対パスならそのまま使う。相対パスなら「読み込んだ元モデル(src_path)と
    同じディレクトリ」を基準に解決する(サーバのcwd基準ではない)。
    ユーザーがexportモーダルに `<元名>_light.ifc` のようなファイル名だけを
    入力する運用を想定している。
    """
    output = Path(output_path)
    if output.is_absolute():
        return output
    return Path(src_path).resolve().parent / output


def _delete_loop(
    ifc_file,
    gids,
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    """gids の各要素を root.remove_product で削除するループ本体。

    _apply_deletes(閾値以下の現行経路)と _mass_delete(閾値超の高速経路)の
    どちらからも同じこの関数が呼ばれる(閾値超でもループ本体のロジック・
    progress契約・RuntimeErrorスキップ挙動は完全に同一)。
    """
    total = len(gids)
    for done, gid in enumerate(gids, start=1):
        try:
            element = ifc_file.by_guid(gid)
        except RuntimeError:
            # 連鎖の副作用で既に削除済み(例: 開口は親要素削除時に自動で消える)。
            element = None
        if element is not None:
            ifcopenshell.api.run("root.remove_product", ifc_file, product=element)
        if progress is not None:
            progress("delete", done, total)


def _mass_delete(
    ifc_file,
    gids,
    progress: Callable[[str, int, int], None] | None = None,
):
    """大量削除(対象>_MASS_DELETE_THRESHOLD件)向けの高速経路。

    ifcopenshell.util.element.batch_remove_deep2/unbatch_remove_deep2 で
    _delete_loop(既存の削除ループ本体、ロジック不変)を包む。batch_remove_deep2
    は remove_deep2 が辿る副次的なサブグラフ(OwnerHistory/ObjectPlacement等)の
    実削除を遅延させ、unbatch_remove_deep2 が1回の文字列シリアライズ+再パースで
    まとめて確定する(Stage A実測で(a)比17〜26%短縮、docs/plans/2026-07-24-cui-phase1.md Task 7 参照)。

    重要な制約: unbatch_remove_deep2 は**新しい** ifcopenshell.file を返す
    (文字列再パース由来。トランザクション履歴は失われ、呼び出し前の要素ハンドルは
    以後使い回せない、という公式ドキュメント記載の制約)。**呼び出し側は戻り値を
    新しい ifc_file として使うこと**(このモジュール内では _apply_deletes が
    戻り値を受け直し、apply_operations の ifc_file 変数を差し替える)。

    安全策: unbatch_remove_deep2 は try/finally で必ず実行する。ループ中に
    例外が起きても batch 状態(ifc_file.to_delete が set のまま)を残さない
    (残ると以降の remove_deep2/batch_remove_deep2 呼び出しが壊れるため)。
    例外自体は finally 後にそのまま再伝播する(呼び出し側から見た失敗時の
    挙動は現行経路と同じ)。
    """
    ifcopenshell.util.element.batch_remove_deep2(ifc_file)
    try:
        _delete_loop(ifc_file, gids, progress)
    finally:
        ifc_file = ifcopenshell.util.element.unbatch_remove_deep2(ifc_file)
    return ifc_file


def _apply_deletes(
    ifc_file,
    delete_gids: list[str],
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[object, list[str], list[str], list[SkippedItem]]:
    """delete_gids の閉包を展開し、削除する。

    存在しない GlobalId は closure展開前にスキップする(by_guid の例外を防ぐ)。
    progress が指定されていれば、閉包の各要素を処理するたびに
    ("delete", 処理済み件数, 閉包の総件数) で通知する(design.md §5-2)。
    closure確定後の対象件数が _MASS_DELETE_THRESHOLD を超えたら _mass_delete
    (batch_remove_deep2/unbatch_remove_deep2)経路を使う。それ以外(閾値以下)は
    現行の直接ループのまま変えない。

    戻り値: (削除後に使うべき ifc_file, 削除済みgidリスト, 警告リスト,
    スキップ項目リスト)。_mass_delete 経路では ifc_file が呼び出し時の引数とは
    別オブジェクトになる(unbatch_remove_deep2 が新しい file を返すため)。
    呼び出し側は戻り値の ifc_file を使うこと。
    """
    skipped: list[SkippedItem] = []
    known_delete_gids: list[str] = []
    for gid in delete_gids:
        try:
            ifc_file.by_guid(gid)
        except RuntimeError:
            skipped.append(SkippedItem(global_id=gid, reason=f"要素が見つかりません(GlobalId={gid})"))
            continue
        known_delete_gids.append(gid)

    closure = compute_delete_closure(ifc_file, known_delete_gids)
    warnings: list[str] = []

    if len(closure.all_gids) > _MASS_DELETE_THRESHOLD:
        ifc_file = _mass_delete(ifc_file, closure.all_gids, progress)
    else:
        _delete_loop(ifc_file, closure.all_gids, progress)

    dangling = verify_no_dangling(ifc_file, closure.all_gids)
    if dangling:
        _sweep_dangling(ifc_file, closure.all_gids)
        warnings.extend(f"関係の不整合を検出し掃除しました: {v}" for v in dangling)

    return ifc_file, sorted(closure.all_gids), warnings, skipped


def _current_mesh(element) -> tuple[np.ndarray, np.ndarray]:
    """要素の現在のローカル座標メッシュ(溶接済み)を取得する。extract.py と同じ設定。"""
    settings = ifcopenshell.geom.settings()
    settings.set("weld-vertices", True)
    shape = ifcopenshell.geom.create_shape(settings, element)
    verts = np.array(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
    faces = np.array(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
    return verts, faces


def _shared_map_key(ifc_file, element) -> int | None:
    """要素の Body 形状が他要素と共有されている場合、その共有実体の識別子
    (entity id)を返す。共有していない/Bodyが無ければ None。

    scope="shared" の simplify で同じ共有実体への重複処理を防ぐための識別に使う。

    - IfcMappedItem 経由: MappingSource.MappedRepresentation(実際に書き換わる
      共有rep)の id。マップのidではなくrepのidを鍵にするのは、(a) 複数の
      RepresentationMapが同一MappedRepresentationを共有する構成、(b) 同じrepを
      直接参照する要素とmapped経由の要素が混在するハイブリッド構成、のどちらでも
      鍵が分裂して二重適用が残るため(CF-C最終レビューI-1、既存欠陥と実証済み。
      shared書き戻しの実体はMappedRepresentationのin-place差し替えであり、
      書き換わる実体こそが正しいdedupの単位)。in-place書き換えでrepのidは
      変わらないため、処理後も鍵は安定する。
    - 直接共有(Body の IfcShapeRepresentation 自体が複数の製品から参照
      されている場合): その rep の id。replace_representation の非 mapped
      経路は rep をその場で書き換えるため mapped と同様に全共有要素へ波及
      する——dedup しないと同じ rep に簡略化が要素数ぶん重ねがけされる
      (直接共有2要素への shared simplify で二重適用を実測、2026-08-01。
      フェーズ最終レビューI-3の carry-forward)。
    - entity id 空間はファイル内で共通なので、マップ id と rep id が
      衝突することはない。
    - 専有(参照する製品が1つ)なら従来どおり None(processed_shared_maps
      を太らせない)。ただしこれは直接共有分岐の話で、mapped 分岐は参照製品数を
      数えず常に鍵を返す(マップ経由は書き戻しが常に共有実体へ向かうため。
      Phase G 最終レビューM-2)。
    """
    rep = getattr(element, "Representation", None)
    if rep is None:
        return None
    for r in rep.Representations or []:
        if r.RepresentationIdentifier == "Body":
            items = list(r.Items)
            if len(items) == 1 and items[0].is_a("IfcMappedItem"):
                return items[0].MappingSource.MappedRepresentation.id()
            users = ifcopenshell.util.element.get_elements_by_representation(
                ifc_file, r
            )
            if len(users) > 1:
                return r.id()
            return None
    return None


def _apply_simplify(
    ifc_file, element, op: Operation, doomed_sink
) -> tuple[bool, str | None, list[str]]:
    """1件の simplify 操作を要素に適用する。

    幾何取得・簡略化計算・書き戻しの全過程を例外から保護する。1要素の失敗が
    export全体を止めないよう、例外はここで捕捉しスキップ理由として返す。

    戻り値: (成功したか, skip理由またはNone, 警告リスト)。
    """
    try:
        verts, faces = _current_mesh(element)

        method = op.params.get("method")
        if method == "bbox":
            new_verts, new_faces = bbox_mesh(verts)
        elif method == "convex_hull":
            new_verts, new_faces = convex_hull_mesh(verts)
        elif method == "decimate":
            ratio = op.params.get("ratio")
            new_verts, new_faces = decimate_mesh(verts, faces, ratio)
        else:
            return False, f"不正な simplify method です: {method!r}", []

        warnings = replace_representation(
            ifc_file, element, new_verts, new_faces, scope=op.scope, doomed_sink=doomed_sink
        )
        return True, None, warnings
    except Exception as exc:  # noqa: BLE001 - 1要素の簡略化失敗でexport全体を止めない
        return False, f"簡略化に失敗しました: {exc}", []


def _stamp_provenance(
    ifc_file: ifcopenshell.file,
    source_name: str,
    deleted_count: int,
    simplified_count: int,
) -> None:
    """出力IFCヘッダに由来情報を刻印する(CUI Phase2 Task1、
    docs/plans/2026-07-25-cui-phase2.md 参照)。

    軽量化出力が下流で「正本」と誤認されないよう、STEPヘッダへ非正本マークを刻む
    (OSS公開に向けたliability対策。要件外だがユーザー承認済みの設計判断であり、
    GUI/CUI両輸出経路に自動適用される)。

    FILE_DESCRIPTION.description は**既存エントリを保存したまま**3行追記する
    (既存エントリには ViewDefinition 等の MVD 宣言が入るため、消すとスキーマ/
    ビューア互換を壊す)。あわせて FILE_NAME.originating_system を上書きする。

    呼び出し側の契約: deleted_count/simplified_count は呼び出し時点で確定済みの
    ローカル値(apply_operations の `deleted`/`simplified` の件数)を渡すこと。
    書込前(ExportReport構築前)に呼ぶため、ExportReportそのものは参照できない。

    刻印文字列の生成自体は `core/provenance.py:build_provenance_lines` に
    切り出し済み(CUI Phase3 Task4、監督者裁定1: textops側がこの重量モジュール
    (ifcopenshell.geom 等を import する)を import せずに同じ文言を共用できる
    ようにするための抽出。挙動変更ゼロ——`tests/test_export.py` の刻印テスト群を
    1行も変更せずに green のままであることで証明済み)。本関数はそのヘルパーを
    呼び、ifcopenshell の header setter に生の文字列を渡すだけになった
    (STEPエスケープはifcopenshellに委ねる。source_name自体がASCII外を含む
    場合もifcopenshellの標準STEPエスケープ(\\X2\\...\\X0\\)がそのまま効く)。
    """
    stamp_lines = build_provenance_lines(source_name, deleted_count, simplified_count)
    header = ifc_file.header
    header.file_description.description = (
        tuple(header.file_description.description) + stamp_lines
    )
    header.file_name.originating_system = f"IFC Occam {__version__}"


def apply_operations(
    src: str | Path | ifcopenshell.file,
    operations: list[Operation],
    output_path,
    consolidate: bool = False,
    consolidate_min_benefit_ratio: float = 1.5,
    progress: Callable[[str, int, int], None] | None = None,
    source_name: str | None = None,
    geometry_cleanup: str = "gc",
) -> ExportReport:
    """src に operations を適用して output_path へ書き出す。

    src はパス(str/Path)または既に開いた ifcopenshell.file を受け付ける
    (extract_model と同じ流儀)。前者は新たに open するため原本は一切変更しない。
    後者(file オブジェクト)を渡した場合は再オープンせず、そのオブジェクトを直接
    変更する(CUIがフルオープンを1回に抑えるための経路)。**そのため file を渡すのは
    呼び出し側がそのオブジェクトを使い捨てにできる場合のみにすること**(以降、原本
    として読み直したり再利用したりはできない)。(削除対象件数が_MASS_DELETE_THRESHOLD
    を超える場合、内部の _mass_delete 経路が unbatch_remove_deep2 により渡された
    オブジェクトをさらに新しい ifcopenshell.file へ差し替える。渡したオブジェクトは
    元々使い捨て前提のためこの内部差し替えは外部契約を変えない。)

    progress を指定すると、削除ループ・simplifyループの進捗を
    ("delete" | "simplify", 処理済み件数, 対象総数) で通知する(design.md §5-2)。
    既定は None で、その場合は一切呼ばれない(既存呼び出しの挙動は変わらない。
    サーバ経路は常に None のまま)。

    source_name は出力ヘッダの由来刻印(_stamp_provenance、CUI Phase2 Task1)の
    "Source:" 行に使う。既定(None)は src がパス(str/Path)なら `Path(src).name`、
    file オブジェクトなら元ファイル名の手がかりが無いため `"(in-memory)"`。
    GUI(server)は常に path を渡すため無変更で元ファイル名が自動的に入る。CUIは
    フルオープン済みの file オブジェクトを渡す経路のため、呼び出し側(repl.py)が
    入力ファイル名を明示的に source_name として渡す。

    geometry_cleanup: "gc"(既定)は旧形状の掃除を書き出し時GCで一括実行する
    (fat一時ファイルを出力の隣に作り、グラフスキャンにfatサイズの約4.8倍の
    メモリを一時使用する)。"inline"は従来どおり要素ごとに掃除する(GCの
    一時ファイル/メモリを避けたい場合の退避経路。Task 3 でバッチ化される)。
    不正な値を渡すと ValueError になる。

    手順: resolve_effective → delete群のclosure展開→削除→simplify群を
    replace_representation で適用 → (consolidate=Trueなら)重複形状の共有化 → write。

    consolidate=True の場合、削除・軽量化を適用した**後**の状態で改めて
    extract_model + find_duplicates を実行して重複群を求め、consolidate_duplicates で
    共有 IfcRepresentationMap に置き換える。この順序により:
    - 削除された要素は再抽出の対象自体に含まれないため、削除済みメンバーを含む群は
      そもそも生成されない。
    - simplify で幾何が変わった要素は、他メンバーと平行移動不変で一致しなくなるため
      群から自然に外れる(幾何が変わった要素を対象外とする§5.4の要件をそのまま満たす)。
    """
    stage_seconds: dict[str, float] = {}

    is_file_src = isinstance(src, ifcopenshell.file)
    if source_name is None:
        source_name = "(in-memory)" if is_file_src else Path(src).name

    # 出力先=入力先の禁止(原本非破壊の契約。C1の水平展開)。src がパスのときだけ
    # 判定できる(file オブジェクト渡しには比較対象の元パスが無い——その経路
    # (CUI)は repl 側の UI ガードが塞ぐ)。フルオープン経路は全体をメモリに
    # 読んでから書くため truncate はしないが、原本を軽量化結果で上書きするのは
    # 同じく契約違反なので、フルオープン/テキストの両経路で同じ判定器
    # (core/paths.refers_to_same_file)を使って拒否する。
    #
    # 位置: **フルオープンより前**。ここで弾かないと、数十秒〜数分かけて開いて
    # 削除・簡略化まで終えた後に write 直前で落ちることになる(GUI では
    # export スレッドが失敗として報告するだけで、費用は丸ごと無駄になる)。
    # resolve_output_path は純粋関数なので、この事前判定と後段の実際の解決で
    # 2回呼んでも副作用はない。
    if not is_file_src and refers_to_same_file(src, resolve_output_path(src, output_path)):
        raise ValueError(
            f"出力先が入力ファイルと同一です: {output_path} "
            "(原本非破壊のため拒否しました。別のファイル名を指定してください)"
        )

    t0 = time.monotonic()
    ifc_file = src if is_file_src else ifcopenshell.open(str(src))
    stage_seconds["open"] = time.monotonic() - t0

    known_gids = {gid for gid in (_safe_global_id(e) for e in ifc_file.by_type("IfcRoot")) if gid}
    warnings: list[str] = list(validate_operations(operations, known_gids))

    effective = resolve_effective(operations)

    t0 = time.monotonic()
    delete_gids = [gid for gid, op in effective.items() if op.op == "delete"]
    ifc_file, deleted, delete_warnings, delete_skipped = _apply_deletes(
        ifc_file, delete_gids, progress
    )
    warnings.extend(delete_warnings)
    deleted_set = set(deleted)
    stage_seconds["deletes"] = time.monotonic() - t0

    if geometry_cleanup not in ("gc", "inline"):
        raise ValueError(f"不正な geometry_cleanup です: {geometry_cleanup!r}")
    doomed_root_ids: list[int] | None = [] if geometry_cleanup == "gc" else None

    t0 = time.monotonic()
    simplified: list[str] = []
    skipped: list[SkippedItem] = list(delete_skipped)
    # map_key(int) -> 実際にその共有マップへ書き込んだ (method, ratio)。
    # フェーズ最終レビューI-1/I-2: 値は「in-place書き換えが実際に成功した」
    # ときだけ記録する(フォールバック時やsimplify失敗時は記録しない。I-2)。
    # 既に記録済みの共有マップへ異なる(method, ratio)で到達した要素には
    # 「先勝ちで無視された」ことを警告として可視化する(I-1。挙動そのもの=
    # 先勝ちは変更しない)。
    processed_shared_maps: dict[int, tuple[str | None, float | None]] = {}

    simplify_total = sum(1 for op in effective.values() if op.op == "simplify")
    simplify_done = 0

    use_batch = doomed_root_ids is None and simplify_total > _SIMPLIFY_BATCH_THRESHOLD
    if use_batch:
        ifcopenshell.util.element.batch_remove_deep2(ifc_file)
    try:
        for gid, op in effective.items():
            if op.op != "simplify":
                continue
            simplify_done += 1
            if progress is not None:
                progress("simplify", simplify_done, simplify_total)
            if gid in deleted_set:
                skipped.append(
                    SkippedItem(
                        global_id=gid,
                        reason="削除連鎖により対象が既に削除されたためスキップ",
                    )
                )
                continue

            try:
                element = ifc_file.by_guid(gid)
            except RuntimeError:
                skipped.append(
                    SkippedItem(global_id=gid, reason=f"要素が見つかりません(GlobalId={gid})")
                )
                continue

            cur_method = op.params.get("method")
            cur_ratio = op.params.get("ratio")

            shared_key = None
            if op.scope == "shared":
                shared_key = _shared_map_key(ifc_file, element)
                if shared_key is not None and shared_key in processed_shared_maps:
                    # 既にこのexport実行内で同じ共有マップへ「実際の書き換え」が
                    # 成功済み(フォールバックではない)。in-place編集の複合(二重
                    # 適用)を避けるため再処理しないが、この要素の幾何は共有マップ
                    # 経由で変化済みなので simplified には含める(従来どおり)。
                    #
                    # 対象を横断した"勝者"のルールは、effective(gid→Operation) を
                    # 反復する順序(Pythonのdict挿入順=最初にそのgidへ有効操作が
                    # 確定した順)で、最初にそのマップへ到達した操作のパラメータ
                    # (method/ratio)が実際に書き込まれ、後続の操作は(このgidに
                    # 対しては)何もせず simplified に積まれるだけになる。つまり
                    # 「同じ共有マップに対する複数のsimplify操作」は後続が上書き
                    # されるのではなく、先行が勝ってそれ以降は無視される(list全体
                    # のlast-wins原則である resolve_effective とは別次元の勝者
                    # 決定)。挙動そのもの(先勝ち)は変更しないが、異なる
                    # (method, ratio) で到達した場合は無視されたことを警告で
                    # 可視化する(フェーズ最終レビューI-1)。
                    prev_method, prev_ratio = processed_shared_maps[shared_key]
                    if (cur_method, cur_ratio) != (prev_method, prev_ratio):
                        warnings.append(
                            f"共有形状は先行の {_method_desc(prev_method, prev_ratio)} "
                            "で処理済みのため、"
                            f"この要素(GlobalId={gid})への "
                            f"{_method_desc(cur_method, cur_ratio)} は適用されません"
                            "(共有波及の先勝ち)。"
                        )
                    simplified.append(gid)
                    continue

            success, skip_reason, op_warnings = _apply_simplify(ifc_file, element, op, doomed_root_ids)
            warnings.extend(op_warnings)
            if not success:
                skipped.append(SkippedItem(global_id=gid, reason=skip_reason))
                warnings.append(f"簡略化をスキップしました(GlobalId={gid}): {skip_reason}")
            else:
                simplified.append(gid)
                if shared_key is not None and not any(
                    _SHARED_FALLBACK_MARKER in w for w in op_warnings
                ):
                    # 共有マップの in-place 書き換えが実際に成功したときだけ
                    # 「処理済み」にマークする(フェーズ最終レビューI-2)。
                    # フォールバック(要素個別化)時は共有マップ自体は無変化のため
                    # マークせず、同じマップを持つ他の兄弟に適用の機会を残す。
                    processed_shared_maps[shared_key] = (cur_method, cur_ratio)
    finally:
        if use_batch:
            # unbatch は新しい file オブジェクトを返す(以降のステージは
            # 差し替え後のオブジェクトを使う。_mass_delete と同じ制約)。
            ifc_file = ifcopenshell.util.element.unbatch_remove_deep2(ifc_file)
    stage_seconds["simplify"] = time.monotonic() - t0

    t0 = time.monotonic()
    consolidated_groups = 0
    consolidated_elements = 0
    if consolidate:
        model, extract_warnings = extract_model(ifc_file)
        warnings.extend(extract_warnings)
        groups = find_duplicates(model.shapes)
        stage_seconds["reextract_duplicates"] = time.monotonic() - t0

        t0 = time.monotonic()
        consolidate_report = consolidate_duplicates(
            ifc_file, groups, model, min_benefit_ratio=consolidate_min_benefit_ratio
        )
        warnings.extend(consolidate_report.warnings)
        consolidated_groups = consolidate_report.groups_applied
        consolidated_elements = consolidate_report.elements_remapped
        stage_seconds["consolidate"] = time.monotonic() - t0
    else:
        stage_seconds["reextract_duplicates"] = 0.0
        stage_seconds["consolidate"] = 0.0

    if is_file_src:
        # 元パスが無い(=基準ディレクトリを持たない)ため、相対パスは cwd 基準で
        # 解決する(Path.resolve() の既定動作。resolve_output_path は str/Path 専用)。
        output = Path(output_path)
        resolved_output_path = output if output.is_absolute() else output.resolve()
    else:
        resolved_output_path = resolve_output_path(src, output_path)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    _stamp_provenance(ifc_file, source_name, len(deleted), len(simplified))
    if doomed_root_ids:
        # 掃除は書き出し後のGC(mark-and-sweep)で一括して行う。ゴミ込みの
        # fat ファイルをいったん出力の隣に書き、GC が最終出力へ書き換える。
        fat_path = resolved_output_path.with_name(resolved_output_path.name + ".gc-tmp")
        ifc_file.write(str(fat_path))
        stage_seconds["write"] = time.monotonic() - t0

        t0 = time.monotonic()
        # GCのグラフスキャンは fat サイズの約4.8倍のメモリを使う。フルオープン中
        # のモデル(ファイルサイズの約14倍)を先に手放してから走らせる。
        # ifc_file だけでは足りない: 同じオブジェクトを掴んでいる残りのローカル
        # 束縛(file オブジェクト渡し時の src パラメータ、simplify ループが最後に
        # 束縛した element)も揃って手放さないと解放されない(修正ウェーブの
        # weakref 実測で発見。パラメータ束縛は関数末尾まで生きる)。
        ifc_file = None
        src = None
        element = None
        gc_failed = False
        preserve_fat = False
        try:
            gc_report = gc_rewrite(
                fat_path, resolved_output_path, doomed_root_ids, source_name
            )
        except Exception as exc:  # noqa: BLE001 - GC失敗で数十分の適用結果を失わない
            # fat は正しい(ゴミ込みなだけ)。GCが何かで落ちても出力として救済する。
            gc_report = GcReport(records_dropped=0, doomed_survivors=[], aborted=True)
            gc_failed = True
            try:
                if fat_path.exists():
                    shutil.move(str(fat_path), str(resolved_output_path))
            except Exception as move_exc:  # noqa: BLE001 - 救済自体の失敗でfatまで失わない
                # move が失敗すると出力も未確定のまま(ゴミ込みの)fatだけが
                # 手元に残る状態になる。ここでfatを消すと数十分の適用結果が
                # 完全に失われるため、finallyでの無条件unlinkを止めてfatを残す。
                preserve_fat = True
                warnings.append(
                    f"GCの救済にも失敗しました。ゴミ込みの出力が {fat_path} に"
                    f"残っています: {move_exc}"
                )
            else:
                warnings.append(
                    f"書き出し時GCが失敗したため中止しました(出力は正しいものの、"
                    f"旧形状が残ったままです): {exc}"
                )
        finally:
            if not preserve_fat:
                fat_path.unlink(missing_ok=True)
        if gc_report.aborted and not gc_failed:
            warnings.append(
                "書き出し時GCを中止しました(生き残りが除去対象を参照)。"
                "出力は正しいものの、旧形状が残ったままです。"
            )
        for record_id, class_name, referrer_types in gc_report.doomed_survivors:
            warnings.append(
                f"旧形状 {class_name} #{record_id} が他から参照されているため"
                f"削除できませんでした(参照元: {referrer_types})"
            )
        stage_seconds["gc"] = time.monotonic() - t0
    else:
        ifc_file.write(str(resolved_output_path))
        stage_seconds["write"] = time.monotonic() - t0
        stage_seconds["gc"] = 0.0

    return ExportReport(
        deleted=deleted,
        simplified=simplified,
        skipped=skipped,
        warnings=warnings,
        output_path=str(resolved_output_path),
        consolidated_groups=consolidated_groups,
        consolidated_elements=consolidated_elements,
        stage_seconds=stage_seconds,
    )
