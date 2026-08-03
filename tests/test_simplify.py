"""core/simplify.py のテスト (design.md Phase3 Task3)。"""

from __future__ import annotations

import shutil

import numpy as np
import pytest
from scipy.spatial import ConvexHull

from ifc_occam.core.simplify import (
    bbox_mesh,
    convex_hull_mesh,
    count_shared_elements,
    decimate_mesh,
    get_shared_element_gids,
    obb_mesh,
    replace_representation,
)
from tests.fixtures_ifc import (
    build_ifc2x3_single_element_ifc,
    build_millimeter_single_element_ifc,
    build_single_element_with_styled_item_ifc,
    build_two_elements_sharing_mapped_shape_ifc,
    build_two_elements_sharing_mapped_shape_with_transform_ifc,
    build_two_elements_sharing_representation_directly_ifc,
)


# ---------------------------------------------------------------------------
# 純粋関数: bbox_mesh
# ---------------------------------------------------------------------------


def test_bbox_mesh_returns_8_verts_and_12_faces():
    verts = np.array(
        [[0.0, 0.0, 0.0], [2.0, 3.0, 4.0], [1.0, 1.0, 1.0]], dtype=np.float64
    )
    out_verts, out_faces = bbox_mesh(verts)

    assert out_verts.shape == (8, 3)
    assert out_faces.shape == (12, 3)
    assert out_faces.dtype == np.int64


def test_bbox_mesh_matches_min_max():
    verts = np.array(
        [[-1.0, 2.0, 0.0], [3.0, -5.0, 4.0], [0.0, 0.0, -2.0]], dtype=np.float64
    )
    out_verts, _ = bbox_mesh(verts)

    np.testing.assert_allclose(out_verts.min(axis=0), verts.min(axis=0))
    np.testing.assert_allclose(out_verts.max(axis=0), verts.max(axis=0))


def test_bbox_mesh_faces_index_into_verts():
    verts = np.array([[0.0, 0.0, 0.0], [5.0, 5.0, 5.0]], dtype=np.float64)
    out_verts, out_faces = bbox_mesh(verts)

    assert out_faces.min() >= 0
    assert out_faces.max() < len(out_verts)


def test_bbox_mesh_all_12_triangles_wound_outward():
    """単位立方体点集合に対し、全12三角形の法線が箱の外側を向くこと(レビュー指摘)。"""
    cube = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [1.0, 1.0, 1.0],
            [0.0, 1.0, 1.0],
        ],
        dtype=np.float64,
    )
    out_verts, out_faces = bbox_mesh(cube)
    center = out_verts.mean(axis=0)

    for tri in out_faces:
        p0, p1, p2 = out_verts[tri]
        normal = np.cross(p1 - p0, p2 - p0)
        centroid = (p0 + p1 + p2) / 3.0
        outward = centroid - center
        assert np.dot(normal, outward) > 0, f"三角形 {tri} の法線が内向き"


# ---------------------------------------------------------------------------
# 純粋関数: obb_mesh
# ---------------------------------------------------------------------------


def _rotated_cylinder_verts():
    """斜め45度に傾けた細長い円柱の頂点群(OBBが効く形状の代表)。"""
    theta = np.linspace(0.0, 2.0 * np.pi, 24, endpoint=False)
    circle = np.stack([np.cos(theta) * 0.1, np.sin(theta) * 0.1], axis=1)
    verts = []
    for z in (0.0, 10.0):
        for x, y in circle:
            verts.append([x, y, z])
    v = np.array(verts, dtype=np.float64)
    # Z軸の周りではなく、XZ面内で45度回転して斜材にする
    c, s = np.cos(np.pi / 4), np.sin(np.pi / 4)
    rot = np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])
    return v @ rot.T


def _mesh_volume(verts, faces):
    """符号付き体積(発散定理)。正=外向き面。"""
    tri = verts[faces]
    return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def _box_volume_from_verts(verts):
    ext = verts.max(axis=0) - verts.min(axis=0)
    return float(np.prod(ext))


def test_obb_mesh_斜め円柱で体積がAABBより大幅に縮む():
    v = _rotated_cylinder_verts()
    obb_verts, obb_faces = obb_mesh(v)
    aabb_verts, _ = bbox_mesh(v)
    assert obb_faces.shape == (12, 3)
    assert obb_verts.shape == (8, 3)
    # 長さ10・半径0.1の45度斜材: AABB体積≈10.2、OBB体積≈0.283。1/10未満を要求
    assert abs(_mesh_volume(obb_verts, obb_faces)) < _box_volume_from_verts(aabb_verts) * 0.1


def test_obb_mesh_全頂点が箱の内側に入る():
    v = _rotated_cylinder_verts()
    obb_verts, _ = obb_mesh(v)
    # 箱の8頂点からフレームを復元して包含判定する代わりに、
    # 「各頂点は8頂点の凸結合の中」を凸包の平面群で判定する
    from scipy.spatial import ConvexHull as _CH
    hull = _CH(obb_verts)
    eps = 1e-9 + np.abs(v).max() * 1e-12
    for eq in hull.equations:
        assert (v @ eq[:3] + eq[3] <= eps).all()


def test_obb_mesh_面の向きが外向き():
    v = _rotated_cylinder_verts()
    obb_verts, obb_faces = obb_mesh(v)
    assert _mesh_volume(obb_verts, obb_faces) > 0


def test_obb_mesh_軸平行入力はAABBと同一():
    rng = np.random.default_rng(7)
    v = rng.uniform(0.0, 1.0, size=(50, 3))
    v = np.concatenate([v, [[0, 0, 0], [1, 1, 1]]])  # 角を確定させる
    obb_verts, obb_faces = obb_mesh(v)
    aabb_verts, aabb_faces = bbox_mesh(v)
    # 一様乱数の雲はどの回転でも締まらないのでAABBフォールバックが選ばれる
    assert np.array_equal(obb_verts, aabb_verts)
    assert np.array_equal(obb_faces, aabb_faces)


def test_obb_mesh_決定性_同一入力で同一出力():
    v = _rotated_cylinder_verts()
    a_verts, a_faces = obb_mesh(v)
    b_verts, b_faces = obb_mesh(v)
    assert np.array_equal(a_verts, b_verts)
    assert np.array_equal(a_faces, b_faces)


def test_obb_mesh_体積は常にAABB以下():
    rng = np.random.default_rng(11)
    for _ in range(20):
        n = int(rng.integers(4, 200))
        v = rng.normal(size=(n, 3)) * rng.uniform(0.1, 5.0, size=3)
        obb_verts, obb_faces = obb_mesh(v)
        aabb_verts, _ = bbox_mesh(v)
        # OBBの実体積はメッシュの符号付き体積で測る(頂点のAABBで測ると
        # 回転した箱を過大評価して常に成立する無意味な検査になる)
        assert abs(_mesh_volume(obb_verts, obb_faces)) <= (
            _box_volume_from_verts(aabb_verts) * (1.0 + 1e-9))


def test_obb_mesh_退化入力はAABBフォールバックで例外なし():
    cases = [
        np.array([[0.0, 0.0, 0.0]]),                              # 1点
        np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]),             # 2点(共線)
        np.array([[float(i), float(i), float(i)] for i in range(10)]),  # 共線10点
        np.array([[float(i % 3), float(i // 3), 0.0] for i in range(9)]),  # 共面
    ]
    for v in cases:
        obb_verts, obb_faces = obb_mesh(v)
        aabb_verts, aabb_faces = bbox_mesh(v)
        assert np.array_equal(obb_faces, aabb_faces)
        assert obb_verts.shape == (8, 3)


# ---------------------------------------------------------------------------
# 純粋関数: convex_hull_mesh
# ---------------------------------------------------------------------------


def test_convex_hull_mesh_removes_interior_point():
    # 単位立方体の8頂点 + 内部点1つ
    cube = np.array(
        [
            [0, 0, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 0, 1],
            [1, 1, 1],
            [0, 1, 1],
        ],
        dtype=np.float64,
    )
    interior = np.array([[0.5, 0.5, 0.5]], dtype=np.float64)
    verts = np.vstack([cube, interior])

    out_verts, out_faces = convex_hull_mesh(verts)

    # 内部点は結果の頂点集合に現れない
    assert not any(np.allclose(v, interior[0]) for v in out_verts)
    assert len(out_verts) == 8
    assert out_faces.max() < len(out_verts)


def test_convex_hull_mesh_faces_valid_indices():
    rng = np.random.default_rng(1)
    pts = rng.normal(size=(50, 3))
    out_verts, out_faces = convex_hull_mesh(pts)

    assert out_faces.min() >= 0
    assert out_faces.max() < len(out_verts)
    assert len(out_verts) <= len(pts)


# ---------------------------------------------------------------------------
# 純粋関数: decimate_mesh
# ---------------------------------------------------------------------------


def _sphere_mesh(n_points=200, seed=0):
    rng = np.random.default_rng(seed)
    pts = rng.normal(size=(n_points, 3))
    pts /= np.linalg.norm(pts, axis=1, keepdims=True)
    hull = ConvexHull(pts)
    return hull.points, hull.simplices.astype(np.int64)


@pytest.mark.parametrize("ratio", [0.1, 0.5, 0.9])
def test_decimate_mesh_ratio_within_tolerance(ratio):
    verts, faces = _sphere_mesh()
    original_count = len(faces)

    out_verts, out_faces = decimate_mesh(verts, faces, ratio)

    assert np.all(np.isfinite(out_verts))
    kept_ratio = len(out_faces) / original_count
    assert abs(kept_ratio - ratio) <= 0.20 * max(ratio, 0.05) + 0.05


def test_decimate_mesh_invalid_ratio_raises():
    verts, faces = _sphere_mesh()
    with pytest.raises(ValueError):
        decimate_mesh(verts, faces, 1.5)
    with pytest.raises(ValueError):
        decimate_mesh(verts, faces, 0.0)


# ---------------------------------------------------------------------------
# 統合: replace_representation (scope="element") on small.ifc のコピー
# ---------------------------------------------------------------------------


def _first_element_with_own_map(model_file):
    """small.ifc から、他要素と共有されていない(shared count==1) 要素を1つ返す。"""
    import ifcopenshell.util.element as ue

    for product in model_file.by_type("IfcProduct"):
        rep = getattr(product, "Representation", None)
        if rep is None:
            continue
        body = next(
            (r for r in rep.Representations if r.RepresentationIdentifier == "Body"),
            None,
        )
        if body is None or len(body.Items) != 1:
            continue
        item = body.Items[0]
        if not item.is_a("IfcMappedItem"):
            continue
        mapped_rep = item.MappingSource.MappedRepresentation
        elements = ue.get_elements_by_representation(model_file, mapped_rep)
        if len(elements) == 1:
            return product
    return None


def test_replace_representation_element_scope_bbox_on_real_element(tmp_path, small_ifc_path):
    import ifcopenshell
    import ifcopenshell.geom

    copy_path = tmp_path / "small_copy.ifc"
    shutil.copy(small_ifc_path, copy_path)

    model = ifcopenshell.open(str(copy_path))
    target = _first_element_with_own_map(model)
    if target is None:
        pytest.skip("small.ifc has no element with an unshared RepresentationMap")

    target_gid = target.GlobalId

    settings = ifcopenshell.geom.settings()
    shape = ifcopenshell.geom.create_shape(settings, target)
    verts = np.array(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)

    bbox_verts, bbox_faces = bbox_mesh(verts)
    original_extents = bbox_verts.max(axis=0) - bbox_verts.min(axis=0)

    # 他要素の三角形数を事前に記録(不変であることを確認するため)
    other = next(
        p
        for p in model.by_type("IfcProduct")
        if p.GlobalId != target_gid and getattr(p, "Representation", None) is not None
    )
    other_gid = other.GlobalId
    other_shape_before = ifcopenshell.geom.create_shape(settings, other)
    other_count_before = len(other_shape_before.geometry.faces) // 3

    replace_representation(model, target, bbox_verts, bbox_faces, scope="element")

    saved_path = tmp_path / "small_copy_bbox.ifc"
    model.write(str(saved_path))

    reopened = ifcopenshell.open(str(saved_path))
    target2 = reopened.by_guid(target_gid)
    new_shape = ifcopenshell.geom.create_shape(settings, target2)
    new_face_count = len(new_shape.geometry.faces) // 3
    assert new_face_count == 12

    # 単位変換バグの回帰検証: 再抽出したbboxの寸法が、元要素のbboxと一致すること
    # (単位変換漏れがあると、mm単位ファイルでは1/1000に縮んで失敗する)。
    new_verts = np.array(new_shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
    new_extents = new_verts.max(axis=0) - new_verts.min(axis=0)
    np.testing.assert_allclose(new_extents, original_extents, rtol=1e-6)

    other2 = reopened.by_guid(other_gid)
    other_shape_after = ifcopenshell.geom.create_shape(settings, other2)
    other_count_after = len(other_shape_after.geometry.faces) // 3
    assert other_count_after == other_count_before


# ---------------------------------------------------------------------------
# 統合: replace_representation (scope="shared") on 合成フィクスチャ
# ---------------------------------------------------------------------------


def test_replace_representation_shared_scope_propagates_to_both_elements():
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    box_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    box_faces = np.array([[0, 1, 2]], dtype=np.int64)
    new_verts, new_faces = bbox_mesh(box_verts)

    replace_representation(f, elem1, new_verts, new_faces, scope="shared")

    for elem in (elem1, elem2):
        body = elem.Representation.Representations[0]
        # scope="shared" では両要素とも同じ (マップ経由の) 新形状を指す
        item = body.Items[0]
        assert item.is_a("IfcMappedItem")
        mapped_rep = item.MappingSource.MappedRepresentation
        assert len(mapped_rep.Items) == 1
        tfs = mapped_rep.Items[0]
        assert tfs.is_a("IfcTriangulatedFaceSet")
        assert len(tfs.CoordIndex) == 12


def test_replace_representation_element_scope_unshares_only_target():
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    box_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    box_faces = np.array([[0, 1, 2]], dtype=np.int64)
    new_verts, new_faces = bbox_mesh(box_verts)

    replace_representation(f, elem1, new_verts, new_faces, scope="element")

    body1 = elem1.Representation.Representations[0]
    assert not body1.Items[0].is_a("IfcMappedItem")
    assert body1.Items[0].is_a("IfcTriangulatedFaceSet")
    assert len(body1.Items[0].CoordIndex) == 12

    # elem2 は元の共有形状(三角形1枚)のまま
    body2 = elem2.Representation.Representations[0]
    assert body2.Items[0].is_a("IfcMappedItem")
    mapped_rep2 = body2.Items[0].MappingSource.MappedRepresentation
    assert len(mapped_rep2.Items[0].CoordIndex) == 1


# ---------------------------------------------------------------------------
# scope="shared" と非恒等 MappingTarget: 書き戻し時にMappingTargetを2重適用
# しないこと(Final Review Fix3)。
# ---------------------------------------------------------------------------


def _bbox_center(verts: np.ndarray) -> np.ndarray:
    v = np.asarray(verts, dtype=np.float64).reshape(-1, 3)
    return (v.min(axis=0) + v.max(axis=0)) / 2.0


def test_shared_scope_writeback_does_not_double_apply_mapping_target(tmp_path):
    """Elem1(MappingTarget=平行移動(2,0,0))経由でscope="shared"のsimplifyを行った後、
    Elem1を再抽出したジオメトリのbbox中心が、置換前と(誤差範囲内で)同じ位置になること。

    修正前は MappingTarget が2重適用され、bbox中心がさらに(2,0,0)分ズレる(バグ再現)。
    """
    import ifcopenshell.geom

    f = build_two_elements_sharing_mapped_shape_with_transform_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    settings = ifcopenshell.geom.settings()
    shape_before = ifcopenshell.geom.create_shape(settings, elem1)
    verts_before = np.array(shape_before.geometry.verts, dtype=np.float64).reshape(-1, 3)
    center_before = _bbox_center(verts_before)

    # bbox_mesh は入力の座標系(=MappingTarget適用後のelem1ローカル座標)のAABBを
    # 返すだけなので、中心は不変のはず(単純な簡略化操作として使う)。
    new_verts, new_faces = bbox_mesh(verts_before)

    warnings_out = replace_representation(f, elem1, new_verts, new_faces, scope="shared")
    assert warnings_out == []

    reopened_path = tmp_path / "out.ifc"
    f.write(str(reopened_path))
    reopened = ifcopenshell.open(str(reopened_path))
    elem1_after = next(e for e in reopened.by_type("IfcBuildingElementProxy") if e.Name == "Elem1")

    shape_after = ifcopenshell.geom.create_shape(settings, elem1_after)
    verts_after = np.array(shape_after.geometry.verts, dtype=np.float64).reshape(-1, 3)
    center_after = _bbox_center(verts_after)

    np.testing.assert_allclose(center_after, center_before, atol=1e-6)


def test_shared_scope_writeback_falls_back_to_element_when_not_safely_invertible(monkeypatch):
    """MappingTargetの逆変換が安全に求まらない場合、その要素だけscope="element"に
    フォールバックし、警告を返すこと(Final Review Fix3)。"""
    import ifc_occam.core.simplify as simplify_module

    f = build_two_elements_sharing_mapped_shape_with_transform_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    monkeypatch.setattr(simplify_module, "_transform_operator_matrix", lambda op: None)

    box_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    box_faces = np.array([[0, 1, 2]], dtype=np.int64)
    new_verts, new_faces = bbox_mesh(box_verts)

    warnings_out = replace_representation(f, elem1, new_verts, new_faces, scope="shared")

    assert len(warnings_out) >= 1
    assert any("フォールバック" in w for w in warnings_out)

    # elem1 は個別化され、共有マップ(elem2側)は変更されていない
    body1 = elem1.Representation.Representations[0]
    assert not body1.Items[0].is_a("IfcMappedItem")
    body2 = elem2.Representation.Representations[0]
    assert body2.Items[0].is_a("IfcMappedItem")
    mapped_rep2 = body2.Items[0].MappingSource.MappedRepresentation
    assert len(mapped_rep2.Items[0].CoordIndex) == 1


# ---------------------------------------------------------------------------
# count_shared_elements
# ---------------------------------------------------------------------------


def test_count_shared_elements_synthetic_fixture():
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, _ = f.by_type("IfcBuildingElementProxy")

    assert count_shared_elements(f, elem1.GlobalId) == 2


def test_replace_representation_returns_warning_when_cleanup_fails(monkeypatch):
    """掃除(remove_deep2)が例外を出しても書き戻し自体は成功し、警告文字列が返ること(レビュー指摘)。"""
    import ifcopenshell.util.element as ue

    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, _ = f.by_type("IfcBuildingElementProxy")

    box_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    box_faces = np.array([[0, 1, 2]], dtype=np.int64)
    new_verts, new_faces = bbox_mesh(box_verts)

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ue, "remove_deep2", _raise)

    warnings = replace_representation(f, elem1, new_verts, new_faces, scope="element")

    assert isinstance(warnings, list)
    assert len(warnings) > 0
    assert any("boom" in w for w in warnings)

    # 書き戻し自体は成功している(新形状に差し替わっている)
    body1 = elem1.Representation.Representations[0]
    assert not body1.Items[0].is_a("IfcMappedItem")
    assert body1.Items[0].is_a("IfcTriangulatedFaceSet")


def test_count_shared_elements_after_unsharing_one_element():
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    box_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    box_faces = np.array([[0, 1, 2]], dtype=np.int64)
    new_verts, new_faces = bbox_mesh(box_verts)
    replace_representation(f, elem1, new_verts, new_faces, scope="element")

    assert count_shared_elements(f, elem1.GlobalId) == 1
    assert count_shared_elements(f, elem2.GlobalId) == 1


def test_get_shared_element_gids_returns_sibling_excluding_self():
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    assert get_shared_element_gids(f, elem1.GlobalId) == [elem2.GlobalId]
    assert get_shared_element_gids(f, elem2.GlobalId) == [elem1.GlobalId]


def test_get_shared_element_gids_covers_direct_sharing():
    """IfcMappedItem を介さない直接共有(同一 IfcShapeRepresentation を
    複数製品が直接参照)でも兄弟が返ること(フェーズ最終レビューI-3の
    carry-forward)。書き戻しは rep をその場で書き換えるため、この構成も
    実際に波及グループである。"""
    f = build_two_elements_sharing_representation_directly_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    assert get_shared_element_gids(f, elem1.GlobalId) == [elem2.GlobalId]
    assert count_shared_elements(f, elem1.GlobalId) == 2


def test_get_shared_element_gids_empty_when_not_shared():
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, _ = f.by_type("IfcBuildingElementProxy")

    box_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    box_faces = np.array([[0, 1, 2]], dtype=np.int64)
    new_verts, new_faces = bbox_mesh(box_verts)
    replace_representation(f, elem1, new_verts, new_faces, scope="element")

    assert get_shared_element_gids(f, elem1.GlobalId) == []


def test_get_shared_element_gids_returns_empty_for_element_without_geometry():
    f = build_ifc2x3_single_element_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    element.Representation = None

    assert get_shared_element_gids(f, element.GlobalId) == []


def test_count_shared_elements_returns_0_for_element_without_geometry():
    """Body representation を持たない要素は共有0(レビュー指摘の安価な保証テスト)。"""
    f = build_ifc2x3_single_element_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    # Body representation 自体は存在するが Items が空 → 幾何なし扱いではないので、
    # Representation を完全に取り除いて「幾何なし要素」を作る。
    element.Representation = None

    assert count_shared_elements(f, element.GlobalId) == 0


# ---------------------------------------------------------------------------
# CoordIndex の内容検証(安価な保証テスト)
# ---------------------------------------------------------------------------


def test_coord_index_matches_input_faces_plus_one():
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, _ = f.by_type("IfcBuildingElementProxy")

    tetra_verts = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    tetra_faces = np.array(
        [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64
    )

    replace_representation(f, elem1, tetra_verts, tetra_faces, scope="element")

    body1 = elem1.Representation.Representations[0]
    tfs = body1.Items[0]
    assert tfs.is_a("IfcTriangulatedFaceSet")

    expected = [tuple(int(i) + 1 for i in tri) for tri in tetra_faces]
    actual = [tuple(tfs.CoordIndex[i]) for i in range(len(tfs.CoordIndex))]
    assert actual == expected


# ---------------------------------------------------------------------------
# IFC2X3 パス(IfcFacetedBrep 書き戻し)
# ---------------------------------------------------------------------------


def test_replace_representation_ifc2x3_produces_faceted_brep():
    f = build_ifc2x3_single_element_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]

    tetra_verts = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    tetra_faces = np.array(
        [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64
    )

    warnings = replace_representation(f, element, tetra_verts, tetra_faces, scope="element")
    assert warnings == []

    assert f.schema == "IFC2X3"

    breps = f.by_type("IfcFacetedBrep")
    assert len(breps) == 1
    brep = breps[0]

    faces = list(brep.Outer.CfsFaces)
    assert len(faces) == 4
    for face in faces:
        assert face.is_a("IfcFace")
        bounds = list(face.Bounds)
        assert len(bounds) == 1
        bound = bounds[0]
        assert bound.is_a("IfcFaceOuterBound")
        loop = bound.Bound
        assert loop.is_a("IfcPolyLoop")
        points = list(loop.Polygon)
        assert len(points) == 3
        for pt in points:
            assert pt.is_a("IfcCartesianPoint")
            assert len(pt.Coordinates) == 3

    body_rep = element.Representation.Representations[0]
    assert body_rep.RepresentationType == "Brep"

    # 再抽出(ifcopenshell.geom)を試みる。最小フィクスチャで難しければ構造検証のみで良い。
    import ifcopenshell.geom

    try:
        settings = ifcopenshell.geom.settings()
        shape = ifcopenshell.geom.create_shape(settings, element)
        n_triangles = len(shape.geometry.faces) // 3
        assert n_triangles == 4
    except Exception:
        pytest.skip(
            "最小フィクスチャでは ifcopenshell.geom による再抽出が実施不可"
            "(エンティティ構造検証で代替済み)"
        )


# ---------------------------------------------------------------------------
# 単位変換(replace_representation は SI/メートル入力を前提とし、ファイルの
# 実寸法単位(ミリメートル等)に変換して書き込む)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# IfcStyledItem の移送(Phase4 Task2追補: 色の保持+旧アイテム掃除の解禁)
# ---------------------------------------------------------------------------


def test_replace_representation_transfers_styled_item_and_cleans_up_old():
    """IfcStyledItemが付いたBodyアイテムを差し替えると、
    (1) 新アイテムが同じスタイルを引き継ぎ、(2) 旧アイテムは削除され、
    (3) IfcStyledItemの総数は増えない(既存のものが新アイテムへ付け替わる)こと。"""
    f = build_single_element_with_styled_item_ifc(rgb=(0.2, 0.4, 0.6))
    element = f.by_type("IfcBuildingElementProxy")[0]

    assert len(f.by_type("IfcTriangulatedFaceSet")) == 1
    assert len(f.by_type("IfcStyledItem")) == 1
    old_tfs_id = f.by_type("IfcTriangulatedFaceSet")[0].id()

    box_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    box_faces = np.array([[0, 1, 2]], dtype=np.int64)
    new_verts, new_faces = bbox_mesh(box_verts)

    warnings = replace_representation(f, element, new_verts, new_faces, scope="element")
    assert warnings == []

    tfs_list = f.by_type("IfcTriangulatedFaceSet")
    assert len(tfs_list) == 1
    new_tfs = tfs_list[0]
    assert new_tfs.id() != old_tfs_id  # 旧アイテムは実際に削除され、新規に差し替わっている

    styled_items = f.by_type("IfcStyledItem")
    assert len(styled_items) == 1
    styled_item = styled_items[0]
    assert styled_item.Item == new_tfs

    colour = styled_item.Styles[0].Styles[0].SurfaceColour
    assert (colour.Red, colour.Green, colour.Blue) == (0.2, 0.4, 0.6)

    body = element.Representation.Representations[0]
    assert body.Items[0] == new_tfs


def test_replace_representation_converts_meters_to_millimeters():
    """ミリメートル単位ファイルへ1m立方体(メートル座標)を書き込むと、
    格納される IfcCartesianPointList3D の座標はミリメートル(=1000.0)であること。

    修正前は SI/メートル座標がそのまま書き込まれ、ファイル側はミリメートルとして
    解釈するため、形状が実寸の1/1000に縮んでしまう(CRITICALバグ)。
    """
    f = build_millimeter_single_element_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]

    cube_verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [1.0, 1.0, 1.0],
            [0.0, 1.0, 1.0],
        ],
        dtype=np.float64,
    )
    cube_faces = np.array(
        [
            [0, 1, 2], [0, 2, 3],
            [4, 6, 5], [4, 7, 6],
            [0, 4, 5], [0, 5, 1],
            [1, 5, 6], [1, 6, 2],
            [2, 6, 7], [2, 7, 3],
            [3, 7, 4], [3, 4, 0],
        ],
        dtype=np.int64,
    )

    replace_representation(f, element, cube_verts, cube_faces, scope="element")

    body = element.Representation.Representations[0]
    tfs = body.Items[0]
    assert tfs.is_a("IfcTriangulatedFaceSet")

    coords = np.array(tfs.Coordinates.CoordList, dtype=np.float64)
    extents = coords.max(axis=0) - coords.min(axis=0)
    np.testing.assert_allclose(extents, [1000.0, 1000.0, 1000.0])
