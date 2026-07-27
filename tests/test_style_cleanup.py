"""形状差し替え時のスタイル付け替えと、旧形状の掃除に関するテスト。

軽量化出力が入力より太る不具合(旧形状が IfcStyledItem に掴まれて remove_deep2 で
消えない)の再発防止。
"""

import numpy as np

import ifcopenshell

from ifc_occam.core.consolidate import consolidate_duplicates
from ifc_occam.core.duplicates import find_duplicates
from ifc_occam.core.extract import extract_model
from ifc_occam.core.simplify import (
    _resolve_surface_rgb,
    bbox_mesh,
    replace_representation,
    style_signature,
)
from tests.fixtures_ifc import (
    build_brep_with_styles_at_every_subtree_depth_ifc,
    build_element_with_a_discarded_style_referenced_by_a_styled_representation_ifc,
    build_single_consumer_mapped_child_styled_brep_ifc,
    build_single_element_with_child_styled_brep_ifc,
    build_single_element_with_styled_item_ifc,
    build_single_element_with_wrapped_child_styled_brep_ifc,
    build_two_consumers_mapped_child_styled_brep_ifc,
    build_two_translated_child_styled_cubes_ifc,
)
from tests.ifc_graph import unreachable_geometry


def _simplify_first_element(ifc_file):
    """最初の要素の Body を、その頂点のAABB直方体に差し替える。"""
    element = ifc_file.by_type("IfcBuildingElementProxy")[0]
    verts = np.array(
        [p.Coordinates for p in ifc_file.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    return replace_representation(ifc_file, element, new_verts, new_faces, scope="element")


def test_child_styled_brep_leaves_no_unreachable_geometry():
    """スタイルが内側の IfcClosedShell に付いていても、差し替え後に旧形状が残らない。"""
    f = build_single_element_with_child_styled_brep_ifc()
    _simplify_first_element(f)
    assert unreachable_geometry(f) == {}


def test_child_styled_brep_keeps_its_colour_on_the_new_item():
    """差し替え後の新アイテムに、元と同じRGBの IfcStyledItem が付いている。"""
    f = build_single_element_with_child_styled_brep_ifc(rgb=(0.0, 0.25, 1.0))
    _simplify_first_element(f)

    body_rep = f.by_type("IfcShapeRepresentation")[0]
    new_item = body_rep.Items[0]
    styled_items = list(getattr(new_item, "StyledByItem", []) or [])
    assert len(styled_items) == 1

    rendering = styled_items[0].Styles[0].Styles[0]
    colour = rendering.SurfaceColour
    assert (colour.Red, colour.Green, colour.Blue) == (0.0, 0.25, 1.0)


def test_top_level_styled_item_still_works():
    """既存の挙動(トップレベルにスタイルが付いている場合)を壊していない。"""
    f = build_single_element_with_styled_item_ifc(rgb=(1.0, 0.0, 0.0))
    _simplify_first_element(f)
    assert unreachable_geometry(f) == {}


def test_style_signature_sees_styles_on_child_items():
    """style_signature は子アイテムに付いたスタイルも拾う。

    拾えないと「スタイル無し同士」と誤判定され、色の違う要素が
    consolidate で1つの共有形状に統合されてしまう。
    """
    f = build_single_element_with_child_styled_brep_ifc(rgb=(0.0, 0.25, 1.0))
    body_rep = f.by_type("IfcShapeRepresentation")[0]
    signature = style_signature(list(body_rep.Items))
    assert signature is not None
    assert (0.0, 0.25, 1.0) in {rgb for _, rgb in signature}


def test_resolve_surface_rgb_unwraps_presentation_style_assignment():
    """_resolve_surface_rgb は IfcPresentationStyleAssignment を1段はがしてRGBを返す。

    small.ifc(Rebro2026出力)の IfcStyledItem 4,053件は実測で全てこのwrapper経由
    だった(直接 IfcSurfaceStyle を持つ形ではない)。展開しないと一度もRGBを返せず、
    styles_match の「同一RGB」経由の一致判定が実データで機能しない(color-task-4追補)。
    """
    f = build_single_element_with_wrapped_child_styled_brep_ifc(rgb=(0.0, 0.25, 1.0))
    styled_item = f.by_type("IfcStyledItem")[0]
    wrapped_style = styled_item.Styles[0]
    assert wrapped_style.is_a("IfcPresentationStyleAssignment")

    rgb = _resolve_surface_rgb(wrapped_style)
    assert rgb is not None
    assert (round(rgb[0], 3), round(rgb[1], 3), round(rgb[2], 3)) == (0.0, 0.25, 1.0)


def test_style_signature_sees_rgb_through_presentation_style_assignment():
    """style_signature も IfcPresentationStyleAssignment 経由のRGBを拾う。

    wrapper未対応だと signature の RGB が常に None になり、style エンティティが
    別々でも同じ色の要素同士が styles_match の RGB経路で一致しなくなる。
    """
    f = build_single_element_with_wrapped_child_styled_brep_ifc(rgb=(0.0, 0.25, 1.0))
    body_rep = f.by_type("IfcShapeRepresentation")[0]
    signature = style_signature(list(body_rep.Items))
    assert signature is not None
    assert (0.0, 0.25, 1.0) in {rgb for _, rgb in signature}


# ---------------------------------------------------------------------------
# Task 3 修正ブリーフ: レビュー指摘 F1-F4 の再発防止(追加テスト5件)
# ---------------------------------------------------------------------------


def test_consolidate_keeps_colour_and_leaves_no_unreachable_geometry():
    """子アイテムにスタイルが付いた同形状2要素をconsolidateすると、共有ソースに
    色が乗り、旧形状(shell/face/polyloop)が到達不能にならない(F1)。

    consolidate.py は Task3 で simplify.py 側が部分木探索へ切り替わった後も
    トップレベルのみを見る _styled_items_for_item を使い続けていたため、
    共有化後のアイテムが無色になり、両メンバーの旧形状が到達不能で残った。
    """
    f = build_two_translated_child_styled_cubes_ifc(rgb=(0.0, 0.25, 1.0))

    model, warnings = extract_model(f)
    assert warnings == []
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1

    report = consolidate_duplicates(f, groups, model, min_benefit_ratio=0)
    assert report.groups_applied == 1
    assert report.elements_remapped == 2

    assert unreachable_geometry(f) == {}

    rep_maps = f.by_type("IfcRepresentationMap")
    assert len(rep_maps) == 1
    shared_item = rep_maps[0].MappedRepresentation.Items[0]
    styled = list(getattr(shared_item, "StyledByItem", []) or [])
    assert len(styled) == 1
    rendering = styled[0].Styles[0].Styles[0]
    colour = rendering.SurfaceColour
    assert (colour.Red, colour.Green, colour.Blue) == (0.0, 0.25, 1.0)


def test_unshare_releases_an_exclusively_used_shared_map():
    """1要素だけが使う共有マップ(内部shellに子スタイル)をscope="element"で
    差し替えると、旧形状(四面体一式)が到達不能にならず、新アイテムに色が
    引き継がれる(F2)。

    _unshare_and_replace は body_rep.Items(=[IfcMappedItem])を
    _transfer_styled_items に渡すが、深さ上限(旧実装2)では
    MappingSource(深さ1)→MappedRepresentation(深さ2)までしか辿れず、共有マップ
    内部の形状アイテム(深さ3)には絶対到達しない。
    """
    f = build_single_consumer_mapped_child_styled_brep_ifc(rgb=(0.0, 0.25, 1.0))
    element = f.by_type("IfcBuildingElementProxy")[0]

    verts = np.array(
        [p.Coordinates for p in f.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    replace_representation(f, element, new_verts, new_faces, scope="element")

    assert unreachable_geometry(f) == {}

    body_rep = element.Representation.Representations[0]
    new_item = body_rep.Items[0]
    styled = list(getattr(new_item, "StyledByItem", []) or [])
    assert len(styled) == 1
    rendering = styled[0].Styles[0].Styles[0]
    colour = rendering.SurfaceColour
    assert (colour.Red, colour.Green, colour.Blue) == (0.0, 0.25, 1.0)


def test_unshare_does_not_steal_styles_from_a_map_shared_with_others():
    """2要素が使う共有マップを、片方だけscope="element"で差し替える。

    もう片方の要素の色が残っていることを確認する(F2の副作用防止。これが
    落ちるようなら共有マップから色を奪っている)。他の要素も使っている共有マップ
    の内部には絶対に入ってはならない、という制約の番人テスト。
    """
    f = build_two_consumers_mapped_child_styled_brep_ifc(rgb=(0.0, 0.25, 1.0))
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    verts = np.array(
        [p.Coordinates for p in f.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    replace_representation(f, elem1, new_verts, new_faces, scope="element")

    # elem2はまだ共有マップ(IfcMappedItem)経由。共有マップ内部のshellに、
    # 色付きIfcStyledItemが奪われずに1件だけ残っていることを確認する。
    body_rep2 = elem2.Representation.Representations[0]
    mapped_item2 = body_rep2.Items[0]
    assert mapped_item2.is_a("IfcMappedItem")
    shared_brep = mapped_item2.MappingSource.MappedRepresentation.Items[0]
    shell = shared_brep.Outer
    styled = list(getattr(shell, "StyledByItem", []) or [])
    assert len(styled) == 1
    rendering = styled[0].Styles[0].Styles[0]
    colour = rendering.SurfaceColour
    assert (colour.Red, colour.Green, colour.Blue) == (0.0, 0.25, 1.0)


def test_styles_deep_in_the_subtree_are_found():
    """brep/shell/face/faceBound/polyloopの各階層にスタイルを付けても、
    差し替え後に旧形状が到達不能にならない(F3: 深さ上限の穴)。
    """
    f = build_brep_with_styles_at_every_subtree_depth_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]

    verts = np.array(
        [p.Coordinates for p in f.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    replace_representation(f, element, new_verts, new_faces, scope="element")

    assert unreachable_geometry(f) == {}


def test_removing_a_styled_item_does_not_leave_an_empty_styled_representation():
    """discard対象のIfcStyledItemをIfcStyledRepresentation.Itemsから参照させ、
    差し替え後にそのrepresentationが残っていない(または空でない)ことを
    確認する(F4)。

    IFC4のIfcRepresentation.ItemsはSET[1:?]であり、空集合はスキーマ違反。
    参照元のIfcStyledItemを削除しただけではItemsが空リストのまま黙って
    残ってしまう(ifcopenshell.file.removeがinverse参照を自動パッチするため
    danglingにはならないが、スキーマ違反のオブジェクトは残る)。
    """
    f = build_element_with_a_discarded_style_referenced_by_a_styled_representation_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    styled_rep_id = f.by_type("IfcStyledRepresentation")[0].id()

    verts = np.array(
        [p.Coordinates for p in f.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    replace_representation(f, element, new_verts, new_faces, scope="element")

    try:
        styled_rep = f.by_id(styled_rep_id)
    except RuntimeError:
        return  # representation自体が削除されていれば要件を満たす
    assert styled_rep.Items  # 残っているなら空であってはならない


# --- IfcPresentationStyleAssignment の異常な連鎖に対する耐性(レビュー指摘) ---
#
# ラッパーは入れ子にも循環にもなり得る。再帰で書くと深い連鎖で RecursionError に
# なり、_resolve_surface_rgb を呼ぶ extract_model がファイル全体の読込ごと
# 失敗する(実際に2000段で再現した)。反復実装であることを固定する。


def _make_surface_style(f, rgb):
    """RGB から IfcSurfaceStyle を1つ作る(このテスト専用の最小ヘルパ)。"""
    colour = f.create_entity(
        "IfcColourRgb", Red=float(rgb[0]), Green=float(rgb[1]), Blue=float(rgb[2])
    )
    rendering = f.create_entity(
        "IfcSurfaceStyleRendering", SurfaceColour=colour, ReflectanceMethod="NOTDEFINED"
    )
    return f.create_entity("IfcSurfaceStyle", Side="BOTH", Styles=[rendering])


def test_resolve_surface_rgb_stops_on_a_cyclic_style_wrapper():
    """自己参照・相互参照する IfcPresentationStyleAssignment で無限ループしない。"""
    f = ifcopenshell.file(schema="IFC4")
    style = _make_surface_style(f, (0.1, 0.2, 0.3))

    self_ref = f.create_entity("IfcPresentationStyleAssignment", Styles=[style])
    self_ref.Styles = [self_ref]
    assert _resolve_surface_rgb(self_ref) is None

    left = f.create_entity("IfcPresentationStyleAssignment", Styles=[style])
    right = f.create_entity("IfcPresentationStyleAssignment", Styles=[style])
    left.Styles = [right]
    right.Styles = [left]
    assert _resolve_surface_rgb(left) is None


def test_resolve_surface_rgb_handles_a_deep_style_wrapper_chain():
    """5,000段のラッパー連鎖でも RecursionError にならず、末端の色を返す。

    Python の既定再帰上限は1,000。再帰実装だとここで落ちる。
    """
    f = ifcopenshell.file(schema="IFC4")
    style = _make_surface_style(f, (0.1, 0.2, 0.3))
    node = f.create_entity("IfcPresentationStyleAssignment", Styles=[style])
    for _ in range(5000):
        node = f.create_entity("IfcPresentationStyleAssignment", Styles=[node])
    assert _resolve_surface_rgb(node) == (0.1, 0.2, 0.3)


def test_resolve_surface_rgb_returns_the_first_branch_in_document_order():
    """複数のスタイルがぶら下がる場合、先頭の枝の色を返す(先行順を固定する)。"""
    f = ifcopenshell.file(schema="IFC4")
    first = _make_surface_style(f, (1.0, 0.0, 0.0))
    second = _make_surface_style(f, (0.0, 1.0, 0.0))
    wrapper = f.create_entity("IfcPresentationStyleAssignment", Styles=[first, second])
    assert _resolve_surface_rgb(wrapper) == (1.0, 0.0, 0.0)
