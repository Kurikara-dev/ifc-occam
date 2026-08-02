"""合成IFC(IFC4)ビルダ。core/cascade.py のテスト用フィクスチャ。

ifcopenshell 0.8.5 では void.* は feature.* にリネームされている
(ifcopenshell.api.feature.add_feature / add_filling)。ここでは文字列usecase名
(`ifcopenshell.api.run("...", ...)`)で呼ぶ。幾何・配置・コンテキストは cascade の
関係走査には不要なため作らない(最小構成)。
"""

from __future__ import annotations

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.api.geometry
import ifcopenshell.guid
import numpy as np


def build_wall_with_window_ifc() -> ifcopenshell.file:
    """壁+開口+窓+IfcElementAssembly(子部材2つ)を含む合成IFC4を返す。

    関係:
      - Wall1 --HasOpenings(IfcRelVoidsElement)--> Opening1
      - Opening1 --FillsVoids(IfcRelFillsElement)--> Window1
      - Assembly1 --IsDecomposedBy(IfcRelAggregates)--> Member1, Member2
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f)

    wall = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcWall", name="Wall1")
    opening = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcOpeningElement", name="Opening1"
    )
    window = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcWindow", name="Window1")
    ifcopenshell.api.run("feature.add_feature", f, feature=opening, element=wall)
    ifcopenshell.api.run("feature.add_filling", f, opening=opening, element=window)

    assembly = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcElementAssembly", name="Assembly1"
    )
    member1 = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBeam", name="Member1")
    member2 = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBeam", name="Member2")
    ifcopenshell.api.run(
        "aggregate.assign_object", f, products=[member1, member2], relating_object=assembly
    )

    return f


def build_two_elements_sharing_mapped_shape_ifc() -> ifcopenshell.file:
    """2要素が同じ IfcRepresentationMap (三角形1枚の最小形状) を共有する合成IFC4を返す。

    core/simplify.py の scope="shared" テスト用(Task 3)。各要素は自分専用の
    "Body" IfcShapeRepresentation を持つが、その中身は IfcMappedItem 経由で
    同一の IfcRepresentationMap を指す。MappedRepresentation 側を書き換えると
    両要素に波及することを確認できる。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    # メートル単位を明示(既定のミリメートル単位だと geom 抽出値(SI/メートル)を
    # そのまま書き戻す際に単位不整合で座標が1/1000に縮み、再抽出で座標が
    # 精度閾値未満に潰れてしまう。export.py の再抽出テスト用にメートルへ揃える)。
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    # 3点は非axis-aligned(x/y/z全軸に値を持つ)にしてある。z=0固定の平面三角形だと
    # bboxがz方向に厚み0で退化し、再抽出時に法線カリング等で三角形が失われるため
    # (export.py の simplify 再抽出テストで実座標を扱うのに必要)。
    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3)],
    )
    mapped_representation = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=mapped_representation,
    )

    elements = []
    for name in ("Elem1", "Elem2"):
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=name
        )
        mapped_item = f.create_entity(
            "IfcMappedItem",
            MappingSource=rep_map,
            MappingTarget=f.create_entity(
                "IfcCartesianTransformationOperator3D",
                Axis1=None,
                Axis2=None,
                LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
                Scale=None,
                Axis3=None,
            ),
        )
        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="MappedRepresentation",
            Items=[mapped_item],
        )
        product_shape = f.create_entity(
            "IfcProductDefinitionShape",
            Representations=[body_rep],
        )
        element.Representation = product_shape
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)
        elements.append(element)

    return f


def build_two_elements_sharing_representation_directly_ifc() -> ifcopenshell.file:
    """2要素が同じ IfcShapeRepresentation(三角形1枚)を IfcMappedItem を
    介さず直接共有する合成IFC4を返す(実データでは稀な構成)。

    replace_representation の非 mapped 経路は rep をその場で書き換えるため、
    この構成では書き戻しが全共有要素へ波及する。dedup が無いと同じ rep へ
    簡略化が要素数ぶん重ねがけされる(2026-08-01 実測——CUI共有波及フェーズ
    最終レビュー I-3 の carry-forward)。座標・単位の設計意図は
    build_two_elements_sharing_mapped_shape_ifc と同じ。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )
    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3)],
    )
    shared_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    for name in ("Elem1", "Elem2"):
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=name
        )
        element.Representation = f.create_entity(
            "IfcProductDefinitionShape", Representations=[shared_rep]
        )
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)
    return f


def build_three_elements_sharing_mapped_shape_ifc() -> ifcopenshell.file:
    """3要素が同じ IfcRepresentationMap(三角形1枚の最小形状)を共有する合成IFC4を
    返す。build_two_elements_sharing_mapped_shape_ifc の3要素版
    (フェーズ最終レビューI-2の再現・回帰テスト用)。

    各要素のMappingTargetはすべて恒等変換(見た目の差はない)。テスト側で
    `ifc_occam.core.simplify._transform_operator_matrix` を先頭の呼び出しだけ
    None を返すよう monkeypatch すれば、最初に処理される要素だけが
    scope="element" にフォールバックする状況を再現できる。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3)],
    )
    mapped_representation = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=mapped_representation,
    )

    for name in ("Elem1", "Elem2", "Elem3"):
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=name
        )
        mapped_item = f.create_entity(
            "IfcMappedItem",
            MappingSource=rep_map,
            MappingTarget=f.create_entity(
                "IfcCartesianTransformationOperator3D",
                Axis1=None,
                Axis2=None,
                LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
                Scale=None,
                Axis3=None,
            ),
        )
        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="MappedRepresentation",
            Items=[mapped_item],
        )
        product_shape = f.create_entity(
            "IfcProductDefinitionShape",
            Representations=[body_rep],
        )
        element.Representation = product_shape
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_two_elements_sharing_mapped_shape_with_transform_ifc() -> ifcopenshell.file:
    """build_two_elements_sharing_mapped_shape_ifc の変形版: Elem1 の MappingTarget に
    非恒等変換(平行移動 (2,0,0))を持たせる。Elem2 は恒等(既存フィクスチャと同じ)。

    core/simplify.py の scope="shared" 書き戻しバグ(Elem1経由でMappingTargetを
    2重適用してしまう不具合)の再現・回帰テスト用(Final Review Fix3)。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3)],
    )
    mapped_representation = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=mapped_representation,
    )

    # Elem1: MappingTarget = 平行移動 (2,0,0) の非恒等変換。
    mapped_item1 = f.create_entity(
        "IfcMappedItem",
        MappingSource=rep_map,
        MappingTarget=f.create_entity(
            "IfcCartesianTransformationOperator3D",
            Axis1=None,
            Axis2=None,
            LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(2.0, 0.0, 0.0)),
            Scale=None,
            Axis3=None,
        ),
    )
    elem1 = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="Elem1"
    )
    body_rep1 = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="MappedRepresentation",
        Items=[mapped_item1],
    )
    elem1.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep1]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=elem1)

    # Elem2: MappingTarget = 恒等(既存フィクスチャと同じ)。
    mapped_item2 = f.create_entity(
        "IfcMappedItem",
        MappingSource=rep_map,
        MappingTarget=f.create_entity(
            "IfcCartesianTransformationOperator3D",
            Axis1=None,
            Axis2=None,
            LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
            Scale=None,
            Axis3=None,
        ),
    )
    elem2 = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="Elem2"
    )
    body_rep2 = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="MappedRepresentation",
        Items=[mapped_item2],
    )
    elem2.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep2]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=elem2)

    return f


def build_two_maps_sharing_mapped_representation_ifc() -> ifcopenshell.file:
    """クラスの異なる2要素(IfcWall/IfcColumn)が、別々の IfcRepresentationMap
    (M1/M2)経由で同一の IfcShapeRepresentation(MappedRepresentation R)を共有する
    合成IFC4を返す。両要素の MappingTarget はいずれも恒等変換(simplify.py:815の
    恒等分岐に確実に乗せ、shared簡略化がRをin-placeで書き換える経路を通す)。

    CF-C最終レビューI-1で実証されたハイブリッド鍵分裂の再現用: マップidを鍵に
    すると M1/M2 は別idのため鍵が分裂し、同じRが要素数ぶん重ねがけされる。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3)],
    )
    mapped_representation = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    # M1 と M2 は別々の IfcRepresentationMap だが、いずれも同じ
    # mapped_representation(R)を指す。
    rep_map_1 = f.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=mapped_representation,
    )
    rep_map_2 = f.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=mapped_representation,
    )

    for ifc_class, name, rep_map in (
        ("IfcWall", "Wall1", rep_map_1),
        ("IfcColumn", "Column1", rep_map_2),
    ):
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class=ifc_class, name=name
        )
        mapped_item = f.create_entity(
            "IfcMappedItem",
            MappingSource=rep_map,
            MappingTarget=f.create_entity(
                "IfcCartesianTransformationOperator3D",
                Axis1=None,
                Axis2=None,
                LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
                Scale=None,
                Axis3=None,
            ),
        )
        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="MappedRepresentation",
            Items=[mapped_item],
        )
        element.Representation = f.create_entity(
            "IfcProductDefinitionShape", Representations=[body_rep]
        )
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_hybrid_direct_and_mapped_share_ifc() -> ifcopenshell.file:
    """クラスの異なる2要素のうち、一方(IfcWall)は Body で共有rep Rを直接参照し、
    もう一方(IfcColumn)は IfcMappedItem+IfcRepresentationMap M 経由で同じ Rを
    参照する合成IFC4を返す(ハイブリッド構成)。mapped側の MappingTarget は恒等
    変換(shared簡略化がRをin-placeで書き換える経路を通す)。

    CF-C最終レビューI-1で実証されたハイブリッド鍵分裂の再現用: 直接側は鍵=Rの
    id、mapped側は(修正前は)鍵=Mのidとなり分裂し、同じRが二重に書き換わる。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3)],
    )
    shared_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )

    # 直接側: IfcWall が shared_rep をそのまま Body として参照する。
    direct_elem = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcWall", name="Wall1"
    )
    direct_elem.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[shared_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=direct_elem)

    # mapped側: IfcColumn が IfcRepresentationMap M 経由で同じ shared_rep を参照する。
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=shared_rep,
    )
    mapped_item = f.create_entity(
        "IfcMappedItem",
        MappingSource=rep_map,
        MappingTarget=f.create_entity(
            "IfcCartesianTransformationOperator3D",
            Axis1=None,
            Axis2=None,
            LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
            Scale=None,
            Axis3=None,
        ),
    )
    mapped_elem = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcColumn", name="Column1"
    )
    mapped_body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="MappedRepresentation",
        Items=[mapped_item],
    )
    mapped_elem.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[mapped_body_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=mapped_elem)

    return f


def build_millimeter_single_element_ifc() -> ifcopenshell.file:
    """ミリメートル単位(IFC4)の最小合成ファイルを返す(要素1つ、Body representation は空)。

    core/simplify.py の単位変換バグ(replace_representation が SI/メートル座標を
    ミリメートル単位のファイルへ無変換で書き込み、1/1000に縮む不具合)の
    再現・回帰テスト用(Task 3 単位変換修正)。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "MILLIMETERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[],
    )
    product_shape = f.create_entity("IfcProductDefinitionShape", Representations=[body_rep])
    element.Representation = product_shape
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def _create_surface_style(f: ifcopenshell.file, rgb: tuple[float, float, float]):
    """RGBから IfcSurfaceStyle(IfcSurfaceStyleRendering経由) を新規作成する。

    呼び出しごとに新しいエンティティを作る(同じRGBでも別entityになるのが既定)。
    スタイル比較(同一entity id or 同一RGB)の後者ルートをテストで踏むため。
    """
    colour = f.create_entity(
        "IfcColourRgb", Red=float(rgb[0]), Green=float(rgb[1]), Blue=float(rgb[2])
    )
    rendering = f.create_entity(
        "IfcSurfaceStyleRendering", SurfaceColour=colour, ReflectanceMethod="NOTDEFINED"
    )
    return f.create_entity("IfcSurfaceStyle", Side="BOTH", Styles=[rendering])


def _attach_styled_item(f: ifcopenshell.file, item, style):
    return f.create_entity("IfcStyledItem", Item=item, Styles=[style])


def build_single_element_with_styled_item_ifc(
    rgb: tuple[float, float, float] = (1.0, 0.0, 0.0),
) -> ifcopenshell.file:
    """要素1つ、Body表現(IfcTriangulatedFaceSet の四面体)に色付き IfcStyledItem が
    付いている合成IFC4を返す。core/simplify.py のスタイル移送テスト用
    (Phase4 Task2追補: IfcStyledItem transfer)。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    verts = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[tuple(float(c) for c in row) for row in verts],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3), (1, 2, 4), (1, 3, 4), (2, 3, 4)],
    )
    style = _create_surface_style(f, rgb)
    _attach_styled_item(f, tfs, style)

    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    element.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_three_translated_copies_ifc(
    colors: list[tuple[float, float, float] | None] | None = None,
) -> ifcopenshell.file:
    """3要素、各自own個別Body representation(Tessellation)を持ち、各ローカル頂点が
    互いに平行移動の関係にある合成IFC4を返す(非axis-alignedな四面体、重心が全軸に
    値を持つ)。core/consolidate.py のTDD用フィクスチャ(Phase4 Task2)。

    各要素のObjectPlacementはローカル頂点のオフセットとは無関係な値に設定してある
    (ワールドbbox不変性の検証を、placementとlocal頂点が偶然一致して誤魔化されない
    ようにするため)。

    colors: 各要素のBody表現アイテムに付与するIfcStyledItemのRGB(要素と同じ順番、
    3要素分)。None(既定)ならスタイルなし(既存の呼び出し元に影響しない)。要素ごとに
    独立したIfcSurfaceStyleエンティティを作る(同一RGBでもentityは別、というのが
    スタイル比較テストの前提)。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    base = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    face_indices = [(1, 2, 3), (1, 2, 4), (1, 3, 4), (2, 3, 4)]  # IFCは1-based

    offsets = [(10.0, 0.0, 0.0), (0.0, 20.0, 0.0), (5.0, 5.0, 5.0)]
    placements = [(100.0, 0.0, 0.0), (0.0, 200.0, 0.0), (0.0, 0.0, 300.0)]

    for i, (offset, placement) in enumerate(zip(offsets, placements)):
        verts = base + np.array(offset, dtype=np.float64)
        coord_list = f.create_entity(
            "IfcCartesianPointList3D",
            CoordList=[tuple(float(c) for c in row) for row in verts],
        )
        tfs = f.create_entity(
            "IfcTriangulatedFaceSet", Coordinates=coord_list, CoordIndex=face_indices
        )
        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="Tessellation",
            Items=[tfs],
        )
        if colors is not None and colors[i] is not None:
            style = _create_surface_style(f, colors[i])
            _attach_styled_item(f, tfs, style)

        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=f"Elem{i + 1}"
        )
        element.Representation = f.create_entity(
            "IfcProductDefinitionShape", Representations=[body_rep]
        )
        matrix = np.eye(4)
        matrix[:3, 3] = placement
        ifcopenshell.api.run(
            "geometry.edit_object_placement", f, product=element, matrix=matrix
        )

    return f


def build_n_translated_copies_ifc(
    n_members: int, n_verts: int, seed: int = 0
) -> ifcopenshell.file:
    """n_members要素、各自own個別Body representation(Tessellation)を持ち、頂点数
    n_verts の平行移動コピー(扇状三角形分割)を返す。core/consolidate.py の選別ルール
    (savings/overhead比較)のTDD用フィクスチャ(Phase4 Task2追補)。頂点はランダムだが
    seed固定で再現可能。n_verts >= 3 が必要。
    """
    rng = np.random.default_rng(seed)
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    base = rng.uniform(-1.0, 1.0, size=(n_verts, 3))
    faces = [(1, i + 1, i + 2) for i in range(1, n_verts - 1)]  # 扇状三角形分割(1-based)

    for i in range(n_members):
        offset = np.array([10.0 * i, 0.0, 0.0])
        placement = np.array([0.0, 100.0 * i, 0.0])
        verts = base + offset
        coord_list = f.create_entity(
            "IfcCartesianPointList3D",
            CoordList=[tuple(float(c) for c in row) for row in verts],
        )
        tfs = f.create_entity(
            "IfcTriangulatedFaceSet", Coordinates=coord_list, CoordIndex=faces
        )
        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="Tessellation",
            Items=[tfs],
        )
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=f"E{i}"
        )
        element.Representation = f.create_entity(
            "IfcProductDefinitionShape", Representations=[body_rep]
        )
        matrix = np.eye(4)
        matrix[:3, 3] = placement
        ifcopenshell.api.run(
            "geometry.edit_object_placement", f, product=element, matrix=matrix
        )

    return f


def build_ifc2x3_single_element_ifc() -> ifcopenshell.file:
    """IFC2X3 の最小合成ファイルを返す(要素1つ、Body representation は空)。

    ifcopenshell.api の root.create_entity は owner history 生成でユーザー設定を
    要求し IFC2X3 と噛み合わないため、ここではエンティティを手組みする。
    core/simplify.py の IFC2X3 パス(IfcFacetedBrep 書き戻し)テスト用(Task 3 Fix 3)。
    """
    f = ifcopenshell.file(schema="IFC2X3")
    project = f.create_entity("IfcProject", GlobalId=ifcopenshell.guid.new(), Name="P")
    context = f.create_entity(
        "IfcGeometricRepresentationContext",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        ),
    )
    project.RepresentationContexts = [context]

    element = f.create_entity(
        "IfcBuildingElementProxy", GlobalId=ifcopenshell.guid.new(), Name="E1"
    )
    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[],
    )
    product_shape = f.create_entity("IfcProductDefinitionShape", Representations=[body_rep])
    element.Representation = product_shape

    return f


def build_many_minimal_products_ifc(
    n_targets: int,
    n_keep: int = 5,
    target_class: str = "IfcBuildingElementProxy",
    keep_class: str = "IfcWall",
) -> ifcopenshell.file:
    """target_class の要素 n_targets 個(GlobalId のみ持つ最小構成)+
    keep_class の要素 n_keep 個を含む合成IFC4を返す。

    関係(HasOpenings/IsDecomposedBy等)・幾何・配置・OwnerHistoryは一切持たない
    (削除コストだけを見る大量削除の合成テスト用、CUI Phase1 Task7 Stage B)。
    そのため compute_delete_closure のカスケード展開は常に0件(closure_size ==
    n_targets)。target_class 全要素が削除対象、keep_class 全要素が対象外
    (削除後も残ることを確認する側)という想定で使う。

    entity作成は ifcopenshell.api.run("root.create_entity", ...) ではなく
    f.create_entity(...) を直接使う(OwnerHistory等の付随生成コストを避け、
    数千件規模でもテストが高速に走るようにするため)。
    """
    f = ifcopenshell.file(schema="IFC4")

    for i in range(n_targets):
        f.create_entity(target_class, GlobalId=ifcopenshell.guid.new(), Name=f"Target{i}")
    for i in range(n_keep):
        f.create_entity(keep_class, GlobalId=ifcopenshell.guid.new(), Name=f"Keep{i}")

    return f


def build_many_walls_with_openings_ifc(
    n_walls: int,
    n_keep: int = 5,
    fill_every: int = 2,
    keep_class: str = "IfcColumn",
) -> ifcopenshell.file:
    """壁(IfcWall) n_walls件+keep_class の要素 n_keep件を含む合成IFC4を返す。

    build_many_minimal_products_ifc(関係を一切持たない大量削除ベンチ用)の
    対を成すビルダー(CUI Phase1 final review Finding1): 各壁は専属の開口
    (IfcOpeningElement)をIfcRelVoidsElementで持ち、fill_every件ごとに1件は
    その開口をさらに窓(IfcWindow)でIfcRelFillsElement充填する(既定
    fill_every=2で「全件ではなく一部」を再現)。keep_class の要素は関係を
    一切持たず、削除対象外(残存確認用)として使う想定。

    build_many_minimal_products_ifcと同じ理由で、関係エンティティも含めて
    ifcopenshell.api.run(...)ではなくf.create_entity(...)を直接使う
    (OwnerHistory等の付随生成コストを避け、_MASS_DELETE_THRESHOLD(1,000件)
    超の規模でも高速に生成する)。IfcRelVoidsElement/IfcRelFillsElementの
    forward属性(RelatingBuildingElement等)を設定すれば、対応するinverse属性
    (壁のHasOpenings・開口のHasFillings)はifcopenshell側が自動的に維持する
    ため、api usecase(feature.add_feature/add_filling)を経由しなくても
    cascade.compute_delete_closureが辿る本物の関係として機能する。

    幾何・配置・OwnerHistoryは一切持たない(削除連鎖のコストだけを見る
    大量削除の合成テスト用)。
    """
    f = ifcopenshell.file(schema="IFC4")

    for i in range(n_walls):
        wall = f.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name=f"Wall{i}")
        opening = f.create_entity(
            "IfcOpeningElement", GlobalId=ifcopenshell.guid.new(), Name=f"Opening{i}"
        )
        f.create_entity(
            "IfcRelVoidsElement",
            GlobalId=ifcopenshell.guid.new(),
            RelatingBuildingElement=wall,
            RelatedOpeningElement=opening,
        )
        if i % fill_every == 0:
            window = f.create_entity(
                "IfcWindow", GlobalId=ifcopenshell.guid.new(), Name=f"Window{i}"
            )
            f.create_entity(
                "IfcRelFillsElement",
                GlobalId=ifcopenshell.guid.new(),
                RelatingOpeningElement=opening,
                RelatedBuildingElement=window,
            )

    for i in range(n_keep):
        f.create_entity(keep_class, GlobalId=ifcopenshell.guid.new(), Name=f"Keep{i}")

    return f


def build_single_element_with_child_styled_brep_ifc(
    rgb: tuple[float, float, float] = (0.0, 0.25, 1.0),
) -> ifcopenshell.file:
    """要素1つ、Body表現が IfcFacetedBrep(四面体)で、IfcStyledItem が
    トップレベルの IfcFacetedBrep ではなく内側の IfcClosedShell に付いている合成IFC4。

    Rebro2026 の出力(small.ifc)がこの形をしている。実測での IfcStyledItem.Item の
    内訳は IfcClosedShell 1,457件 / IfcOpenShell 268件 が子アイテム側だった。
    トップレベルしか見ないスタイル探索はこのフィクスチャで空振りする。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    # 四面体。面の向きは全て外向き。
    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    faces = []
    for idx in face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)

    style = _create_surface_style(f, rgb)
    # ここが肝: トップレベルの brep ではなく、内側の shell に付ける。
    _attach_styled_item(f, shell, style)

    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    element.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_two_translated_child_styled_cubes_ifc(
    rgb: tuple[float, float, float] = (0.0, 0.25, 1.0),
) -> ifcopenshell.file:
    """2要素、各自own個別Body表現(IfcFacetedBrepの立方体、8頂点12面)を持ち、
    互いに平行移動の関係にある合成IFC4を返す。IfcStyledItem はトップレベルの
    IfcFacetedBrep ではなく内側の IfcClosedShell(子アイテム)に付く
    (build_single_element_with_child_styled_brep_ifc と同じRebro形)。

    core/consolidate.py のスタイル移送テスト用(Task 3修正 F1: consolidate.pyが
    子アイテムスタイルを見落とす不具合の再現・回帰テスト)。頂点数を四面体
    (4頂点)より増やしているのは、savings(節約バイト数)がoverhead×
    min_benefit_ratioの経済フィルタを下回りconsolidate自体がスキップされる
    (=スタイル移送コードに到達しない)のを避けるため。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    base_coords = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 1.0), (0.0, 1.0, 1.0),
    ]
    face_indices = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 5, 4), (0, 1, 5), (1, 6, 5), (1, 2, 6),
        (2, 7, 6), (2, 3, 7), (3, 4, 7), (3, 0, 4),
    ]
    placements = [(100.0, 0.0, 0.0), (0.0, 200.0, 0.0)]

    for i, placement in enumerate(placements):
        points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in base_coords]
        faces = []
        for idx in face_indices:
            loop = f.create_entity("IfcPolyLoop", Polygon=[points[j] for j in idx])
            bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
            faces.append(f.create_entity("IfcFace", Bounds=[bound]))
        shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
        brep = f.create_entity("IfcFacetedBrep", Outer=shell)

        style = _create_surface_style(f, rgb)
        _attach_styled_item(f, shell, style)  # 子アイテム(shell)にスタイル。Rebroの形。

        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="Brep",
            Items=[brep],
        )
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=f"E{i}"
        )
        element.Representation = f.create_entity(
            "IfcProductDefinitionShape", Representations=[body_rep]
        )
        matrix = np.eye(4)
        matrix[:3, 3] = placement
        ifcopenshell.api.run(
            "geometry.edit_object_placement", f, product=element, matrix=matrix
        )

    return f


def build_single_consumer_mapped_child_styled_brep_ifc(
    rgb: tuple[float, float, float] = (0.0, 0.25, 1.0),
) -> ifcopenshell.file:
    """1要素だけが使う IfcRepresentationMap(内部の IfcClosedShell に子スタイル)を
    持つ合成IFC4を返す。要素自身の body_rep.Items は [IfcMappedItem] 1件で、
    そのMappingSource経由で参照する共有マップを他の誰も使っていない
    (=このマップは実質この要素専有で、scope="element"で解いたら丸ごとゴミになる)。

    core/simplify.py の scope="element" テスト用(Task 3修正 F2: 深さ上限のため
    共有マップ内部に絶対到達できず、旧形状が到達不能なまま残った不具合の
    再現・回帰テスト)。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    faces = []
    for idx in face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)

    style = _create_surface_style(f, rgb)
    _attach_styled_item(f, shell, style)  # 子アイテム(shell)にスタイル。Rebroの形。

    mapped_representation = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap", MappingOrigin=identity, MappedRepresentation=mapped_representation
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
    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="MappedRepresentation",
        Items=[mapped_item],
    )
    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    element.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_two_consumers_mapped_child_styled_brep_ifc(
    rgb: tuple[float, float, float] = (0.0, 0.25, 1.0),
) -> ifcopenshell.file:
    """2要素が同じ IfcRepresentationMap を共有し、その内部の IfcClosedShell
    (子アイテム)に色付き IfcStyledItem が付いている合成IFC4を返す
    (build_single_consumer_mapped_child_styled_brep_ifc の「専有」ではない版)。

    core/simplify.py の scope="element" テスト用(Task 3修正 F2の副作用防止:
    共有マップを他の要素も使っている場合、その内部のスタイルを奪ってはならない
    ことの回帰テスト)。2要素のうち片方だけをscope="element"で差し替えても、
    もう片方(共有マップ経由で色を見ているだけの要素)の色は無傷のまま
    残ることを確認する用途。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    faces = []
    for idx in face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)

    style = _create_surface_style(f, rgb)
    _attach_styled_item(f, shell, style)  # 共有マップ内部の子アイテム(shell)にスタイル。

    mapped_representation = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap", MappingOrigin=identity, MappedRepresentation=mapped_representation
    )

    for name in ("Elem1", "Elem2"):
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
        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="MappedRepresentation",
            Items=[mapped_item],
        )
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=name
        )
        element.Representation = f.create_entity(
            "IfcProductDefinitionShape", Representations=[body_rep]
        )
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_brep_with_styles_at_every_subtree_depth_ifc() -> ifcopenshell.file:
    """要素1つ、Body表現の部分木の各階層(brep深さ0/shell深さ1/face深さ2/
    faceOuterBound深さ3/polyLoop深さ4)に、それぞれ別の色の IfcStyledItem が
    付いている合成IFC4を返す。

    core/simplify.py の深さ上限撤廃テスト用(Task 3修正 F2/F3)。深さ2までしか
    見ない実装ではdepth3/4のスタイルを取りこぼし、旧形状(IfcFaceOuterBound/
    IfcPolyLoop)が到達不能なまま残った。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    faces = []
    bounds = []
    loops = []
    for idx in face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        face = f.create_entity("IfcFace", Bounds=[bound])
        loops.append(loop)
        bounds.append(bound)
        faces.append(face)
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)

    _attach_styled_item(f, brep, _create_surface_style(f, (1.0, 0.0, 0.0)))  # 深さ0
    _attach_styled_item(f, shell, _create_surface_style(f, (0.0, 1.0, 0.0)))  # 深さ1
    _attach_styled_item(f, faces[0], _create_surface_style(f, (0.0, 0.0, 1.0)))  # 深さ2
    _attach_styled_item(f, bounds[1], _create_surface_style(f, (0.5, 0.0, 0.5)))  # 深さ3
    _attach_styled_item(f, loops[2], _create_surface_style(f, (1.0, 1.0, 0.0)))  # 深さ4

    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    element.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_element_with_a_discarded_style_referenced_by_a_styled_representation_ifc() -> (
    ifcopenshell.file
):
    """要素1つ、Body表現の部分木に2つの色(brep深さ0=kept想定、shell深さ1=discard
    対象)を持ち、discard対象になる方の IfcStyledItem が、要素の
    IfcProductDefinitionShape に紐づく別の IfcStyledRepresentation.Items からも
    参照されている合成IFC4を返す(legacy style-assignment pattern)。

    core/simplify.py のTask 3修正 F4テスト用: IfcStyledItem削除で
    IfcStyledRepresentation.Itemsが空リストになり、IFC4のSET[1:?]制約に
    違反するオブジェクトが黙って残る不具合の再現・回帰テスト。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    faces = []
    for idx in face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)

    _attach_styled_item(f, brep, _create_surface_style(f, (1.0, 0.0, 0.0)))  # 深さ0(kept想定)
    si_discarded = _attach_styled_item(
        f, shell, _create_surface_style(f, (0.0, 1.0, 0.0))
    )  # 深さ1(discard対象)

    styled_rep = f.create_entity(
        "IfcStyledRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Style",
        RepresentationType="Material",
        Items=[si_discarded],
    )
    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    element.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep, styled_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_single_element_with_different_top_and_child_styles_ifc(
    top_rgb: tuple[float, float, float] = (1.0, 0.0, 0.0),
    child_rgb: tuple[float, float, float] = (0.0, 1.0, 0.0),
) -> ifcopenshell.file:
    """要素1つ、Body表現が IfcFacetedBrep(四面体)で、トップレベルの brep と
    内側の IfcClosedShell の両方に、別々の色の IfcStyledItem が付いている合成IFC4。

    color-task-4 追補: geom(ifcopenshell.geom)はトップレベルの item に付いた
    スタイルを直接解決できるため、この構成では geom 自体が top_rgb を返す
    (子の child_rgb は見ない)。トップレベルの色が勝つことをエンドツーエンドで
    固定するための回帰テスト用フィクスチャ。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    faces = []
    for idx in face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)

    _attach_styled_item(f, brep, _create_surface_style(f, top_rgb))  # トップレベル
    _attach_styled_item(f, shell, _create_surface_style(f, child_rgb))  # 子(無視される想定)

    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    element.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def _wrap_in_style_assignment(f: ifcopenshell.file, style):
    """IfcSurfaceStyle を IfcPresentationStyleAssignment(IFC2X3由来のdeprecated
    wrapper)で包む。color-task-4 追補: small.ifc(Rebro2026出力)の IfcStyledItem
    4,053件は実測で全てこの形(IfcStyledItem.Styles -> IfcPresentationStyleAssignment
    -> IfcSurfaceStyle)だった。直接 IfcSurfaceStyle を持つ既存フィクスチャとは別に、
    このwrapper経由の形を再現するために使う。
    """
    return f.create_entity("IfcPresentationStyleAssignment", Styles=[style])


def build_single_element_with_wrapped_child_styled_brep_ifc(
    rgb: tuple[float, float, float] = (0.0, 0.25, 1.0),
) -> ifcopenshell.file:
    """build_single_element_with_child_styled_brep_ifc と同じ形(四面体の
    IfcFacetedBrep、スタイルは内側の IfcClosedShell)だが、IfcStyledItem.Styles が
    IfcSurfaceStyle を直接ではなく IfcPresentationStyleAssignment 経由で持つ合成IFC4。

    small.ifc(Rebro2026出力)の実際の形(実測: IfcStyledItem 4,053件は全てこの
    wrapper経由)。_resolve_surface_rgb/style_signature がラッパーを展開できず
    RGBを一度も返せていなかった不具合(color-task-4 追補)の再現・回帰テスト用。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coords = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    points = [f.create_entity("IfcCartesianPoint", Coordinates=c) for c in coords]
    face_indices = [(0, 2, 1), (0, 3, 2), (0, 1, 3), (1, 2, 3)]
    faces = []
    for idx in face_indices:
        loop = f.create_entity("IfcPolyLoop", Polygon=[points[i] for i in idx])
        bound = f.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        faces.append(f.create_entity("IfcFace", Bounds=[bound]))
    shell = f.create_entity("IfcClosedShell", CfsFaces=faces)
    brep = f.create_entity("IfcFacetedBrep", Outer=shell)

    style = _create_surface_style(f, rgb)
    wrapped = _wrap_in_style_assignment(f, style)
    _attach_styled_item(f, shell, wrapped)  # 子アイテム(shell)に、wrapper経由でスタイル。

    body_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Brep",
        Items=[brep],
    )
    element = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name="E1"
    )
    element.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[body_rep]
    )
    ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)

    return f


def build_two_elements_with_shared_type_ifc():
    """要素2件(IfcPipeSegment)が同じ型(IfcPipeSegmentType、RepresentationMaps
    を1つ持つ)を共有し、単一の IFCRELDEFINESBYTYPE で束ねられる合成IFC4を返す。

    carry-forward Phase F の事前調査(.superpowers/sdd/cff-probe-report.md
    Fact2)で確認した「1型に RepresentationMap がある」構成(実データで支配的、
    Fact4: small.ifc の IFC*TYPE系1,360件は1型1relの1:1が典型)を再現する。
    full-open(`type.unassign_type`)は related 全滅時に空になった
    IFCRELDEFINESBYTYPE 自身は消すが、RelatingType(型本体)と
    RepresentationMaps は積極的には消さない(psetの`remove_pset`とは非対称)。

    戻り値: (f, type_obj, elements)。elements は [IfcPipeSegment, IfcPipeSegment]。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=[(1, 2, 3)],
    )
    mapped_representation = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    identity = f.create_entity(
        "IfcAxis2Placement3D",
        Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    rep_map = f.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=mapped_representation,
    )

    type_obj = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcPipeSegmentType", name="PST1"
    )
    type_obj.RepresentationMaps = [rep_map]

    elements = [
        ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcPipeSegment", name=f"Pipe{i}"
        )
        for i in range(2)
    ]
    ifcopenshell.api.run(
        "type.assign_type",
        f,
        related_objects=elements,
        relating_type=type_obj,
        should_map_representations=True,
    )

    return f, type_obj, elements


def attach_layer_assignment(f, targets, name: str = "レイヤーA"):
    """targets(IfcRepresentation / IfcRepresentationItem のリスト)を
    AssignedItems に持つ IfcPresentationLayerAssignment を1つ作って返す。

    実務データでは1つの割当が多数要素の IfcShapeRepresentation を束ねる
    構成が観測されている(合成例。実レコードidではない)。複数
    targets を渡せるのはその形を再現するため。
    """
    return f.create_entity(
        "IfcPresentationLayerAssignment",
        Name=name,
        AssignedItems=list(targets),
    )
