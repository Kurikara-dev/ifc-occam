import numpy as np
import pytest
from ifc_occam.core.types import ShapeInfo, ElementInfo, ModelData
from ifc_occam.core.diagnose import aggregate_by_class

def _shape(shape_id, n_tri):
    return ShapeInfo(shape_id=shape_id,
                     vertices=np.zeros((3, 3)),
                     faces=np.zeros((n_tri, 3), dtype=np.int64))

def _elem(gid, cls, shape_id, mapped=False):
    return ElementInfo(global_id=gid, ifc_class=cls, name=None,
                       shape_id=shape_id, is_mapped=mapped,
                       representation_types=(), layer=None)

def _model():
    shapes = {"bolt": _shape("bolt", 100), "wall": _shape("wall", 10)}
    elements = [
        _elem("B1", "IfcMechanicalFastener", "bolt", mapped=True),
        _elem("B2", "IfcMechanicalFastener", "bolt", mapped=True),
        _elem("B3", "IfcMechanicalFastener", "bolt", mapped=True),
        _elem("W1", "IfcWall", "wall"),
        _elem("P1", "IfcSpace", None),  # 幾何なし
    ]
    return ModelData(schema="IFC4", elements=elements, shapes=shapes)

def _model_with_missing_shape():
    """shapes に無い shape_id を持つ要素(不整合データ)を含むモデル。"""
    shapes = {"wall": _shape("wall", 10)}
    elements = [
        _elem("W1", "IfcWall", "wall"),
        _elem("D1", "IfcDoor", "ghost-shape-id"),  # shapes に存在しないキー
    ]
    return ModelData(schema="IFC4", elements=elements, shapes=shapes)

def test_strict_default_raises_keyerror_on_missing_shape():
    """既定(strict=True)では shapes に無い shape_id で KeyError を投げる。"""
    with pytest.raises(KeyError):
        aggregate_by_class(_model_with_missing_shape())

def test_strict_false_skips_missing_shape_but_counts_element():
    """strict=False では該当要素の三角形集計をskipしつつ element_count には数える。"""
    stats = {s.ifc_class: s for s in aggregate_by_class(_model_with_missing_shape(), strict=False)}
    assert stats["IfcDoor"].element_count == 1
    assert stats["IfcDoor"].total_triangles == 0
    assert stats["IfcDoor"].unique_shape_count == 0
    # 他クラスは影響を受けない
    assert stats["IfcWall"].total_triangles == 10

def test_total_triangles_multiplies_by_instance_count():
    stats = {s.ifc_class: s for s in aggregate_by_class(_model())}
    assert stats["IfcMechanicalFastener"].total_triangles == 300  # 100 tri × 3
    assert stats["IfcMechanicalFastener"].element_count == 3
    assert stats["IfcMechanicalFastener"].unique_shape_count == 1
    assert stats["IfcMechanicalFastener"].mapped_count == 3
    assert stats["IfcMechanicalFastener"].max_single_shape_triangles == 100

def test_sorted_descending_by_total_triangles():
    result = aggregate_by_class(_model())
    totals = [s.total_triangles for s in result]
    assert totals == sorted(totals, reverse=True)

def test_geometry_less_element_counted_with_zero_triangles():
    stats = {s.ifc_class: s for s in aggregate_by_class(_model())}
    assert stats["IfcSpace"].element_count == 1
    assert stats["IfcSpace"].total_triangles == 0
    assert stats["IfcSpace"].unique_shape_count == 0
