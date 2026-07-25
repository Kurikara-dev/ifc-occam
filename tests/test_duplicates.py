import numpy as np
from ifc_occam.core.types import ShapeInfo
from ifc_occam.core.duplicates import find_duplicates

TET_V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
TET_F = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)

def _shape(sid, offset=(0, 0, 0), scale=1.0, vertex_order=None):
    v = TET_V * scale + np.array(offset, dtype=np.float64)
    if vertex_order is not None:
        v = v[vertex_order]
    return ShapeInfo(shape_id=sid, vertices=v, faces=TET_F.copy())

def test_translated_copies_form_one_group():
    shapes = {"a": _shape("a"), "b": _shape("b", offset=(100, 0, 0)),
              "c": _shape("c", offset=(0, 50, 0))}
    groups = find_duplicates(shapes)
    assert len(groups) == 1
    assert sorted(groups[0].shape_ids) == ["a", "b", "c"]
    assert groups[0].triangle_count == 4
    assert groups[0].savable_triangles == 8  # 4 × (3-1)

def test_different_scale_not_grouped():
    shapes = {"a": _shape("a"), "big": _shape("big", scale=2.0)}
    assert find_duplicates(shapes) == []

def test_singletons_excluded():
    shapes = {"a": _shape("a")}
    assert find_duplicates(shapes) == []

def test_within_tolerance_grouped():
    b = _shape("b")
    b.vertices[0] += 1e-9  # tol=1e-6 より十分小さい摂動
    shapes = {"a": _shape("a"), "b": b}
    groups = find_duplicates(shapes, tol=1e-6)
    assert len(groups) == 1

def test_groups_sorted_by_savable_triangles_desc():
    big_f = np.tile(TET_F, (10, 1))  # 40三角形の"重い"形状
    heavy = {f"h{i}": ShapeInfo(f"h{i}", TET_V + i * 10, big_f.copy())
             for i in range(2)}
    light = {f"l{i}": _shape(f"l{i}", offset=(i * 10, 0, 0)) for i in range(2)}
    groups = find_duplicates({**light, **heavy})
    assert groups[0].savable_triangles >= groups[1].savable_triangles


def test_vertex_permutation_with_remapped_faces_still_grouped():
    """頂点配列の並びが違っても(面も対応して並べ替えてあれば)同一群になる。"""
    perm = np.array([2, 0, 3, 1])
    # perm[new_idx] = old_idx なので、旧面インデックス old を
    # new = inverse_perm[old] に変換する
    inv = np.argsort(perm)
    b_faces = inv[TET_F]
    b = _shape("b", vertex_order=perm)
    b.faces = b_faces.astype(np.int64)
    shapes = {"a": _shape("a"), "b": b}
    groups = find_duplicates(shapes)
    assert len(groups) == 1
    assert sorted(groups[0].shape_ids) == ["a", "b"]


def test_different_triangulation_not_grouped():
    """同一頂点クラウドでも三角形分割(面接続)が異なれば重複と判定してはならない。"""
    quad_v = np.array(
        [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=np.float64
    )
    faces_diag_a = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    faces_diag_b = np.array([[0, 1, 3], [1, 2, 3]], dtype=np.int64)
    shapes = {
        "a": ShapeInfo(shape_id="a", vertices=quad_v.copy(), faces=faces_diag_a),
        "b": ShapeInfo(shape_id="b", vertices=quad_v.copy(), faces=faces_diag_b),
    }
    groups = find_duplicates(shapes)
    assert groups == []
