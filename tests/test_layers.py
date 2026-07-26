import numpy as np
import pytest
from ifc_occam.core.types import ShapeInfo, ElementInfo, ModelData
from ifc_occam.core.layers import aggregate_by_layer

def _shape(shape_id, n_tri):
    return ShapeInfo(shape_id=shape_id,
                     vertices=np.zeros((3, 3)),
                     faces=np.zeros((n_tri, 3), dtype=np.int64))

def _elem(gid, cls, shape_id, layer, mapped=False):
    return ElementInfo(global_id=gid, ifc_class=cls, name=None,
                       shape_id=shape_id, is_mapped=mapped,
                       representation_types=(), layer=layer)

def _model():
    shapes = {"bolt": _shape("bolt", 100), "wall": _shape("wall", 10)}
    elements = [
        _elem("B1", "IfcMechanicalFastener", "bolt", "Layer-A", mapped=True),
        _elem("B2", "IfcMechanicalFastener", "bolt", "Layer-A", mapped=True),
        _elem("B3", "IfcMechanicalFastener", "bolt", "Layer-A", mapped=True),
        _elem("W1", "IfcWall", "wall", "Layer-B"),
        _elem("P1", "IfcSpace", None, "Layer-B"),  # 幾何なし
        _elem("X1", "IfcWall", "wall", None),  # レイヤー未設定
    ]
    return ModelData(schema="IFC4", elements=elements, shapes=shapes)

def _model_with_missing_shape():
    """shapes に無い shape_id を持つ要素(不整合データ)を含むモデル。"""
    shapes = {"wall": _shape("wall", 10)}
    elements = [
        _elem("W1", "IfcWall", "wall", "Layer-A"),
        _elem("D1", "IfcDoor", "ghost-shape-id", "Layer-A"),  # shapes に存在しないキー
    ]
    return ModelData(schema="IFC4", elements=elements, shapes=shapes)

def _model_no_layers():
    """全要素が layer=None のモデル(レイヤーが1つも無いケース)。"""
    shapes = {"wall": _shape("wall", 10)}
    elements = [_elem("W1", "IfcWall", "wall", None)]
    return ModelData(schema="IFC4", elements=elements, shapes=shapes)


def test_strict_default_raises_keyerror_on_missing_shape():
    """既定(strict=True)では shapes に無い shape_id で KeyError を投げる。"""
    with pytest.raises(KeyError):
        aggregate_by_layer(_model_with_missing_shape())


def test_strict_false_skips_missing_shape_but_counts_element():
    """strict=False では該当要素の三角形集計をskipしつつ element_count には数える。"""
    stats = {s.layer: s for s in aggregate_by_layer(_model_with_missing_shape(), strict=False)}
    assert stats["Layer-A"].element_count == 2
    assert stats["Layer-A"].total_triangles == 10  # ghost-shape-id分はskip、wallの10のみ
    assert stats["Layer-A"].unique_shape_count == 1


def test_same_layer_elements_grouped_into_one_row():
    """(a) 同一レイヤーの要素が1行にまとまる。"""
    stats = {s.layer: s for s in aggregate_by_layer(_model())}
    assert stats["Layer-A"].element_count == 3


def test_unique_shape_count_does_not_double_count_shared_shape_id():
    """(b) unique_shape_count が同じ shape_id を重複して数えない。"""
    stats = {s.layer: s for s in aggregate_by_layer(_model())}
    assert stats["Layer-A"].unique_shape_count == 1  # boltはB1〜B3で共有


def test_total_triangles_is_sum_over_elements():
    """(c) total_triangles が要素ごとの三角形数の総和になる。"""
    stats = {s.layer: s for s in aggregate_by_layer(_model())}
    assert stats["Layer-A"].total_triangles == 300  # 100 tri × 3
    assert stats["Layer-B"].total_triangles == 10  # wallのみ(P1は幾何なし)


def test_layer_none_elements_excluded_from_result():
    """(d) layer が None の要素は結果に含まれない。除外された分が他レイヤーに
    紛れ込んでもいない(監督者裁定2: layerless_element_countの土台になる不変条件。
    レイヤー別集計の合計 + レイヤー未設定要素数 = モデル全体の要素数、が崩れないこと)。
    """
    stats = aggregate_by_layer(_model())
    assert None not in {s.layer for s in stats}
    total_aggregated = sum(s.element_count for s in stats)
    assert total_aggregated == 5  # モデル全体は6要素、layer=NoneのX1が1件
    assert len(_model().elements) - total_aggregated == 1


def test_sorted_descending_by_total_triangles():
    """(e) 三角形数の降順で返る。"""
    result = aggregate_by_layer(_model())
    totals = [s.total_triangles for s in result]
    assert totals == sorted(totals, reverse=True)


def test_sort_actually_reorders_and_is_not_just_insertion_order():
    """降順ソートが実際に効いていること(Task 3 レビュー Minor-1 の引き取り)。

    `_model()` は挿入順(Layer-A=300 → Layer-B=10)がたまたま降順と一致して
    いるため、実装から `sorted(...)` を削除しても上のテストは通ってしまう。
    ここでは**挿入順が昇順**になるフィクスチャを使い、並べ替えが起きなければ
    落ちるようにする。
    """
    shapes = {"small": _shape("small", 5), "big": _shape("big", 500)}
    elements = [
        _elem("S1", "IfcWall", "small", "Layer-Small"),  # 先に挿入=5三角形
        _elem("B1", "IfcWall", "big", "Layer-Big"),  # 後に挿入=500三角形
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    result = aggregate_by_layer(model)

    assert [s.layer for s in result] == ["Layer-Big", "Layer-Small"]
    assert [s.total_triangles for s in result] == [500, 5]


def test_strict_does_not_raise_for_a_layerless_element_with_a_broken_shape():
    """layer=None の要素は strict 契約の対象外(Task 3 レビュー Important-1)。

    集計そのものから除外されるため、shape_id が model.shapes に無くても
    strict=True で例外にならない。`aggregate_by_class` は ifc_class が
    非nullableなので全要素が検証を通り、この抜けが無い。両者の唯一の
    意味的な差なので、docstring の記述をテストでも固定しておく
    (将来この非対称を解消するなら、このテストが変更の合図になる)。
    """
    shapes = {"wall": _shape("wall", 10)}
    elements = [
        _elem("W1", "IfcWall", "wall", "Layer-A"),
        _elem("G1", "IfcDoor", "ghost-shape-id", None),  # layer未設定 かつ shape不整合
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    result = aggregate_by_layer(model, strict=True)  # 例外を投げない

    assert [s.layer for s in result] == ["Layer-A"]
    assert result[0].element_count == 1


def test_model_without_layers_returns_empty_list():
    """(f) レイヤーが1つも無いモデルでは空リスト。"""
    assert aggregate_by_layer(_model_no_layers()) == []


def test_geometry_less_element_counted_with_zero_triangles():
    stats = {s.layer: s for s in aggregate_by_layer(_model())}
    assert stats["Layer-B"].element_count == 2  # W1 + P1
    assert stats["Layer-B"].unique_shape_count == 1  # wallのみ(P1は幾何なし)
