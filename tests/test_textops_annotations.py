"""テキストモード削除のアノテーションピン留め(textops/plan.py)のテスト。

IfcPresentationLayerAssignment と IfcStyledItem は IfcRel* ではないため
パッチ対象にならず、その参照1本が sweep の被参照カウントを支え続け、
削除済み要素の幾何が出力に丸ごと残っていた(2026-07-30 実測。donuts族=
テキストモード本来の対象データで「削除したのにサイズが減らない」が再発
する欠陥)。修正は「anchor 参照をカウントから除外+anchor が全滅した
アノテーション自身は死ぬ+部分的に死んだ PLA はパッチ」の三点セット。
"""

import pytest

import ifcopenshell
import ifcopenshell.api

from ifc_occam.scan.fullgraph import scan_full_graph
from ifc_occam.textops.plan import compute_text_delete_plan
from ifc_occam.textops.rewrite import rewrite_without
from tests.fixtures_ifc import (
    attach_layer_assignment,
    build_single_consumer_mapped_child_styled_brep_ifc,
)
from tests.ifc_graph import unreachable_geometry


def _text_delete(tmp_path, f, delete_classes):
    """f を書き出し、実パイプライン(scan→plan→rewrite→再オープン)で
    delete_classes をテキスト削除し、(再オープンした出力, 出力パス) を返す。

    出力を raw 再スキャンし、未解決参照が入力から増えていないことを毎回
    検査する。ifcopenshell.open は dangling を黙って許容するため、再オープン
    成功だけではパッチ漏れ(例: PLA の AssignedItems に死んだ id が残る)を
    検知できない(タスクレビュー Important-1 で実測された検出線の穴。この
    検査は「_generic_rel_candidates から PLA を外す」ミューテーションを
    合成フィクスチャ単体で赤にする)。"""
    src = tmp_path / "src.ifc"
    out = tmp_path / "out.ifc"
    f.write(str(src))
    graph = scan_full_graph(str(src))
    plan = compute_text_delete_plan(graph, delete_classes)
    rewrite_without(str(src), str(out), plan, graph, "src.ifc")
    unresolved_in = int((graph.ref_targets == -1).sum())
    out_graph = scan_full_graph(str(out))
    unresolved_out = int((out_graph.ref_targets == -1).sum())
    assert unresolved_out <= unresolved_in, (
        f"テキスト削除が dangling を作った: 未解決参照 {unresolved_in} -> {unresolved_out}"
    )
    return ifcopenshell.open(str(out)), out


def _count(f, name):
    return len(f.by_type(name))


def _make_wall_with_own_brep(f, body_ctx):
    """既存フィクスチャに、独立した brep 形状を持つ IfcWall を1体足す
    (クラス単位削除のテストで「片方のクラスだけ消す」ために使う)。
    shell には色付き IfcStyledItem を付ける(共有スタイルのテスト用に
    既存の style エンティティを使い回せるよう、style を引数で受けない
    代わりに呼び出し側が後から付け替えられるよう shell を返す)。"""
    coords = [(10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (10.0, 1.0, 0.0), (10.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    faces = []
    for idx in [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)
    rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    wall = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcWall", name="W1")
    wall.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=wall)
    return wall, rep, shell


def test_text_delete_releases_layer_pinned_geometry(tmp_path):
    """rep 直付けの PLA(donuts 実データの形)。要素クラスを全削除したら、
    幾何(rep/map/brep/shell/face/polyloop)も PLA 自身も出力から消える。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    body_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    attach_layer_assignment(f, [body_rep], name="合成レイヤー - 図形")

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcBuildingElementProxy") == 0
    assert _count(out, "IfcShapeRepresentation") == 0
    assert _count(out, "IfcRepresentationMap") == 0
    assert _count(out, "IfcFacetedBrep") == 0
    assert _count(out, "IfcClosedShell") == 0
    assert _count(out, "IfcFace") == 0
    assert _count(out, "IfcPolyLoop") == 0
    assert _count(out, "IfcPresentationLayerAssignment") == 0


def test_text_delete_releases_styled_item_pinned_geometry(tmp_path):
    """レイヤー無しでも、StyledItem(shell 直付け)が幾何をピン留めして残す
    のが従来の挙動(2026-07-30 実測: shell/face×4/polyloop×4 が残存)。
    修正後は StyledItem も孤立したスタイル一式も幾何も全て消える。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcClosedShell") == 0
    assert _count(out, "IfcFace") == 0
    assert _count(out, "IfcPolyLoop") == 0
    assert _count(out, "IfcStyledItem") == 0
    assert _count(out, "IfcSurfaceStyle") == 0
    assert _count(out, "IfcSurfaceStyleRendering") == 0


def test_partially_dead_pla_is_patched_and_survivor_keeps_membership(tmp_path):
    """1つの PLA が2クラスの要素の rep を束ねる。片方のクラスだけ削除したら、
    PLA は生き残り、死んだ rep だけが AssignedItems から抜ける(パッチ)。
    抜けないと、回収された幾何への dangling になる。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    body_ctx = proxy_rep.ContextOfItems
    _wall, wall_rep, _shell = _make_wall_with_own_brep(f, body_ctx)
    attach_layer_assignment(f, [proxy_rep, wall_rep], name="部材")
    proxy_rep_id = proxy_rep.id()

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    plas = out.by_type("IfcPresentationLayerAssignment")
    assert len(plas) == 1
    assert plas[0].Name == "部材"
    assigned = list(plas[0].AssignedItems)
    assert len(assigned) == 1
    assert assigned[0].id() != proxy_rep_id
    assert assigned[0].is_a("IfcShapeRepresentation")
    # 生き残った壁の幾何は無傷
    assert _count(out, "IfcWall") == 1
    assert _count(out, "IfcFacetedBrep") == 1
    assert unreachable_geometry(out) == {}


def test_shared_style_survives_while_any_user_lives(tmp_path):
    """2要素の StyledItem が同一の IfcSurfaceStyle を共有。片方のクラスだけ
    削除したらスタイルは残り、両方消したらスタイルも消える。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    body_ctx = proxy_rep.ContextOfItems
    shared_style = f.by_type("IfcSurfaceStyle")[0]
    _wall, _wall_rep, wall_shell = _make_wall_with_own_brep(f, body_ctx)
    f.create_entity("IfcStyledItem", Item=wall_shell, Styles=[shared_style])

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])
    assert _count(out, "IfcSurfaceStyle") == 1  # 壁側がまだ使っている
    assert _count(out, "IfcStyledItem") == 1

    out2, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy", "IfcWall"])
    assert _count(out2, "IfcSurfaceStyle") == 0
    assert _count(out2, "IfcStyledItem") == 0


def test_styled_item_without_item_keeps_itself_and_its_styles(tmp_path):
    """Item=$ の IfcStyledItem(IfcStyledRepresentation 配下の形)。anchor を
    持たないので死なず、その Styles も残る(先頭参照=スタイルを anchor と
    誤認して回収したら dangling)。無関係なクラスの削除で確認する。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    body_ctx = proxy_rep.ContextOfItems
    _make_wall_with_own_brep(f, body_ctx)
    lonely_style = f.create_entity(
        "IfcSurfaceStyle",
        Name="material-only",
        Side="BOTH",
        Styles=[
            f.create_entity(
                "IfcSurfaceStyleRendering",
                SurfaceColour=f.create_entity(
                    "IfcColourRgb", Red=0.9, Green=0.1, Blue=0.1
                ),
                ReflectanceMethod="NOTDEFINED",
            )
        ],
    )
    f.create_entity("IfcStyledItem", Item=None, Styles=[lonely_style])

    out, _ = _text_delete(tmp_path, f, ["IfcWall"])

    assert _count(out, "IfcStyledItem") == 2  # 既存1 + Item=$ の1
    names = {getattr(s, "Name", None) for s in out.by_type("IfcSurfaceStyle")}
    assert "material-only" in names


def test_layer_with_style_still_pins_conservatively(tmp_path):
    """IfcPresentationLayerWithStyle は LayerStyles 参照が混ざり平坦な参照列
    から区別できないため対象外(従来どおりピン留め)。幾何は残るが dangling
    は生まれない、という保守的な挙動を固定する(既知の限界の番人)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    body_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    curve_style = f.create_entity("IfcCurveStyle", Name="L")
    f.create_entity(
        "IfcPresentationLayerWithStyle",
        Name="スタイル付き層",
        AssignedItems=[body_rep],
        LayerOn=True,
        LayerFrozen=False,
        LayerBlocked=False,
        LayerStyles=[curve_style],
    )

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    # ピン留めされた幾何は残る(保守側)。参照切れが無いことが本質。
    assert _count(out, "IfcPresentationLayerWithStyle") == 1
    assert _count(out, "IfcShapeRepresentation") >= 1
    reopened = out  # ifcopenshell.open が通っている時点で参照解決は成立
    la = reopened.by_type("IfcPresentationLayerWithStyle")[0]
    assert len(la.AssignedItems) == 1


def test_referenced_styled_item_in_styled_representation_keeps_pinning(tmp_path):
    """IfcStyledRepresentation.Items に入った IfcStyledItem(I1、フェーズ最終
    レビューの再現プローブ(a))。この StyledItem は入力で参照されている
    (in_degree > 0)ため anchor から除外され、要素削除後も StyledItem 自身と
    その anchor(削除された proxy 側の幾何一式)がピン留めされ続ける(肥大側
    =安全側)。修正前はこの StyledItem が _reap_dead_annotations に回収され、
    IfcStyledRepresentation.Items が空リストのまま出力され無音の dangling
    になっていた(_text_delete の未解決参照数チェックがこれを検知する)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    styled_item = f.by_type("IfcStyledItem")[0]
    body_ctx = proxy_rep.ContextOfItems
    wall, _wall_rep, _wall_shell = _make_wall_with_own_brep(f, body_ctx)
    styled_rep = f.create_entity(
        "IfcStyledRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Style",
        RepresentationType="Material",
        Items=[styled_item],
    )
    wall.Representation.Representations = list(wall.Representation.Representations) + [
        styled_rep
    ]

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    # ピン留め継続: StyledItem自身とその参照先(削除されたproxy側の幾何一式)が残る
    # (IfcClosedShellは2件: 削除されたproxy側の1件+生き残るwall自身の1件)
    assert _count(out, "IfcStyledItem") == 1
    assert _count(out, "IfcClosedShell") == 2
    styled_reps = out.by_type("IfcStyledRepresentation")
    assert len(styled_reps) == 1
    assert len(styled_reps[0].Items) == 1


def test_referenced_styled_item_in_layer_with_style_keeps_pinning(tmp_path):
    """IfcPresentationLayerWithStyle.AssignedItems に入った IfcStyledItem
    (I1、フェーズ最終レビューの再現プローブ(b))。同様に in_degree > 0 のため
    anchor から除外され、StyledItem とその anchor(幾何)がピン留めされ続ける。
    修正前はこの StyledItem が回収され、IfcPresentationLayerWithStyle.
    AssignedItems が空リストのまま出力され無音の dangling になっていた。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    styled_item = f.by_type("IfcStyledItem")[0]
    curve_style = f.create_entity("IfcCurveStyle", Name="L")
    f.create_entity(
        "IfcPresentationLayerWithStyle",
        Name="スタイル付き層2",
        AssignedItems=[styled_item],
        LayerOn=True,
        LayerFrozen=False,
        LayerBlocked=False,
        LayerStyles=[curve_style],
    )

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcStyledItem") == 1
    assert _count(out, "IfcClosedShell") == 1
    layers = out.by_type("IfcPresentationLayerWithStyle")
    assert len(layers) == 1
    assert len(layers[0].AssignedItems) == 1


def test_preexisting_annotation_pinned_orphan_is_reclaimed(tmp_path):
    """入力に元から存在する「PLA だけに参照される孤児 rep」は、何かを1件でも
    削除する実行で一緒に回収される(anchor 除外の副作用を仕様として固定。
    full-open 削除は孤児を触らないため、この点で出力レコード数は full-open
    より少なくなり得る)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    body_ctx = proxy_rep.ContextOfItems
    _make_wall_with_own_brep(f, body_ctx)
    # 孤児 rep(どの製品からも参照されない)を作り、PLA だけが掴む
    orphan_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=list(proxy_rep.Items),  # 中身は共有でよい(器だけ孤児)
    )
    attach_layer_assignment(f, [orphan_rep], name="孤児層")

    out, _ = _text_delete(tmp_path, f, ["IfcWall"])

    names = {la.Name for la in out.by_type("IfcPresentationLayerAssignment")}
    assert "孤児層" not in names
    # 孤児の器は消え、共有していた中身(proxy の幾何)は残る
    assert _count(out, "IfcShapeRepresentation") == 2  # proxyのラッパー+専有マップ内


def test_equivalence_with_full_open_on_layered_fixture(tmp_path):
    """レイヤー+スタイル付きフィクスチャで、テキスト削除と full-open 削除
    (apply_operations)の生存要素 GlobalId と幾何クラス別カウントが一致する
    (これまでの等価性テストはアノテーション無しのフィクスチャしか
    見ていなかった)。"""
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation

    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy_rep = f.by_type("IfcBuildingElementProxy")[0].Representation.Representations[0]
    _wall, wall_rep, _shell = _make_wall_with_own_brep(f, proxy_rep.ContextOfItems)
    attach_layer_assignment(f, [proxy_rep, wall_rep], name="部材")
    gids = [e.GlobalId for e in f.by_type("IfcBuildingElementProxy")]

    # text経路とfull-open経路を同一モデル/同一ソースファイルに対して適用する
    # (GlobalIdはifcopenshell.guid.new()が呼ばれるたびランダムに生成されるため、
    # build()を2回呼んで別モデルを比較するとWallのGlobalIdが一致せず、この
    # 等価性チェック自体が成立しない——_text_delete/test_cui_phase3_equivalence.py
    # と同じ「同一ファイルを両経路に使う」流儀に揃える)。
    text_out, out_path = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])
    src = out_path.parent / "src.ifc"

    full_path = tmp_path / "full.ifc"
    apply_operations(str(src), [Operation(op="delete", targets=gids)], str(full_path))
    full_out = ifcopenshell.open(str(full_path))

    assert {e.GlobalId for e in text_out.by_type("IfcProduct")} == {
        e.GlobalId for e in full_out.by_type("IfcProduct")
    }
    for name in (
        "IfcShapeRepresentation",
        "IfcRepresentationMap",
        "IfcFacetedBrep",
        "IfcClosedShell",
        "IfcFace",
        "IfcPolyLoop",
        "IfcPresentationLayerAssignment",
    ):
        assert _count(text_out, name) == _count(full_out, name), name
