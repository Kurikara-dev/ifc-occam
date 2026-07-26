"""CUI Phase 3 の受け入れの核: テキスト経路とフルオープン経路の等価性試験
(docs/plans/2026-07-25-cui-phase3.md Task 4 Step 3)。

small.ifc に対し「あるクラスの delete」をテキスト経路
(`scan_full_graph` → `compute_text_delete_plan` → `rewrite_without`)と
フルオープン経路(`apply_operations`)の両方で適用し、以下3点が一致することを
検証する:
  (i) 出力の製品(IfcProduct)GlobalId集合が一致。
  (ii) 両出力とも `verify_no_dangling` が空(danglingな関係参照が無い)。
  (iii) `core.diagnose.aggregate_by_class` のクラス別 `total_triangles` が
        (削除対象クラス自身を含め)全クラスで一致。

対象クラス: `IfcDuctSilencer`(small.ifc に実在、exact-class件数=44)。選定理由
は本ファイル docstring 末尾の「対象クラス選定について」を参照。

テキスト経路は `class_table` の**完全一致**でクラスを選ぶ(`plan.py:
_rows_of_class` は `==`)。したがってフルオープン経路の対象GlobalId列挙も
サブタイプを含めてはならない——`by_type(CLASS)` は既定でサブタイプを含むため、
`[e for e in f.by_type(CLASS) if e.is_a() == CLASS]` のように厳密一致で
列挙する(監督者裁定10)。

`slow` マーカーは付けない(既定選択で走ることに受け入れの価値がある。
監督者裁定10)。実測所要時間は本ファイルのテスト関数内で計測しレポートに
転記する(docs/plans/2026-07-25-cui-phase3.md Task 4 参照)。

## 対象クラス選定について

small.ifc(21,529,266 bytes)の exact-class 製品件数を事前調査した結果
(IfcDamper/IfcUnitaryEquipment=8, IfcSwitchingDevice/IfcDistributionElement=11,
IfcOutlet=22, IfcSanitaryTerminal=23, IfcDuctSilencer=44, IfcAirTerminal=47,
IfcLightFixture=87 等)、以下の理由で `IfcDuctSilencer`(44件)を選定した:

- カスケード(IFCRELVOIDSELEMENT/FILLSELEMENT/AGGREGATES)が発生しない
  クラスであることを事前確認済み(`compute_text_delete_plan` の
  `stats["cascade"] == 0`)——閉包が seed そのものに限定されるため、
  フルオープン経路(`apply_operations`)の削除ループが軽く(実測: 44要素で
  約1.1秒)、本試験の実行時間が予測可能になる。
- `patch_rel_ids` 候補が実在する(事前確認: 353候補、うち少なくとも1つは
  実際に参照リストが縮む「本物のパッチ」になる)——単純な drop だけでなく
  `patch_rel_record` のパッチ経路も等価性試験の対象として自然に踏む。
- `total_triangles` が非0(852、事前確認)——三角形数比較が自明な0==0の
  比較に潰れない。
- 本試験の実行時間の大部分(概ね90秒中62秒程度)は `extract_model` を
  2回(テキスト出力・フルオープン出力それぞれ)呼ぶこと自体に起因し、
  これは「モデル全体のジオメトリ再構築」であって対象クラスの件数に
  ほぼ依存しない固定コストである。対象クラスの件数はフルオープン経路の
  削除ループ時間だけに影響するため、44件という小さな値を選ぶことで
  全体を90秒程度に収めている(件数を選び直しても大勢は変わらないが、
  実測の安全マージンを優先した)。
"""

from __future__ import annotations

import shutil
import time

import numpy as np

import ifcopenshell

from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.export import apply_operations, verify_no_dangling
from ifc_occam.core.extract import extract_model
from ifc_occam.core.ops import Operation
from ifc_occam.scan.fullgraph import FullGraph, scan_full_graph
from ifc_occam.textops.plan import compute_text_delete_plan
from ifc_occam.textops.rewrite import rewrite_without
from tests.fixtures_ifc import build_wall_with_window_ifc

_TARGET_CLASS = "IfcDuctSilencer"


def _assert_dangling_refs_not_introduced(
    src_graph: FullGraph, out_text_path, out_full_path, label: str = ""
) -> None:
    """I1(Important、フェーズ最終レビュー): クラス非依存の未解決参照チェック。

    `verify_no_dangling`(core/export.py)は4つのrelクラスのみを見て、しかも
    削除済み要素のGlobalIdとだけ突合するため、他のrelクラス・非relレコード・
    sweptレコード(幾何/配置/プロパティセットはGlobalIdを持たない)への
    danglingが原理的に見えない(small.ifc実測: drop 220件のうち176件(80%)が
    swept=検査器の射程外)。さらに`ifcopenshell.open`はdangling参照があっても
    失敗しないため、再オープン比較でも検出できない。

    `ref_targets < 0`はFullGraph自体の定義により解決不能参照を指し、クラス・
    ネスト深さに関わらず全レコードを対象にする。入力側の元々の未解決参照数
    (未解決参照が0件とは限らない——スキーマ違反寄りの実データにも対応する
    ため、0との比較ではなく入力自身の値と比較する)と比較することで、
    「書き換えが**新たに**未解決参照を作らない」ことを担保する(1出力あたり
    約2秒、ifcopenshell不要)。
    """
    unresolved_src = int(np.count_nonzero(src_graph.ref_targets < 0))
    unresolved_text = int(np.count_nonzero(scan_full_graph(out_text_path).ref_targets < 0))
    unresolved_full = int(np.count_nonzero(scan_full_graph(out_full_path).ref_targets < 0))
    prefix = f"[{label}] " if label else ""
    assert unresolved_text == unresolved_src, (
        f"{prefix}テキスト経路の出力が新たに未解決参照を作った(クラス非依存の"
        f"検査): 入力{unresolved_src}件 → 出力{unresolved_text}件"
    )
    assert unresolved_full == unresolved_src, (
        f"{prefix}フルオープン経路の出力が新たに未解決参照を作った(クラス非依存の"
        f"検査): 入力{unresolved_src}件 → 出力{unresolved_full}件"
    )


def test_text_path_and_fullopen_path_produce_equivalent_output_on_small_ifc(
    small_ifc_path, tmp_path
):
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    src_copy = tmp_path / "small_src.ifc"
    shutil.copy(small_ifc_path, src_copy)
    timings["copy_fixture"] = time.perf_counter() - t0

    # --- テキスト経路 ---
    t0 = time.perf_counter()
    graph = scan_full_graph(src_copy)
    timings["scan_full_graph"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    plan = compute_text_delete_plan(graph, {_TARGET_CLASS})
    timings["compute_text_delete_plan"] = time.perf_counter() - t0

    assert plan.stats["seeds"] > 0, "IfcDuctSilencer が small.ifc に実在しない(前提が崩れている)"

    out_text = tmp_path / "out_text.ifc"
    t0 = time.perf_counter()
    report_text = rewrite_without(
        src_copy, out_text, plan, graph, source_name="small.ifc"
    )
    timings["rewrite_without"] = time.perf_counter() - t0

    # --- フルオープン経路 ---
    t0 = time.perf_counter()
    model_for_gids = ifcopenshell.open(str(src_copy))
    # (監督者裁定10) テキスト経路は class_table の完全一致でクラスを選ぶため、
    # フルオープン経路の対象GlobalId列挙もサブタイプを含めない厳密一致にする
    # (by_type は既定でサブタイプを含むため、素朴に使うと偽の不一致/一致になる)。
    target_gids = [
        e.GlobalId for e in model_for_gids.by_type(_TARGET_CLASS) if e.is_a() == _TARGET_CLASS
    ]
    all_original_product_gids = {e.GlobalId for e in model_for_gids.by_type("IfcProduct")}
    timings["open_for_gid_lookup"] = time.perf_counter() - t0

    assert len(target_gids) == int(plan.stats["seeds"]), (
        "テキスト経路とフルオープン経路で対象クラスの厳密一致件数が異なる"
        "(サブタイプ混入等の疑い)"
    )

    out_full = tmp_path / "out_full.ifc"
    t0 = time.perf_counter()
    report_full = apply_operations(
        str(src_copy),
        [Operation(op="delete", targets=target_gids)],
        str(out_full),
        source_name="small.ifc",
    )
    timings["apply_operations"] = time.perf_counter() - t0

    # --- (i) 出力の製品GlobalId集合が一致 ---
    t0 = time.perf_counter()
    reopened_text = ifcopenshell.open(str(out_text))
    reopened_full = ifcopenshell.open(str(out_full))
    timings["reopen_both_outputs"] = time.perf_counter() - t0

    text_gids = {e.GlobalId for e in reopened_text.by_type("IfcProduct")}
    full_gids = {e.GlobalId for e in reopened_full.by_type("IfcProduct")}
    removed_gids = set(report_full.deleted)

    mismatch_only_in_text = text_gids - full_gids
    mismatch_only_in_full = full_gids - text_gids
    assert text_gids == full_gids, (
        f"製品GlobalId集合が不一致: text側のみ={mismatch_only_in_text!r}, "
        f"fullopen側のみ={mismatch_only_in_full!r}"
    )
    assert all_original_product_gids - removed_gids == text_gids

    # --- (ii) 両出力とも verify_no_dangling が空 ---
    t0 = time.perf_counter()
    dangling_text = verify_no_dangling(reopened_text, removed_gids)
    dangling_full = verify_no_dangling(reopened_full, removed_gids)
    timings["verify_no_dangling_both"] = time.perf_counter() - t0

    assert dangling_text == [], f"テキスト経路出力にdanglingな参照: {dangling_text}"
    assert dangling_full == [], f"フルオープン経路出力にdanglingな参照: {dangling_full}"

    # --- (ii-b) I1(Important、フェーズ最終レビュー): クラス非依存の未解決参照
    # チェック(上のverify_no_danglingは4relクラス+GlobalId突合のみで、swept
    # レコード等への射程外danglingを見逃すため)。graphはこのテスト冒頭で
    # scan_full_graph(src_copy)済みなので再スキャンしない。
    t0 = time.perf_counter()
    _assert_dangling_refs_not_introduced(graph, out_text, out_full, label=_TARGET_CLASS)
    timings["class_agnostic_dangling_check"] = time.perf_counter() - t0

    # --- (iii) diagnose の三角形数(クラス別 total_triangles)が一致 ---
    t0 = time.perf_counter()
    model_text, _warnings_text = extract_model(out_text)
    timings["extract_model_text"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    model_full, _warnings_full = extract_model(out_full)
    timings["extract_model_full"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    stats_text = {s.ifc_class: s.total_triangles for s in aggregate_by_class(model_text)}
    stats_full = {s.ifc_class: s.total_triangles for s in aggregate_by_class(model_full)}
    timings["aggregate_by_class_both"] = time.perf_counter() - t0

    assert _TARGET_CLASS not in stats_text, "削除対象クラスがテキスト経路出力に残存している"
    assert _TARGET_CLASS not in stats_full, "削除対象クラスがフルオープン経路出力に残存している"

    class_mismatches = {
        cls: (stats_text.get(cls), stats_full.get(cls))
        for cls in set(stats_text) | set(stats_full)
        if stats_text.get(cls) != stats_full.get(cls)
    }
    assert stats_text == stats_full, f"クラス別total_trianglesが不一致: {class_mismatches}"
    assert sum(stats_text.values()) == sum(stats_full.values())

    timings["total"] = sum(timings.values())
    print("\n--- CUI Phase3 equivalence test timings (seconds) ---")
    for stage, seconds in timings.items():
        print(f"  {stage}: {seconds:.2f}")
    print(f"  target_class={_TARGET_CLASS} seed_count={plan.stats['seeds']} "
          f"drop_ids={len(plan.drop_ids)} patch_rel_ids={len(plan.patch_rel_ids)}")
    print(f"  report_text={report_text}")
    print(f"  report_full.deleted={len(report_full.deleted)} skipped={len(report_full.skipped)}")


# ---------------------------------------------------------------------------
# 修正3(監督者追加要件): カスケードを伴う等価性試験を合成データで張る
#
# 上のテスト(small.ifc、IfcDuctSilencer)は cascade=0(閉包が対象クラス
# そのものに限定される)であり、Phase 3 で最も危険な部分——3関係クラス
# (IFCRELVOIDSELEMENT/IFCRELFILLSELEMENT/IFCRELAGGREGATES)のカスケード
# 意味論(と OwnerHistory スキップ判別)——が実データ相当で検証されていない。
# 監督者の実測: small.ifc には IFCRELVOIDSELEMENT/IFCRELFILLSELEMENT が0件、
# IFCRELAGGREGATES は空間構造の3件のみ(cascade>0はIfcProject/IfcSite/
# IfcBuildingだけで非現実的な削除操作)であり、実データではこの穴を埋め
# られない。そこで既存の合成フィクスチャ tests/fixtures_ifc.py:
# build_wall_with_window_ifc() (Wall1--IfcRelVoidsElement-->Opening1--
# IfcRelFillsElement-->Window1、Assembly1--IfcRelAggregates-->Member1,
# Member2)を使い、cascadeを実際に発火させた上でテキスト経路とフルオープン
# 経路の一致を検証する。
#
# このフィクスチャは幾何(IfcShapeRepresentation等)を一切持たないため、
# 三角形数比較((iii))は両経路とも 0 == 0 の自明一致に潰れる(各テストの
# assert文で実際に0であることを確認している)。load-bearingなのは
# (i) 生存GlobalId集合の一致 と (ii) verify_no_dangling が両出力とも空、の
# 2点のみ——docstringにその旨を明記する(自明一致を「証明した」と偽らない)。
# ---------------------------------------------------------------------------


def _run_synthetic_cascade_comparison(tmp_path, target_class: str):
    """build_wall_with_window_ifc() を tmp_path へ書き出し、target_class の
    削除をテキスト経路(scan_full_graph→compute_text_delete_plan→
    rewrite_without)とフルオープン経路(apply_operations)の両方で適用し、
    上の小規模等価性試験と同じ3判定基準——(i) 出力の製品GlobalId集合が一致
    (ii) 両出力とも verify_no_dangling が空 (iii) diagnose の
    クラス別total_trianglesが一致——を検証する。

    不一致が出た場合、テストを緩めたり対象を差し替えたりしない
    (監督者の明示指示)。assert メッセージに不一致の具体(どのGlobalIdが
    片方にしか無いか)を出すことで、そのまま失敗理由として報告できるように
    する。

    戻り値: (f, survivor_gids, stats_text, stats_full)。f は削除前の
    in-memory fixture(write() 後もオブジェクトとしては削除されず、個別の
    GlobalId確認にそのまま使える——tests/test_textops_rewrite.pyの
    _build_wall_window_fixtureと同じ流儀)。survivor_gidsはテキスト経路・
    フルオープン経路の両方が一致した生存IfcProduct GlobalId集合(一致
    したからこそ返せる値であり、一致確認そのものはこの関数内で完了して
    いる)。
    """
    f = build_wall_with_window_ifc()
    src_path = tmp_path / "src.ifc"
    f.write(str(src_path))
    all_original_product_gids = {e.GlobalId for e in f.by_type("IfcProduct")}

    # --- テキスト経路 ---
    graph = scan_full_graph(src_path)
    plan = compute_text_delete_plan(graph, {target_class})
    assert plan.stats["seeds"] > 0, f"{target_class} が合成フィクスチャに実在しない(前提が崩れている)"

    out_text = tmp_path / "out_text.ifc"
    rewrite_without(src_path, out_text, plan, graph, source_name="src.ifc")

    # --- フルオープン経路 ---
    model_for_gids = ifcopenshell.open(str(src_path))
    # (監督者裁定10の流儀を継承) テキスト経路はclass_tableの完全一致で
    # クラスを選ぶため、フルオープン経路の対象GlobalId列挙もサブタイプを
    # 含めない厳密一致にする(by_typeは既定でサブタイプを含むため)。
    target_gids = [
        e.GlobalId for e in model_for_gids.by_type(target_class) if e.is_a() == target_class
    ]
    assert len(target_gids) == int(plan.stats["seeds"]), (
        "テキスト経路とフルオープン経路で対象クラスの厳密一致件数が異なる(サブタイプ混入等の疑い)"
    )

    out_full = tmp_path / "out_full.ifc"
    report_full = apply_operations(
        str(src_path),
        [Operation(op="delete", targets=target_gids)],
        str(out_full),
        source_name="src.ifc",
    )

    # --- (i) 出力の製品GlobalId集合が一致 ---
    reopened_text = ifcopenshell.open(str(out_text))
    reopened_full = ifcopenshell.open(str(out_full))

    text_gids = {e.GlobalId for e in reopened_text.by_type("IfcProduct")}
    full_gids = {e.GlobalId for e in reopened_full.by_type("IfcProduct")}
    removed_gids = set(report_full.deleted)

    mismatch_only_in_text = text_gids - full_gids
    mismatch_only_in_full = full_gids - text_gids
    assert text_gids == full_gids, (
        f"[target_class={target_class}] 製品GlobalId集合が不一致: "
        f"text側のみ={mismatch_only_in_text!r}, fullopen側のみ={mismatch_only_in_full!r}"
    )
    assert all_original_product_gids - removed_gids == text_gids

    # --- (ii) 両出力とも verify_no_dangling が空 ---
    dangling_text = verify_no_dangling(reopened_text, removed_gids)
    dangling_full = verify_no_dangling(reopened_full, removed_gids)
    assert dangling_text == [], f"[target_class={target_class}] テキスト経路出力にdanglingな参照: {dangling_text}"
    assert dangling_full == [], f"[target_class={target_class}] フルオープン経路出力にdanglingな参照: {dangling_full}"

    # --- (ii-b) I1(Important、フェーズ最終レビュー): クラス非依存の未解決
    # 参照チェック(上のverify_no_danglingの射程外——swept レコード等——を
    # 全クラス・全ネスト深さで捉える。graphは本関数冒頭でscan_full_graph
    # (src_path)済みなので再スキャンしない)。
    _assert_dangling_refs_not_introduced(graph, out_text, out_full, label=target_class)

    # --- (iii) diagnose の三角形数(クラス別total_triangles)が一致 ---
    # (このフィクスチャは幾何を持たないため、両辞書とも全クラスtotal_triangles=0の
    # 自明一致になる想定——呼び出し側テストのdocstring/assertで明記する。)
    model_text, _warnings_text = extract_model(out_text)
    model_full, _warnings_full = extract_model(out_full)
    stats_text = {s.ifc_class: s.total_triangles for s in aggregate_by_class(model_text)}
    stats_full = {s.ifc_class: s.total_triangles for s in aggregate_by_class(model_full)}

    class_mismatches = {
        cls: (stats_text.get(cls), stats_full.get(cls))
        for cls in set(stats_text) | set(stats_full)
        if stats_text.get(cls) != stats_full.get(cls)
    }
    assert stats_text == stats_full, f"[target_class={target_class}] クラス別total_trianglesが不一致: {class_mismatches}"

    return f, text_gids, stats_text, stats_full


def test_text_path_and_fullopen_path_agree_on_synthetic_wall_opening_window_cascade(tmp_path):
    """壁(IfcWall)の削除が IFCRELVOIDSELEMENT→IFCRELFILLSELEMENT の連鎖で
    Opening1・Window1にも正しく伝播することを、テキスト経路とフルオープン
    経路の両方で検証する(サブケース1)。

    load-bearingなのは (i) 生存GlobalId集合の一致 と (ii) verify_no_dangling
    が両出力とも空、の2点である。(iii)の三角形数比較はこのフィクスチャが
    幾何を一切持たないため両経路とも0==0の自明一致であり(下のassertで実際に
    0であることを確認している)、この比較自体は何も証明していない。
    """
    f, survivor_gids, stats_text, stats_full = _run_synthetic_cascade_comparison(
        tmp_path, "IfcWall"
    )

    wall_gid = f.by_type("IfcWall")[0].GlobalId
    opening_gid = f.by_type("IfcOpeningElement")[0].GlobalId
    window_gid = f.by_type("IfcWindow")[0].GlobalId
    assembly_gid = f.by_type("IfcElementAssembly")[0].GlobalId
    member_gids = {b.GlobalId for b in f.by_type("IfcBeam")}

    # cascade: Wall(seed) --voids--> Opening --fills--> Window は両方消える
    assert wall_gid not in survivor_gids
    assert opening_gid not in survivor_gids
    assert window_gid not in survivor_gids
    # 無関係の集約(Assembly1とその部材)は残存する
    assert assembly_gid in survivor_gids
    assert member_gids <= survivor_gids

    assert sum(stats_text.values()) == 0, "前提が崩れている: このフィクスチャは幾何を持たないはず"
    assert sum(stats_full.values()) == 0, "前提が崩れている: このフィクスチャは幾何を持たないはず"


def test_text_path_and_fullopen_path_agree_on_synthetic_assembly_aggregates_cascade(tmp_path):
    """IfcElementAssembly(Assembly1)の削除が IFCRELAGGREGATES 経由で
    Member1/Member2にも正しく伝播することを、テキスト経路とフルオープン
    経路の両方で検証する(サブケース2)。

    load-bearingなのは (i) 生存GlobalId集合の一致 と (ii) verify_no_dangling
    が両出力とも空、の2点である。(iii)の三角形数比較はこのフィクスチャが
    幾何を一切持たないため両経路とも0==0の自明一致であり(下のassertで実際に
    0であることを確認している)、この比較自体は何も証明していない。
    """
    f, survivor_gids, stats_text, stats_full = _run_synthetic_cascade_comparison(
        tmp_path, "IfcElementAssembly"
    )

    wall_gid = f.by_type("IfcWall")[0].GlobalId
    opening_gid = f.by_type("IfcOpeningElement")[0].GlobalId
    window_gid = f.by_type("IfcWindow")[0].GlobalId
    assembly_gid = f.by_type("IfcElementAssembly")[0].GlobalId
    member_gids = {b.GlobalId for b in f.by_type("IfcBeam")}

    # cascade: Assembly(seed) --aggregates--> Member1, Member2 は両方消える
    assert assembly_gid not in survivor_gids
    assert member_gids.isdisjoint(survivor_gids)
    # 無関係の壁・開口・窓は残存する
    assert wall_gid in survivor_gids
    assert opening_gid in survivor_gids
    assert window_gid in survivor_gids

    assert sum(stats_text.values()) == 0, "前提が崩れている: このフィクスチャは幾何を持たないはず"
    assert sum(stats_full.values()) == 0, "前提が崩れている: このフィクスチャは幾何を持たないはず"
