"""形状差し替え・削除の後に、レイヤー割当(IfcPresentationLayerAssignment)が
旧形状をピン留めして残さないこと、およびレイヤー所属が新形状へ引き継がれる
ことのテスト。

発端: test-donuts_mini.ifc(305MB)の decimate 0.1 で出力が -1.7% にしか
ならなかった。レイヤー割当が旧 IfcShapeRepresentation 456件を AssignedItems に
保持し続け、remove_deep2 が「開始要素に inverse が残っている」として無言の
no-op になり、旧形状211万面が死荷重で残った(さらに新repはレイヤー割当に
入らないため、間引いた要素のレイヤー情報が事実上消えるという二重欠陥)。
"""

import numpy as np
import pytest

import ifcopenshell
import ifcopenshell.api

from ifc_occam.core.consolidate import consolidate_duplicates
from ifc_occam.core.duplicates import find_duplicates
from ifc_occam.core.export import _mass_delete
from ifc_occam.core.extract import extract_model
from ifc_occam.core.simplify import bbox_mesh, replace_representation
from tests.fixtures_ifc import (
    attach_layer_assignment,
    build_single_consumer_mapped_child_styled_brep_ifc,
    build_single_element_with_child_styled_brep_ifc,
    build_three_translated_copies_ifc,
    build_two_consumers_mapped_child_styled_brep_ifc,
)
from tests.ifc_graph import unreachable_geometry


def _replace_first_element_with_bbox(f):
    """最初の要素の Body を、全頂点のAABB直方体に差し替える。"""
    element = f.by_type("IfcBuildingElementProxy")[0]
    verts = np.array(
        [p.Coordinates for p in f.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    return element, replace_representation(
        f, element, new_verts, new_faces, scope="element"
    )


def test_layered_rep_is_released_and_membership_moves_to_the_new_rep():
    """rep 直付けのレイヤー割当(donuts 実データの形)。差し替え後、
    旧形状が到達不能で残らず、レイヤー所属は新しい rep に引き継がれる。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    body_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    assignment = attach_layer_assignment(f, [body_rep], name="合成レイヤー - 図形")
    old_rep_id = body_rep.id()

    element, warnings = _replace_first_element_with_bbox(f)

    assert warnings == []
    assert unreachable_geometry(f) == {}
    new_rep = element.Representation.Representations[0]
    assigned_ids = {x.id() for x in assignment.AssignedItems}
    assert new_rep.id() in assigned_ids
    assert old_rep_id not in assigned_ids
    assert assignment.Name == "合成レイヤー - 図形"


def test_one_assignment_bundling_many_reps_keeps_the_other_members():
    """1つの割当が複数要素の rep(マップ用ラッパー)を束ねる(donuts では
    456要素分)。1要素だけ差し替えたとき、他要素の所属は動かさず、対象要素
    だけ旧→新に入れ替わる。

    フィクスチャは donuts と同じく「各要素が専用の IfcShapeRepresentation
    (Items=[IfcMappedItem])を持つ」形を使う。非マップ(rep直持ち)だと
    差し替え後も rep 自体が生き残るため、このバグは発現しない。"""
    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    rep1 = elem1.Representation.Representations[0]
    rep2 = elem2.Representation.Representations[0]
    assignment = attach_layer_assignment(f, [rep1, rep2], name="部材")

    verts = np.array(
        [p.Coordinates for p in f.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    replace_representation(f, elem1, new_verts, new_faces, scope="element")

    assert unreachable_geometry(f) == {}
    new_rep1 = elem1.Representation.Representations[0]
    assigned_ids = {x.id() for x in assignment.AssignedItems}
    assert assigned_ids == {new_rep1.id(), rep2.id()}


def test_item_level_layer_assignment_moves_to_the_new_item():
    """item 直付け(IfcLayeredItem は rep|item の SELECT)。旧アイテムを外し、
    新アイテムへ引き継ぐ。"""
    f = build_single_element_with_child_styled_brep_ifc()
    brep = f.by_type("IfcFacetedBrep")[0]
    assignment = attach_layer_assignment(f, [brep], name="部材")

    element, _ = _replace_first_element_with_bbox(f)

    assert unreachable_geometry(f) == {}
    new_item = element.Representation.Representations[0].Items[0]
    assigned_ids = {x.id() for x in assignment.AssignedItems}
    assert assigned_ids == {new_item.id()}


def test_shared_map_layer_assignment_is_not_hijacked_by_one_consumers_replace():
    """共有 IfcRepresentationMap 内部(共有 IfcFacetedBrep)に付いたレイヤー所属は、
    片方の要素だけを scope="element" で差し替えても奪われない(共有マップ強奪
    ガード _map_is_exclusive の番人。フェーズ最終レビュー I-1。ガードを外すと
    差し替えた要素の新形状にすり替わり、もう片方のレイヤー情報が消える)。"""
    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, _elem2 = f.by_type("IfcBuildingElementProxy")
    brep = f.by_type("IfcFacetedBrep")[0]
    assignment = attach_layer_assignment(f, [brep], name="部材")

    verts = np.array(
        [p.Coordinates for p in f.by_type("IfcCartesianPoint")], dtype=np.float64
    )
    new_verts, new_faces = bbox_mesh(verts)
    replace_representation(f, elem1, new_verts, new_faces, scope="element")

    assigned_ids = {x.id() for x in assignment.AssignedItems}
    assert assigned_ids == {brep.id()}
    assert {x.is_a() for x in assignment.AssignedItems} == {"IfcFacetedBrep"}


def test_replace_in_place_keeps_nested_exclusive_map_rep_assignment_on_the_surviving_rep():
    """rep 直付け(ネスト): body_rep.Items = [brep, IfcMappedItem] の形で、
    IfcMappedItem が専有する内側 IfcRepresentationMap の MappedRepresentation
    (内側rep)にPLAが付いている。_replace_items_in_place 経由の差し替え後、
    PLAは item へ降格せず、生き残る外側 body_rep(rep自身は差し替えられず
    Itemsだけが入れ替わる)を指す(フェーズ最終レビュー M-1)。"""
    f = build_single_element_with_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    brep = body_rep.Items[0]
    old_body_rep_id = body_rep.id()

    # この要素だけが使う専有マップを、もう1つのitemとしてbody_repに足す。
    inner_coords = [(2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (2.0, 1.0, 0.0), (2.0, 0.0, 1.0)]
    inner_points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in inner_coords]
    inner_face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    inner_faces = []
    for idx in inner_face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[inner_points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        inner_faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    inner_shell = f.create_entity("IfcClosedShell", CfsFaces=inner_faces)
    inner_brep = f.create_entity("IfcFacetedBrep", Outer=inner_shell)
    inner_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_rep.ContextOfItems,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[inner_brep],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap", MappingOrigin=identity, MappedRepresentation=inner_rep
    )
    mapped_item = f.create_entity(
        "IfcMappedItem",
        MappingSource=rep_map,
        MappingTarget=f.create_entity(
            "IfcCartesianTransformationOperator3D",
            Axis1=None,
            Axis2=None,
            Axis3=None,
            Scale=None,
            LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        ),
    )
    body_rep.Items = [brep, mapped_item]

    assignment = attach_layer_assignment(f, [inner_rep], name="部材")

    element, warnings = _replace_first_element_with_bbox(f)

    assert unreachable_geometry(f) == {}
    new_body_rep = element.Representation.Representations[0]
    assert new_body_rep.id() == old_body_rep_id  # in-placeパスなのでrep自体は生き残る
    assigned_ids = {x.id() for x in assignment.AssignedItems}
    assert assigned_ids == {new_body_rep.id()}


def test_transfer_with_no_targets_detaches_and_removes_an_emptied_assignment():
    """引き継ぎ先が無い呼び出しでは所属を外すだけになり、空になった割当は
    ファイルから削除される(ifcopenshell は SET[1:?] の空代入を検証しない
    ため、消さないとスキーマ違反の壊れた割当が出力に残る)。"""
    from ifc_occam.core.simplify import _transfer_layer_assignments

    f = build_single_element_with_child_styled_brep_ifc()
    brep = f.by_type("IfcFacetedBrep")[0]
    assignment = attach_layer_assignment(f, [brep])
    assignment_id = assignment.id()

    _transfer_layer_assignments(f, [brep], None, None)

    with pytest.raises(RuntimeError):
        f.by_id(assignment_id)


def test_transfer_removes_orphaned_layer_with_style_but_protects_shared_style():
    """IfcPresentationLayerWithStyle が空になって削除されるとき、LayerStyles
    配下で他から参照されなくなった style(IfcSurfaceStyle等)も孤児として
    残さない。ただし他の割当がまだ参照しているstyleは保護される
    (フェーズ最終レビュー M-2)。"""
    from ifc_occam.core.simplify import _transfer_layer_assignments

    f = build_single_element_with_child_styled_brep_ifc()
    brep = f.by_type("IfcFacetedBrep")[0]

    def _make_style(rgb):
        colour = f.create_entity(
            "IfcColourRgb", Red=rgb[0], Green=rgb[1], Blue=rgb[2]
        )
        shading = f.create_entity("IfcSurfaceStyleShading", SurfaceColour=colour)
        return f.create_entity("IfcSurfaceStyle", Side="BOTH", Styles=[shading])

    orphan_style = _make_style((1.0, 0.0, 0.0))
    shared_style = _make_style((0.0, 1.0, 0.0))
    unrelated_item = f.create_entity("IfcCartesianPoint", Coordinates=(9.0, 9.0, 9.0))

    assignment = f.create_entity(
        "IfcPresentationLayerWithStyle",
        Name="色付きレイヤー",
        AssignedItems=[brep],
        LayerStyles=[orphan_style, shared_style],
    )
    other_assignment = f.create_entity(
        "IfcPresentationLayerWithStyle",
        Name="別レイヤー",
        AssignedItems=[unrelated_item],
        LayerStyles=[shared_style],
    )
    assignment_id = assignment.id()

    _transfer_layer_assignments(f, [brep], None, None)

    with pytest.raises(RuntimeError):
        f.by_id(assignment_id)
    with pytest.raises(RuntimeError):
        f.by_id(orphan_style.id())
    # shared_style は other_assignment からまだ参照されているため保護される。
    assert f.by_id(shared_style.id()) is not None
    assert list(other_assignment.LayerStyles) == [shared_style]


def test_cleanup_reports_an_item_it_could_not_remove():
    """remove_deep2 の無言の no-op を警告として可視化する番人。
    旧トップアイテムが別の rep からも参照されている(専有でない)状況では、
    掃除は正しく退き、その事実が参照元の型つきで警告として返る。"""
    f = build_single_element_with_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    brep = f.by_type("IfcFacetedBrep")[0]
    body_rep = element.Representation.Representations[0]
    other_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_rep.ContextOfItems,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    other = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E2"
    )
    other.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[other_rep]
    )

    _, warnings = _replace_first_element_with_bbox(f)

    assert any("削除できませんでした" in w for w in warnings)
    assert any("IfcShapeRepresentation" in w for w in warnings)
    # brep は other 経由で製品から到達可能なので、残っていても健全
    assert unreachable_geometry(f) == {}


def test_root_remove_product_cleans_layered_geometry():
    """フルオープン削除(root.remove_product)はレイヤー割当があっても幾何を
    残さない(2026-07-29 実測で健全と確認済み。回帰の番人)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    attach_layer_assignment(f, [body_rep])

    ifcopenshell.api.run("root.remove_product", f, product=element)

    assert unreachable_geometry(f) == {}
    assert f.by_type("IfcPresentationLayerAssignment") == []
    assert f.by_type("IfcFacetedBrep") == []


def test_mass_delete_batch_path_cleans_layered_geometry():
    """閾値超で使われる batch 経路(_mass_delete)でも同じく残さない。
    batch_remove_deep2 は削除を遅延させるため、レイヤー割当との組み合わせは
    このテストが初めて押さえる。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    attach_layer_assignment(f, [body_rep])

    f2 = _mass_delete(f, [element.GlobalId])

    assert unreachable_geometry(f2) == {}
    assert f2.by_type("IfcPresentationLayerAssignment") == []
    assert f2.by_type("IfcFacetedBrep") == []


def test_oracle_detects_an_orphan_representation():
    """オラクル自身の番人: どの製品からも参照されない孤児 rep(とその幾何)を
    検出できる。旧オラクルは全 rep をルートに数えたため、この形が見えなかった
    (donuts バグの出力でも空辞書を返していた穴)。"""
    f = build_single_element_with_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    product_shape = element.Representation
    product_shape.Representations = []  # 製品から切り離す(repは残る)

    assert unreachable_geometry(f) == {
        "IfcClosedShell": 1,
        "IfcFace": 4,
        "IfcPolyLoop": 4,
        "IfcFacetedBrep": 1,
        "IfcShapeRepresentation": 1,
    }


# ---------------------------------------------------------------------------
# フェーズ最終レビュー I-3: consolidate.py の item レベル PLA(スコープ外だが
# 確定した欠陥。「縮めるはずが増える」の再現・回帰テスト)
# ---------------------------------------------------------------------------


def test_consolidate_transfers_item_level_layer_assignments_to_the_shared_item():
    """3要素の各幾何アイテムに1つずつ item レベルの PLA が付いている状態で
    consolidate すると、共有ソース側の新アイテムへ所属が引き継がれ、旧形状も
    掃除される(consolidateが「縮めるはずが増える」欠陥の再現・回帰。
    フェーズ最終レビュー I-3)。"""
    f = build_three_translated_copies_ifc()
    elements = f.by_type("IfcBuildingElementProxy")
    assignments = []
    for element in elements:
        old_item = element.Representation.Representations[0].Items[0]
        assignments.append(attach_layer_assignment(f, [old_item], name="部材"))

    model, warnings = extract_model(f)
    assert warnings == []
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1

    report = consolidate_duplicates(f, groups, model, min_benefit_ratio=0)

    assert report.groups_applied == 1
    assert report.elements_remapped == 3
    assert unreachable_geometry(f) == {}
    # 純増しない: 共有ソース側の1件だけに減る(旧3件は消える)。
    assert len(f.by_type("IfcTriangulatedFaceSet")) == 1

    shared_item = f.by_type("IfcRepresentationMap")[0].MappedRepresentation.Items[0]
    for assignment in assignments:
        assigned_ids = {x.id() for x in assignment.AssignedItems}
        assert assigned_ids == {shared_item.id()}


# ---------------------------------------------------------------------------
# carry-forward Phase D Task2: GUI出力チェックボックス(consolidate)経路の
# レイヤー保存をプローブして固定(事前プローブの実測: 両構成とも dangling
# ゼロ。consolidate.py の _transfer_layer_assignments 呼び出しが両ケースを
# カバー済みと確認)。
# ---------------------------------------------------------------------------


def test_consolidate_preserves_rep_attached_layer_assignment(tmp_path):
    """consolidate=True(重複形状の共有化)で、rep 直付けのレイヤー割当が
    dangling にならず生き残ること(carry-forward「GUIのconsolidateレイヤー
    未検証」の固定)。"""
    from ifc_occam.core.export import apply_operations

    f = build_three_translated_copies_ifc()
    reps = [
        e.Representation.Representations[0]
        for e in f.by_type("IfcBuildingElementProxy")
    ]
    attach_layer_assignment(f, reps, name="レイヤーA")

    src_path = tmp_path / "src.ifc"
    f.write(str(src_path))
    out_path = str(tmp_path / "out.ifc")
    report = apply_operations(
        str(src_path), [], out_path, consolidate=True, consolidate_min_benefit_ratio=0
    )
    assert report.consolidated_elements == 3

    reopened = ifcopenshell.open(out_path)
    plas = reopened.by_type("IfcPresentationLayerAssignment")
    assert len(plas) == 1
    assert plas[0].Name == "レイヤーA"
    # rep直付けは body_rep 自体(Itemsだけが共有マップへの参照に差し替わる)を
    # 指し続けるため、3件とも生き残る(実測)。
    assert len(plas[0].AssignedItems) == 3
    for item in plas[0].AssignedItems:
        # 参照先が出力に実在する(dangling でない)ことを id 再解決で確認
        assert reopened.by_id(item.id()) is not None
        assert item.is_a() == "IfcShapeRepresentation"
    assert unreachable_geometry(reopened) == {}


def test_consolidate_transfers_item_attached_layer_assignment_through_apply_operations(tmp_path):
    """item 直付けのレイヤー割当(consolidateで置き換えられる旧アイテムに
    直接付いた形)。consolidate は置き換えで消える旧アイテムから、共有ソース側の
    新アイテムへ所属を引き継ぐ(_transfer_layer_assignments、フェーズ最終レビュー
    I-3 の実装が apply_operations 経由でも効いていることを実測で固定)。
    引き継ぎ先は3要素とも同じ共有アイテム1件のため、SETの重複排除で
    AssignedItems は1件に収束する(実測)。"""
    from ifc_occam.core.export import apply_operations

    f = build_three_translated_copies_ifc()
    items = [
        e.Representation.Representations[0].Items[0]
        for e in f.by_type("IfcBuildingElementProxy")
    ]
    attach_layer_assignment(f, items, name="レイヤーB")

    src_path = tmp_path / "src.ifc"
    f.write(str(src_path))
    out_path = str(tmp_path / "out.ifc")
    report = apply_operations(
        str(src_path), [], out_path, consolidate=True, consolidate_min_benefit_ratio=0
    )
    assert report.consolidated_elements == 3

    reopened = ifcopenshell.open(out_path)
    plas = reopened.by_type("IfcPresentationLayerAssignment")
    assert len(plas) == 1
    assert plas[0].Name == "レイヤーB"
    assigned = list(plas[0].AssignedItems)
    assert len(assigned) == 1
    assert assigned[0].is_a() == "IfcTriangulatedFaceSet"
    assert reopened.by_id(assigned[0].id()) is not None
    shared_item = reopened.by_type("IfcRepresentationMap")[0].MappedRepresentation.Items[0]
    assert assigned[0].id() == shared_item.id()
    assert unreachable_geometry(reopened) == {}


# --- 書き出し時GC経路(apply_operations 既定)の統合テスト ---


def test_apply_operations_gc_path_cleans_layered_geometry(tmp_path):
    """既定(geometry_cleanup="gc")の apply_operations で、レイヤー付き専有マップ
    要素の decimate 後に旧形状が残らず、レイヤー所属が新repへ移り、一時fat
    ファイルも残らない。"""
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation

    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    attach_layer_assignment(f, [body_rep], name="合成レイヤー - 図形")
    src = tmp_path / "src.ifc"
    f.write(str(src))

    out = tmp_path / "out.ifc"
    ops = [Operation(op="simplify", targets=[element.GlobalId], scope="element",
                     params={"method": "bbox"})]
    report = apply_operations(str(src), ops, str(out))

    assert [w for w in report.warnings if "削除できません" in w] == []
    # GCが実行された証拠はタイマーではなく構造で取る: doomed_sink 経路では
    # 要素ループが掃除をしないため、下の「旧 IfcFacetedBrep が消えている」は
    # GCが走った場合にしか成立しない。stage_seconds["gc"] の値そのものは
    # Windows の time.monotonic 分解能(15.6ms)より極小フィクスチャのGCが
    # 速く、>0 を期待すると約50%で 0.0 に丸まってフレークする(実測)。
    assert "gc" in report.stage_seconds
    assert not (tmp_path / "out.ifc.gc-tmp").exists()

    f2 = ifcopenshell.open(str(out))
    assert unreachable_geometry(f2) == {}
    assert f2.by_type("IfcFacetedBrep") == []
    la = f2.by_type("IfcPresentationLayerAssignment")[0]
    assert la.Name == "合成レイヤー - 図形"
    new_rep = f2.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    assert [x.id() for x in la.AssignedItems] == [new_rep.id()]


def test_apply_operations_gc_path_stamps_provenance_exactly_once(tmp_path):
    """GC経路でも由来刻印はちょうど1回(fat側で刻印済み、GCは素通し)。"""
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation

    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    src = tmp_path / "src.ifc"
    f.write(str(src))
    out = tmp_path / "out.ifc"
    ops = [Operation(op="simplify", targets=[element.GlobalId], scope="element",
                     params={"method": "bbox"})]
    apply_operations(str(src), ops, str(out))

    header = out.read_text(encoding="utf-8", errors="replace")[:4000]
    assert header.count("IFC Occam") == 2  # description内の非正本行1回 + originating_system 1回


def test_apply_operations_gc_rescue_failure_keeps_the_fat_file(tmp_path, monkeypatch):
    """GC失敗時の救済(fatをshutil.moveで出力へ移す)自体が失敗した場合、
    fatを消してはならない(フェーズ最終レビューI1)。無条件の
    `finally: fat_path.unlink(missing_ok=True)` のままだと、move失敗時に
    fatと出力の両方が消え、数十分かけた適用結果が全損する。gc_rewriteと
    shutil.moveの両方を失敗させ、fatが残ること・警告にfatのパスが
    含まれることを確認する。"""
    import ifc_occam.core.export as export_mod
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation

    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    src = tmp_path / "src.ifc"
    f.write(str(src))
    out = tmp_path / "out.ifc"
    ops = [Operation(op="simplify", targets=[element.GlobalId], scope="element",
                     params={"method": "bbox"})]

    def _boom_gc_rewrite(*a, **kw):
        raise MemoryError("gc_rewriteが落ちた(再現用)")

    def _boom_move(*a, **kw):
        raise OSError(28, "no space left on device(再現用)")

    monkeypatch.setattr(export_mod, "gc_rewrite", _boom_gc_rewrite)
    monkeypatch.setattr(export_mod.shutil, "move", _boom_move)

    report = apply_operations(str(src), ops, str(out))

    fat_path = out.with_name(out.name + ".gc-tmp")
    assert fat_path.exists()  # 救済も失敗したのでfatは残す(全損を防ぐ)
    assert not out.exists()  # 救済が失敗したので出力は作られていない
    assert any(str(fat_path) in w for w in report.warnings)
    assert any("GCの救済にも失敗しました" in w for w in report.warnings)


def test_apply_operations_inline_path_still_works(tmp_path):
    """geometry_cleanup="inline" は従来どおりその場で掃除する(退避経路の番人)。"""
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation

    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    attach_layer_assignment(f, [body_rep])
    src = tmp_path / "src.ifc"
    f.write(str(src))
    out = tmp_path / "out.ifc"
    ops = [Operation(op="simplify", targets=[element.GlobalId], scope="element",
                     params={"method": "bbox"})]
    report = apply_operations(str(src), ops, str(out), geometry_cleanup="inline")

    assert report.stage_seconds["gc"] == 0.0
    f2 = ifcopenshell.open(str(out))
    assert unreachable_geometry(f2) == {}
    assert f2.by_type("IfcFacetedBrep") == []


def test_inline_batch_path_cleans_and_does_not_warn(tmp_path, monkeypatch):
    """inline+バッチ経路(閾値超)でも旧形状が消え、バッチ遅延を残置と誤検知した
    警告が出ない。閾値は monkeypatch で 0 にして少数要素で発動させる。"""
    import ifc_occam.core.export as export_mod
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation

    monkeypatch.setattr(export_mod, "_SIMPLIFY_BATCH_THRESHOLD", 0)

    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    attach_layer_assignment(f, [body_rep])
    src = tmp_path / "src.ifc"
    f.write(str(src))
    out = tmp_path / "out.ifc"
    ops = [Operation(op="simplify", targets=[element.GlobalId], scope="element",
                     params={"method": "bbox"})]
    report = apply_operations(str(src), ops, str(out), geometry_cleanup="inline")

    assert [w for w in report.warnings if "削除できません" in w] == []
    f2 = ifcopenshell.open(str(out))
    assert unreachable_geometry(f2) == {}
    assert f2.by_type("IfcFacetedBrep") == []


def test_gc_path_releases_the_model_before_the_graph_scan(tmp_path, monkeypatch):
    """GC(fatの約4.8倍のメモリ)が始まる前に、フルオープン中のモデル
    (ファイルサイズの約14倍)が解放されていることを weakref で固定する。

    ifc_file = None だけでは解放されない: apply_operations のパラメータ束縛
    (src)と simplify ループの element 束縛が同じオブジェクトを掴んだまま
    関数末尾まで生きる(GCフェーズ最終レビュー I4 の修正ウェーブで weakref
    実測により発見)。この3つを揃って手放す実装を再発防止として固定する。"""
    import gc as pygc
    import weakref

    import ifc_occam.core.export as export_mod
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation
    from ifc_occam.textops import gc as gc_mod

    f = build_single_consumer_mapped_child_styled_brep_ifc()
    gid = f.by_type("IfcBuildingElementProxy")[0].GlobalId
    model_ref = weakref.ref(f)
    alive_at_gc = {}

    real_gc_rewrite = gc_mod.gc_rewrite

    def spy_gc_rewrite(fat_path, out_path, doomed_root_ids, source_name):
        pygc.collect()
        alive_at_gc["alive"] = model_ref() is not None
        return real_gc_rewrite(fat_path, out_path, doomed_root_ids, source_name)

    monkeypatch.setattr(export_mod, "gc_rewrite", spy_gc_rewrite)

    # 呼び出し式の評価中以外にこちら側の参照を残さない受け渡し
    holder = [f]
    del f
    ops = [Operation(op="simplify", targets=[gid], scope="element",
                     params={"method": "bbox"})]
    apply_operations(holder.pop(), ops, str(tmp_path / "out.ifc"))

    assert alive_at_gc["alive"] is False
