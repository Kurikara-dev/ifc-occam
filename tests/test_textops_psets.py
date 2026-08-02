"""テキストモード削除の pset 残置回収(textops/plan.py)のテスト。

削除対象を参照する IfcRelDefinesByProperties は汎用 IFCREL* パッチで
RelatedObjects から死んだ id が抜かれるが、空になっても rel 自身は残り、
RelatingPropertyDefinition(pset)と IfcPropertySingleValue 群を掴み
続けていた(donuts 全削除の実測で 912 pset + 922 value ≒ 残存 214KB の
大半)。full-open 削除は root.remove_product が pset まで消すため、
テキストモードだけが太る既知の差分だった。修正は「related が全滅した
RelDefines 自身を dead にし、解放された pset サブグラフを sweep で回収」。

carry-forward Phase F(2026-08-02)で IFCRELDEFINESBYTYPE(型)の等価性番人
テストもこのファイルの守備範囲に加わった(pset とは異なり型は積極回収しない
契約——ファイル末尾のテスト群を参照)。
"""

import ifcopenshell
import ifcopenshell.api

from ifc_occam.core.export import apply_operations, verify_no_dangling
from ifc_occam.core.ops import Operation
from ifc_occam.scan.fullgraph import scan_full_graph
from ifc_occam.textops.plan import compute_text_delete_plan
from ifc_occam.textops.rewrite import rewrite_without
from tests.fixtures_ifc import (
    build_single_consumer_mapped_child_styled_brep_ifc,
    build_two_elements_with_shared_type_ifc,
)
from tests.test_cui_phase3_equivalence import _assert_dangling_refs_not_introduced
from tests.test_textops_annotations import (
    _count,
    _make_wall_with_own_brep,
    _text_delete,
)


def _add_pset(f, element, name):
    """element に pset(IfcPropertySingleValue 1件入り)を付け、
    (rel, pset) を返す。ifcopenshell.api の pset モジュールで作る
    (IfcRelDefinesByProperties + IfcPropertySet + value が生成される)。"""
    pset = ifcopenshell.api.run("pset.add_pset", f, product=element, name=name)
    ifcopenshell.api.run(
        "pset.edit_pset", f, pset=pset, properties={"合成キー": "合成値"}
    )
    rel = next(
        r
        for r in f.by_type("IfcRelDefinesByProperties")
        if r.RelatingPropertyDefinition == pset
    )
    return rel, pset


def test_text_delete_reaps_empty_rel_defines_and_exclusive_pset(tmp_path):
    """クラス全削除で、その要素の pset・値・RelDefines が全て出力から消える。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    _add_pset(f, proxy, "合成Pset")

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcBuildingElementProxy") == 0
    assert _count(out, "IfcRelDefinesByProperties") == 0
    assert _count(out, "IfcPropertySet") == 0
    assert _count(out, "IfcPropertySingleValue") == 0


def test_shared_pset_survives_while_any_user_lives(tmp_path):
    """2つの RelDefines(要素ごとに1本)が同一 pset を共有。片方のクラス
    だけ削除したら、死んだ側の rel は消えるが pset と値は残る。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    body_ctx = proxy.Representation.Representations[0].ContextOfItems
    wall, _rep, _shell = _make_wall_with_own_brep(f, body_ctx)
    _rel, pset = _add_pset(f, proxy, "共有Pset")
    f.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=[wall],
        RelatingPropertyDefinition=pset,
    )

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcRelDefinesByProperties") == 1
    assert _count(out, "IfcPropertySet") == 1
    assert _count(out, "IfcPropertySingleValue") == 1
    assert list(out.by_type("IfcRelDefinesByProperties")[0].RelatedObjects)[
        0
    ].is_a("IfcWall")

    out2, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy", "IfcWall"])
    assert _count(out2, "IfcRelDefinesByProperties") == 0
    assert _count(out2, "IfcPropertySet") == 0
    assert _count(out2, "IfcPropertySingleValue") == 0


def test_partially_dead_rel_defines_is_patched_not_reaped(tmp_path):
    """1本の RelDefines が2クラスの要素を束ねる。片方のクラスだけ削除したら、
    rel は生き残り、死んだ要素だけが RelatedObjects から抜ける(既存の
    汎用パッチの挙動が退行していないこと)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    body_ctx = proxy.Representation.Representations[0].ContextOfItems
    wall, _rep, _shell = _make_wall_with_own_brep(f, body_ctx)
    rel, _pset = _add_pset(f, proxy, "束ねPset")
    rel.RelatedObjects = [proxy, wall]

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    rels = out.by_type("IfcRelDefinesByProperties")
    assert len(rels) == 1
    related = list(rels[0].RelatedObjects)
    assert len(related) == 1
    assert related[0].is_a("IfcWall")
    assert _count(out, "IfcPropertySet") == 1
    assert _count(out, "IfcPropertySingleValue") == 1


def test_reaped_rel_defines_referrer_chain_is_patched(tmp_path):
    """(合成・スキーマ外データ)RelDefines 自身が別の IfcRel から参照されて
    いても dangling を作らないこと。従来は patch 段の規則3(空になった rel の
    drop)が plan から見えないところで rel を消すため、参照元がパッチ候補に
    ならず dangling になっていた(本フェーズの RED で発覚した既存ギャップ)。
    plan 段で reap すれば参照元は _generic_rel_candidates のパッチ候補に
    入り、空になった参照元自身は規則3が落とす——連鎖全体が矛盾なく消える。
    dangling が増えていないことは _text_delete 内の raw 再スキャンが検査する。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    rel, _pset = _add_pset(f, proxy, "被参照Pset")
    group = f.create_entity("IfcGroup", GlobalId=ifcopenshell.guid.new(), Name="G")
    f.create_entity(
        "IfcRelAssignsToGroup",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=[rel],
        RelatingGroup=group,
    )

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcRelDefinesByProperties") == 0
    assert _count(out, "IfcPropertySet") == 0
    assert _count(out, "IfcPropertySingleValue") == 0
    # 参照元も RelatedObjects が空になり規則3で落ち、グループ本体は残る
    # (孤児レコードにはなるが dangling は無い)
    assert _count(out, "IfcRelAssignsToGroup") == 0
    assert _count(out, "IfcGroup") == 1


# ---------------------------------------------------------------------------
# carry-forward Phase F: IFCRELDEFINESBYTYPE(型)のテキスト/フルオープン
# 等価性番人テスト。
#
# .superpowers/sdd/cff-probe-report.md の実測どおり、full-open
# (`apply_operations` の delete、内部で `type.unassign_type`)は related が
# 全滅した IFCRELDEFINESBYTYPE 自身は消すが、RelatingType(型本体)と
# RepresentationMaps は積極的には消さない(pset の `remove_pset` とは非対称)。
# テキスト経路は既存の汎用機構(`_generic_rel_candidates` の IFCREL 前方一致
# → `patch_rel_record` の規則3=空リストになった record の drop)だけで、
# 追加コード無しにこれと完全一致する生存集合を出力する。このフェーズは
# 実装変更を一切行わず、その一致を退行から守るテストだけを追加する。
# ---------------------------------------------------------------------------


def _text_and_fullopen_delete(tmp_path, f, delete_classes):
    """f を書き出し、テキスト経路(scan→plan→rewrite)とフルオープン経路
    (apply_operations)の両方で delete_classes の全レコードを削除し、
    (reopened_text, reopened_full, removed_gids) を返す。

    削除対象クラスの GlobalId 列挙は class_table の完全一致で厳密に選ぶ
    (test_cui_phase3_equivalence.py の監督者裁定10の流儀を継承。by_type は
    既定でサブタイプを含むため素朴に使うと偽の不一致になる)。両出力とも
    `verify_no_dangling`(4relクラス+GlobalId突合)と
    `_assert_dangling_refs_not_introduced`(クラス非依存の未解決参照チェック、
    test_cui_phase3_equivalence.py から流用)の両方を通すことを確認する。"""
    src = tmp_path / "src.ifc"
    f.write(str(src))

    graph = scan_full_graph(src)
    plan = compute_text_delete_plan(graph, delete_classes)
    out_text = tmp_path / "out_text.ifc"
    rewrite_without(src, out_text, plan, graph, source_name="src.ifc")

    model_for_gids = ifcopenshell.open(str(src))
    target_gids = [
        e.GlobalId
        for cls in delete_classes
        for e in model_for_gids.by_type(cls)
        if e.is_a() == cls
    ]
    out_full = tmp_path / "out_full.ifc"
    report_full = apply_operations(
        str(src),
        [Operation(op="delete", targets=target_gids)],
        str(out_full),
        source_name="src.ifc",
    )

    reopened_text = ifcopenshell.open(str(out_text))
    reopened_full = ifcopenshell.open(str(out_full))
    removed_gids = set(report_full.deleted)

    dangling_text = verify_no_dangling(reopened_text, removed_gids)
    dangling_full = verify_no_dangling(reopened_full, removed_gids)
    assert dangling_text == [], f"テキスト経路出力にdanglingな参照: {dangling_text}"
    assert dangling_full == [], f"フルオープン経路出力にdanglingな参照: {dangling_full}"
    _assert_dangling_refs_not_introduced(graph, out_text, out_full)

    return reopened_text, reopened_full, removed_gids


def test_type_rel_dies_but_type_object_survives_matching_full_open(tmp_path):
    """related 2件を全削除。テキスト経路とフルオープン経路の両方で、
    (i) IFCRELDEFINESBYTYPEが消え (ii) 型オブジェクトとRepresentationMapは
    生存し (iii) 生存GlobalId集合が一致し (iv) テキスト出力にdanglingが無い
    ことを固定する(full-openが型を残す設計であることのテキスト経路側の
    等価性証明)。"""
    f, type_obj, elements = build_two_elements_with_shared_type_ifc()
    type_gid = type_obj.GlobalId

    reopened_text, reopened_full, removed_gids = _text_and_fullopen_delete(
        tmp_path, f, {"IfcPipeSegment"}
    )

    # (i) 両出力でIFCRELDEFINESBYTYPEが消えている
    assert _count(reopened_text, "IfcRelDefinesByType") == 0
    assert _count(reopened_full, "IfcRelDefinesByType") == 0
    assert _count(reopened_text, "IfcPipeSegment") == 0
    assert _count(reopened_full, "IfcPipeSegment") == 0

    # (ii) 両出力で型オブジェクトとRepresentationMapが生存
    assert _count(reopened_text, "IfcPipeSegmentType") == 1
    assert _count(reopened_full, "IfcPipeSegmentType") == 1
    assert reopened_text.by_type("IfcPipeSegmentType")[0].GlobalId == type_gid
    assert reopened_full.by_type("IfcPipeSegmentType")[0].GlobalId == type_gid
    assert _count(reopened_text, "IfcRepresentationMap") == 1
    assert _count(reopened_full, "IfcRepresentationMap") == 1

    # (iii) 両出力の生存GlobalId集合が一致(型オブジェクトも含めた全IfcRoot)
    gids_text = {e.GlobalId for e in reopened_text.by_type("IfcRoot")}
    gids_full = {e.GlobalId for e in reopened_full.by_type("IfcRoot")}
    assert gids_text == gids_full, f"生存GlobalId集合が不一致: text-only={gids_text - gids_full!r}, full-only={gids_full - gids_text!r}"

    # (iv) テキスト出力にdanglingな参照が無いことは _text_and_fullopen_delete
    # 内で verify_no_dangling / _assert_dangling_refs_not_introduced の両方で
    # 既に検証済み(removed_gids はここでは未使用だが戻り値として残す)。
    assert removed_gids  # 前提: 全削除で少なくとも1件は削除されている


def _build_pipe_and_fitting_sharing_type():
    """IfcPipeSegment + IfcPipeFitting という異なるクラスの要素2件が同じ
    IfcPipeSegmentType(RepresentationMapsなし)を共有する最小合成IFC4。

    text経路の削除粒度はクラス単位(`compute_text_delete_plan`)であるため、
    「2件中1件だけ削除」を再現するには要素のクラスを分ける必要がある
    (cff-probe-report.md Fact3 ケースCと同じ流儀)。
    戻り値: (f, type_obj, segment, fitting)。"""
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f)
    type_obj = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcPipeSegmentType", name="PST1"
    )
    segment = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcPipeSegment", name="Seg0"
    )
    fitting = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcPipeFitting", name="Fit0"
    )
    ifcopenshell.api.run(
        "type.assign_type",
        f,
        related_objects=[segment, fitting],
        relating_type=type_obj,
    )
    return f, type_obj, segment, fitting


def test_partial_delete_keeps_type_rel_patched(tmp_path):
    """2件中1件だけ削除。テキスト経路・フルオープン経路の両方で
    IFCRELDEFINESBYTYPEは生存し、RelatedObjectsから死んだ要素だけが除かれる
    (テキスト側は既存の汎用パッチ、規則3の drop ではなく patch が発火する
    こと自体を固定する)。"""
    f, type_obj, segment, fitting = _build_pipe_and_fitting_sharing_type()
    type_gid = type_obj.GlobalId
    segment_gid = segment.GlobalId

    reopened_text, reopened_full, _removed_gids = _text_and_fullopen_delete(
        tmp_path, f, {"IfcPipeFitting"}
    )

    assert _count(reopened_text, "IfcPipeFitting") == 0
    assert _count(reopened_full, "IfcPipeFitting") == 0

    rels_text = reopened_text.by_type("IfcRelDefinesByType")
    rels_full = reopened_full.by_type("IfcRelDefinesByType")
    assert len(rels_text) == 1
    assert len(rels_full) == 1
    assert rels_text[0].RelatingType.GlobalId == type_gid
    assert rels_full[0].RelatingType.GlobalId == type_gid

    related_text = list(rels_text[0].RelatedObjects)
    related_full = list(rels_full[0].RelatedObjects)
    assert len(related_text) == 1
    assert len(related_full) == 1
    assert related_text[0].GlobalId == segment_gid
    assert related_full[0].GlobalId == segment_gid
    assert related_text[0].is_a("IfcPipeSegment")
    assert related_full[0].is_a("IfcPipeSegment")
