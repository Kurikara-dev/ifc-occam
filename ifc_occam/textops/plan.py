"""テキストレベル削除計画(cui-design.md §8、docs/plans/2026-07-25-cui-phase3.md
Task 2)。Task 1 の `FullGraph`(全レコード参照グラフ、CSR/numpy)だけを入力に、
「クラス指定による削除」から「出力から除外する record id 一覧」と「参照リスト
修正が必要な IFCREL* record id 一覧」を計算する。ifcopenshell オブジェクトも
`core/cascade.py` も import しない——本モジュールはテキストグラフ(FullGraph)
上で完結する(GUI の `compute_delete_closure` とは意味論を揃えるが実装は独立)。

## カスケード: 3関係クラスのみ属性位置で解釈する

`IFCRELVOIDSELEMENT` / `IFCRELFILLSELEMENT` / `IFCRELAGGREGATES` の3クラス
だけは、relating 側が dead になったら rel 自身と related 側も dead にする
(不動点まで反復。voids→fills のように異なるルールへ連鎖することもある)。

IFC2X3/IFC4 とも、この3クラスは `IfcRoot`(GlobalId, OwnerHistory, Name,
Description)を継承した上で Relating* / Related* の2属性を追加するだけの
単純な形をしており、0-indexで attr4=Relating*、attr5=Related* になる
(`IFCRELAGGREGATES` の RelatedObjects だけは LIST[1:?])。

### 属性位置の識別方法(raw bytes を使わない設計)

`FullGraph` はレコードの生バイト列を保持しない(Task 1 の設計—— レコード規模の
文字列を持たないことで CSR を軽くしている)ため、`ref_targets` の行は
「そのレコードの body に現れた参照を出現順に並べたもの」でしかなく、
「どのインデックスが何attr目か」という情報は失われている。GlobalId/Name/
Description は(IfcLabel/IfcText/文字列であり)決して参照にならないが、
OwnerHistory は参照になり得る唯一の任意attrで、しかも**実データで頻繁に
参照として現れる**(small.ifc の実レコード:
`#380500 = IFCRELAGGREGATES('...', #209, 'BuildingContainer', '',
#380499, (#380324,...,#380491));` — attr1=OwnerHistory=`#209` が解決可能な
参照として先頭に来る)。これを考慮せず単純に「行の先頭 = Relating」と
決め打つと、OwnerHistory を RelatingObject と誤認し、実データでカスケードが
機能しなくなる(このモジュール自身のテストで固定済み: 3クラスの合成
FullGraph には常にOwnerHistory参照を含めている)。

そこで、raw bytes を再取得せずに(= `compute_text_delete_plan` の入力を
`FullGraph` だけに保つ)位置を特定する方法として、**行の先頭の参照が
`IFCOWNERHISTORY` クラスのレコードに解決するなら1個だけスキップする**、
という構造的な判別を使う。GlobalId/Name/Description が参照にならないこと
(IFCスキーマ上保証される)、OwnerHistory が Relating/Related より前にしか
現れ得ないこと(スキーマの属性順が固定)、`IfcOwnerHistory` が
`IfcObjectDefinition`/`IfcElement`/`IfcOpeningElement` のいずれとも
非互換な別系統のエンティティであること(取り違えの可能性がない)から、
この判別は attr4/5 の厳密な位置と等価であり、`_blank_strings`/
`_split_top_level`(parser.py)による raw bytes 上の属性分割と同じ結果を
`FullGraph` の構造情報だけで得られる。OwnerHistory 自身が未解決参照(-1)の
場合は判別できないためスキップせず先頭をそのまま Relating とみなす
(このケースは既存テストの対象外——スキーマ違反の域)。

## rel の生死/パッチ判定(汎用規則、クラス列挙しない)

3クラス以外の `IFCREL*`(class_table 名が `IFCREL` で始まる。前方一致で
判定——クラス種類数のみに比例する `class_table` へのループなので record
規模ではない)については位置解釈を一切行わず、「kept(まだ dead でない)
レコードが dead な record を1つでも参照しているか」だけを見る。参照リスト
中の除去か単独属性の drop かの最終判定は record のテキスト解析が要るため
Task 3 の `patch_rel_record`(戻り値 None=drop)に委ねる——本モジュールは
「候補の列挙」(`patch_rel_ids`)までを行う。

## 専有サブグラフ回収(カウントダウン)

`alive_ref_count = in_degree.copy()` から出発し、dead(seeds ∪ カスケード
死)の各レコードが持つ参照を、CSR 上の**出現1回ごとに**減算する(重複除去
しない——多重辺は加算/減算で正確に相殺される設計。Task 1 裁定)。0に到達し
かつ元の `in_degree > 0` のレコードを新たに dead に加え、その参照もさらに
減算する worklist を、dead が増えなくなるまで繰り返す。`in_degree == 0` の
レコード(IFCPROJECT 等のトップレベル)は alive_ref_count が最初から 0 だが
「元の in_degree > 0」を満たさないため、seeds/カスケード以外では死なない。

カスケード(3クラス)とsweep(カウントダウン)は**2段階**(カスケードを
不動点まで完了させてから sweep を1回走らせる)で、逆方向には戻さない——
sweep で新たに dead 化したレコードが3クラスの relating 側だった場合、
そこから追加のカスケードは発生しない。これは意図的な単純化であり、実務上は
「パッチ候補 rel を生存扱いする近似」(下記)の影響で relating 側が cascade
を経ずに sweep 単独で dead 化するケースは稀と分析しているが、厳密な相互
不動点が必要な場合は監督者確認を推奨する(自己申告事項。テストでは
(a)-(g) いずれもこの単純化で正しく解ける範囲)。

## 既知の限界(2つ)

1. **dead 同士の循環は回収されない**: 参照カウント方式(このカウントダウン)
   は、外部から到達不能になった相互参照グループを構造的に検出できない
   (お互いがお互いを「生かして」しまう——古典的な reference counting の
   循環検出不能問題)。この場合そのレコード群は dead に到達せず出力に残存
   する(スキーマは有効なまま、削除漏れによるファイルサイズの肥大のみ。
   mark-sweep 的な到達可能性解析であれば回収できるが、本タスクはカウント
   ダウン方式を採用するため対象外とする)。
2. **パッチ候補 rel は「生存」としてカウントダウンする**: `patch_rel_ids`
   の候補は Task 3 の判定を待たずに本モジュール内では dead 集合に加えない
   (= 生存として扱う)近似を取る。これらの rel が最終的に Task 3 で drop
   されるとしても、その専有参照は OwnerHistory 等の共有資源が大半で、
   残っても出力の有効性(スキーマ違反やダングリング参照)を壊さない
   ——ただし専有回収の機会を一部逃す(例: 削除された階に紐づく
   `IFCRELCONTAINEDINSPATIALSTRUCTURE` の RelatingStructure が dead でも、
   この rel はカスケード対象クラスではないため patch 候補のまま生存扱いに
   なり、その RelatedElements の要素が sweep で回収されないことがある)。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from ifc_occam.scan.fullgraph import FullGraph

__all__ = ["TextDeletePlan", "compute_text_delete_plan"]

_VOIDS_CLASS = "IFCRELVOIDSELEMENT"
_FILLS_CLASS = "IFCRELFILLSELEMENT"
_AGGREGATES_CLASS = "IFCRELAGGREGATES"
_OWNER_HISTORY_CLASS = "IFCOWNERHISTORY"
_REL_PREFIX = "IFCREL"
# I3(Important、フェーズ最終レビュー): `_generic_rel_candidates` は
# クラス名が `_REL_PREFIX` で始まるレコードだけを patch 候補にする——kept
# (dead でない)かつ非 `IfcRel*` のレコードが dead を参照していても、この
# 汎用規則は検知しない(パッチもされず verbatim 出力され、無音で dangling
# になり得る)。この汎用規則が健全であるのは、明文化されていない次の2つの
# 不変条件のおかげであり、いずれかが破れると上記の無音dangling化が起こる:
#   (a) sweep(専有サブグラフ回収)は in_degree が0になるまでレコードを
#       殺さないため、kept な referrer(このレコードを参照し続けている、
#       dead でない別のレコード)を孤立させ得ない——kept な非IfcRel*レコード
#       がdeadを参照する状況そのものは、seeds/cascadeがそのレコードの
#       「専有される側」を直接殺した場合にしか生じず、sweepが誘発すること
#       はない。
#   (b) seeds/cascade が殺すのは製品(要素)のみで、正常な(schema-legal な)
#       IFCでは製品を参照するのは `IfcRelationship` サブタイプだけであり、
#       かつそれらは全てクラス名が `IFCREL` で始まる(IFC2X3/IFC4 いずれの
#       スキーマも命名規則としてこれを保証する)。
# schema-legal な反例(正常なIFCでこの2条件を破るデータ)は構築できないため、
# 本モジュールの挙動は変えない(assert も入れない——record 規模のコストに
# なるため)。将来この前提が破れる状況が生じた場合の網は、本モジュール自身
# ではなく `tests/test_cui_phase3_equivalence.py` の I1(クラス非依存の
# 未解決参照チェック、`scan_full_graph(out).ref_targets < 0` の件数が入力と
# 一致することを検証する)が担う——この前提が破れて無音dangling化が起きれば、
# そちらのテストが機械的に落ちる。


@dataclass(slots=True)
class TextDeletePlan:
    """削除計画(cui-phase3 Task 2 契約。フィールド名/型は
    docs/plans/2026-07-25-cui-phase3.md Task 2 から verbatim)。"""

    drop_ids: np.ndarray  # (k,) int64 昇順。出力から除外する record id
    patch_rel_ids: np.ndarray  # (p,) int64 昇順。参照リスト修正が必要な IFCREL* record id
    stats: dict[str, int]  # {"seeds","cascade","swept","rels_dropped","rels_patched"}


def _class_code(graph: FullGraph, name: str) -> int | None:
    """class_table(クラス種類数のみに比例する有界なインターン表)上で name
    に対応するコードを返す。存在しなければ None(このグラフにそのクラスの
    レコードが1件もない)。"""
    try:
        return graph.class_table.index(name)
    except ValueError:
        return None


def _rows_of_class(graph: FullGraph, name: str) -> np.ndarray:
    """class_table 上で name と完全一致するクラスの行indexをすべて返す
    (ベクトル演算。record規模のPythonループはしない)。"""
    code = _class_code(graph, name)
    if code is None:
        return np.empty(0, dtype=np.int64)
    return np.nonzero(graph.class_codes == code)[0]


def _bounded_class_mask(graph: FullGraph, predicate) -> np.ndarray:
    """class_table(クラス種類数のみに比例=有界)上で predicate を満たす
    クラスの bool ベクトルを作り、それを class_codes で record 規模へ
    ベクトル化して展開する。record 規模の Python ループはしない
    (predicate 自身は class_table の要素数だけ呼ばれる)。"""
    n = graph.record_count
    if not graph.class_table:
        return np.zeros(n, dtype=bool)
    per_class = np.array([predicate(name) for name in graph.class_table], dtype=bool)
    return per_class[graph.class_codes]


def _relating_and_related(
    graph: FullGraph, owner_history_code: int | None, row: int
) -> tuple[int | None, np.ndarray]:
    """3特殊クラスの1行から (relating行index, related行indexの配列) を返す。

    行先頭の参照が IFCOWNERHISTORY に解決するならスキップする(モジュール
    docstring「属性位置の識別方法」参照)。スキップ後に何も残らない場合は
    (None, 空配列)——スキーマ違反相当の壊れた入力に対する防御的な扱いで、
    例外を投げずカスケードを単に発火させない。"""
    start, end = int(graph.ref_indptr[row]), int(graph.ref_indptr[row + 1])
    pos = start
    if pos < end:
        head = int(graph.ref_targets[pos])
        if (
            head != -1
            and owner_history_code is not None
            and int(graph.class_codes[head]) == owner_history_code
        ):
            pos += 1
    if pos >= end:
        return None, np.empty(0, dtype=np.int64)
    relating = int(graph.ref_targets[pos])
    related = graph.ref_targets[pos + 1 : end]
    return relating, related


def _collect_pair_edges(
    graph: FullGraph, owner_history_code: int | None, rows: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """VOIDS/FILLS 用: relating・related が両方スカラーのクラスから
    (rel行, relating行, related行) の並列配列を作る。未解決(-1)や、
    スカラーが2個ぴったり残らない壊れた行は防御的に読み飛ばす。

    rows は IFCRELVOIDSELEMENT/IFCRELFILLSELEMENT の行数だけに比例する
    (record 全体規模ではない、有界に近い集合)ため、Python ループで
    十分——ここで作るのは小さい numpy 配列3本のみ。
    """
    rel_list: list[int] = []
    relating_list: list[int] = []
    related_list: list[int] = []
    for row in rows.tolist():
        relating, related = _relating_and_related(graph, owner_history_code, int(row))
        if relating is None or relating == -1:
            continue
        if related.size != 1:
            continue
        target = int(related[0])
        if target == -1:
            continue
        rel_list.append(int(row))
        relating_list.append(relating)
        related_list.append(target)
    return (
        np.array(rel_list, dtype=np.int64),
        np.array(relating_list, dtype=np.int64),
        np.array(related_list, dtype=np.int64),
    )


def _collect_list_edges(
    graph: FullGraph, owner_history_code: int | None, rows: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """AGGREGATES 用: relating はスカラー、related は1個以上のリストの
    クラスから (rel行, relating行, related_indptr, related行の平坦配列) の
    CSR を作る。rows は IFCRELAGGREGATES の行数だけに比例するため
    Python ループで十分(rel1件あたりのRelatedObjects展開のみ)。
    """
    rel_list: list[int] = []
    relating_list: list[int] = []
    related_flat: list[int] = []
    related_lengths: list[int] = []
    for row in rows.tolist():
        relating, related = _relating_and_related(graph, owner_history_code, int(row))
        if relating is None or relating == -1:
            continue
        valid_related = related[related != -1]
        if valid_related.size == 0:
            continue
        rel_list.append(int(row))
        relating_list.append(relating)
        related_lengths.append(int(valid_related.size))
        related_flat.extend(int(x) for x in valid_related.tolist())

    related_indptr = np.zeros(len(rel_list) + 1, dtype=np.int64)
    if related_lengths:
        related_indptr[1:] = np.cumsum(related_lengths)
    return (
        np.array(rel_list, dtype=np.int64),
        np.array(relating_list, dtype=np.int64),
        related_indptr,
        np.array(related_flat, dtype=np.int64),
    )


def _run_cascade(
    dead: np.ndarray,
    voids_edges: tuple[np.ndarray, np.ndarray, np.ndarray],
    fills_edges: tuple[np.ndarray, np.ndarray, np.ndarray],
    agg_edges: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
) -> None:
    """3クラスのカスケード規則を不動点まで反復する。dead を破壊的に更新
    する。voids→fills のように異なるルールへ連鎖する場合、その連鎖が
    確定するのは反応が伝播した次の反復になる(1回の走査では届かない)。
    """
    voids_rel, voids_relating, voids_related = voids_edges
    fills_rel, fills_relating, fills_related = fills_edges
    agg_rel, agg_relating, agg_related_indptr, agg_related = agg_edges
    agg_lengths = np.diff(agg_related_indptr) if agg_rel.size else np.empty(0, dtype=np.int64)

    changed = True
    while changed:
        changed = False

        for rel_rows, relating_rows, related_rows in (
            (voids_rel, voids_relating, voids_related),
            (fills_rel, fills_relating, fills_related),
        ):
            if rel_rows.size == 0:
                continue
            trigger = dead[relating_rows]
            if not trigger.any():
                continue
            newly_rel = rel_rows[trigger & ~dead[rel_rows]]
            if newly_rel.size:
                dead[newly_rel] = True
                changed = True
            newly_related = related_rows[trigger & ~dead[related_rows]]
            if newly_related.size:
                dead[newly_related] = True
                changed = True

        if agg_rel.size:
            trigger = dead[agg_relating]
            if trigger.any():
                newly_rel = agg_rel[trigger & ~dead[agg_rel]]
                if newly_rel.size:
                    dead[newly_rel] = True
                    changed = True
                flat_trigger = np.repeat(trigger, agg_lengths)
                candidates = agg_related[flat_trigger]
                newly_related = candidates[~dead[candidates]]
                if newly_related.size:
                    dead[newly_related] = True
                    changed = True


def _sweep(graph: FullGraph, dead: np.ndarray) -> None:
    """専有サブグラフ回収(カウントダウン)。dead を破壊的に更新する
    (モジュール docstring 参照)。"""
    n = graph.record_count
    if n == 0:
        return
    row_of_ref = np.repeat(np.arange(n, dtype=np.int64), np.diff(graph.ref_indptr))
    alive_ref_count = graph.in_degree.copy()

    pending = np.nonzero(dead)[0]
    while pending.size:
        pending_mask = np.zeros(n, dtype=bool)
        pending_mask[pending] = True
        emitted = pending_mask[row_of_ref] & (graph.ref_targets >= 0)
        targets = graph.ref_targets[emitted]
        if targets.size:
            alive_ref_count = alive_ref_count - np.bincount(targets, minlength=n)
        newly_dead_mask = (alive_ref_count == 0) & (graph.in_degree > 0) & ~dead
        pending = np.nonzero(newly_dead_mask)[0]
        dead[pending] = True


def _generic_rel_candidates(graph: FullGraph, dead: np.ndarray) -> np.ndarray:
    """3特殊クラス以外の汎用 IFCREL* 規則: kept(dead でない)かつ class_table
    名が IFCREL で始まるレコードのうち、dead な record を1件以上参照する
    ものの id を昇順で返す(drop/patch の最終判定は Task 3 に委ねる——ここ
    では候補列挙のみ)。sweep 完了後の最終的な dead 集合に対して評価する
    (sweep 由来の dead 参照も候補判定に含めるため)。

    この「クラス名が `_REL_PREFIX` で始まるレコードのみ」という汎用規則が
    健全である前提(2つの明文化されていない不変条件)は `_REL_PREFIX` 定義
    直上のコメント参照。
    """
    n = graph.record_count
    if n == 0:
        return np.empty(0, dtype=np.int64)

    rel_mask = _bounded_class_mask(graph, lambda name: name.startswith(_REL_PREFIX))
    kept_rel = rel_mask & ~dead
    if not kept_rel.any():
        return np.empty(0, dtype=np.int64)

    row_of_ref = np.repeat(np.arange(n, dtype=np.int64), np.diff(graph.ref_indptr))
    valid = graph.ref_targets >= 0
    dead_target = np.zeros_like(valid)
    dead_target[valid] = dead[graph.ref_targets[valid]]

    rows_with_dead_ref = np.zeros(n, dtype=bool)
    if dead_target.any():
        rows_with_dead_ref[np.unique(row_of_ref[dead_target])] = True

    patch_mask = kept_rel & rows_with_dead_ref
    return np.sort(graph.ids[patch_mask])


def compute_text_delete_plan(
    graph: FullGraph, delete_classes: Iterable[str]
) -> TextDeletePlan:
    """delete_classes(大文字小文字を問わず upper() 突合)に属す全レコードを
    seed とし、3クラスのカスケード → 専有サブグラフ回収(sweep) → 汎用
    IFCREL* パッチ候補列挙、の順で `TextDeletePlan` を計算する
    (アルゴリズムの詳細はモジュール docstring 参照)。"""
    n = graph.record_count
    dead = np.zeros(n, dtype=bool)

    # --- seeds ---
    delete_classes_upper = {c.upper() for c in delete_classes}
    seed_mask = _bounded_class_mask(graph, lambda name: name in delete_classes_upper)
    dead[seed_mask] = True
    seeds_count = int(dead.sum())

    # --- カスケード(3特殊クラス、不動点まで) ---
    owner_history_code = _class_code(graph, _OWNER_HISTORY_CLASS)
    voids_edges = _collect_pair_edges(graph, owner_history_code, _rows_of_class(graph, _VOIDS_CLASS))
    fills_edges = _collect_pair_edges(graph, owner_history_code, _rows_of_class(graph, _FILLS_CLASS))
    agg_edges = _collect_list_edges(graph, owner_history_code, _rows_of_class(graph, _AGGREGATES_CLASS))
    _run_cascade(dead, voids_edges, fills_edges, agg_edges)
    cascade_count = int(dead.sum()) - seeds_count

    # --- 専有サブグラフ回収(カウントダウン) ---
    _sweep(graph, dead)
    swept_count = int(dead.sum()) - seeds_count - cascade_count

    # --- 汎用 IFCREL* パッチ候補(sweep 完了後の最終 dead 集合に対して) ---
    patch_rel_ids = _generic_rel_candidates(graph, dead)

    drop_ids = np.sort(graph.ids[dead])
    rel_mask = _bounded_class_mask(graph, lambda name: name.startswith(_REL_PREFIX))
    rels_dropped = int(np.count_nonzero(rel_mask & dead))

    stats = {
        "seeds": seeds_count,
        "cascade": cascade_count,
        "swept": swept_count,
        "rels_dropped": rels_dropped,
        "rels_patched": int(patch_rel_ids.size),
    }

    return TextDeletePlan(drop_ids=drop_ids, patch_rel_ids=patch_rel_ids, stats=stats)
