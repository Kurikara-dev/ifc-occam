import json
import struct
import time

import numpy as np
import pytest

from ifc_occam.core.extract import extract_model
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo
from ifc_occam.server.meshpack import build_mesh_payload, world_vertices


def _translation_matrix(dx: float, dy: float, dz: float) -> np.ndarray:
    m = np.eye(4, dtype=np.float64)
    m[0, 3] = dx
    m[1, 3] = dy
    m[2, 3] = dz
    return m


def _triangle_shape(shape_id: str) -> ShapeInfo:
    vertices = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float64
    )
    faces = np.array([[0, 1, 2]], dtype=np.int64)
    return ShapeInfo(shape_id=shape_id, vertices=vertices, faces=faces)


def _make_element(
    global_id, ifc_class, name, layer, shape_id, placement, color=None
) -> ElementInfo:
    return ElementInfo(
        global_id=global_id,
        ifc_class=ifc_class,
        name=name,
        shape_id=shape_id,
        is_mapped=False,
        representation_types=("SweptSolid",),
        layer=layer,
        placement=placement,
        color=color,
    )


def _parse_payload(payload: bytes):
    (json_len,) = struct.unpack_from("<I", payload, 0)
    offset = 4
    meta = json.loads(payload[offset : offset + json_len].decode("utf-8"))
    offset += json_len

    vertex_count = meta["vertex_count"]
    triangle_count = meta["triangle_count"]

    positions = np.frombuffer(
        payload, dtype="<f4", count=vertex_count * 3, offset=offset
    ).reshape(vertex_count, 3)
    offset += vertex_count * 3 * 4

    indices = np.frombuffer(
        payload, dtype="<u4", count=triangle_count * 3, offset=offset
    ).reshape(triangle_count, 3)
    offset += triangle_count * 3 * 4

    assert offset == len(payload)
    return meta, positions, indices


def test_world_vertices_applies_translation():
    shape = _triangle_shape("s1")
    placement = _translation_matrix(10.0, 20.0, 30.0)

    world = world_vertices(shape, placement)

    assert world.shape == (3, 3)
    np.testing.assert_allclose(world[0], [10.0, 20.0, 30.0])
    np.testing.assert_allclose(world[1], [11.0, 20.0, 30.0])
    np.testing.assert_allclose(world[2], [10.0, 21.0, 30.0])


def test_world_vertices_applies_rotation_and_translation():
    shape = _triangle_shape("s1")
    # 90° rotation about Z: R = [[0,-1,0],[1,0,0],[0,0,1]], t = (10, 20, 30)
    placement = np.array(
        [
            [0.0, -1.0, 0.0, 10.0],
            [1.0, 0.0, 0.0, 20.0],
            [0.0, 0.0, 1.0, 30.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )

    extra_shape = ShapeInfo(
        shape_id="rot",
        vertices=np.array(
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64
        ),
        faces=np.array([[0, 1, 2]], dtype=np.int64),
    )

    world = world_vertices(extra_shape, placement)

    np.testing.assert_allclose(world[0], [10.0, 21.0, 30.0])
    np.testing.assert_allclose(world[1], [9.0, 20.0, 30.0])
    np.testing.assert_allclose(world[2], [10.0, 20.0, 31.0])


def test_build_mesh_payload_roundtrip_two_elements_sharing_shape():
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element(
            "GID1", "IfcWall", "Wall A", "L1", "s1", _translation_matrix(0, 0, 0)
        ),
        _make_element(
            "GID2", "IfcWall", "Wall B", "L1", "s1", _translation_matrix(5, 0, 0)
        ),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, positions, indices = _parse_payload(payload)

    assert meta["vertex_count"] == 6  # 3 verts x 2 elements, not shared
    assert meta["triangle_count"] == 2
    assert len(meta["elements"]) == 2

    e0, e1 = meta["elements"]
    assert e0["global_id"] == "GID1"
    assert e0["tri_start"] == 0
    assert e0["tri_count"] == 1
    assert e0["vertex_start"] == 0
    assert e0["vertex_count"] == 3
    assert e1["global_id"] == "GID2"
    assert e1["tri_start"] == 1
    assert e1["tri_count"] == 1
    assert e1["vertex_start"] == 3
    assert e1["vertex_count"] == 3

    total_vertex_count = meta["vertex_count"]
    for el in meta["elements"]:
        assert el["vertex_start"] + el["vertex_count"] <= total_vertex_count

    # contiguous and non-overlapping vertex ranges (sorted by vertex_start)
    sorted_elements = sorted(meta["elements"], key=lambda e: e["vertex_start"])
    expected_next_start = 0
    for el in sorted_elements:
        assert el["vertex_start"] == expected_next_start
        expected_next_start += el["vertex_count"]
    assert expected_next_start == total_vertex_count

    assert positions.dtype == np.float32
    assert indices.dtype == np.uint32
    assert indices.max() < meta["vertex_count"]

    # second element's indices must be offset by vertex_offset (not [[0,1,2],[0,1,2]])
    np.testing.assert_array_equal(indices, np.array([[0, 1, 2], [3, 4, 5]]))

    # second element's vertices are offset by translation, not welded with first
    np.testing.assert_allclose(positions[3], [5.0, 0.0, 0.0], atol=1e-5)


def _tetrahedron_shape(shape_id: str) -> ShapeInfo:
    """溶接済み(welded)頂点を持つ形状: 頂点4個・面4個 → vertex_count=4 だが
    tri_count*3=12。welding前提のコードが混入していないことを検出する回帰テスト用。
    """
    vertices = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    faces = np.array(
        [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64
    )
    return ShapeInfo(shape_id=shape_id, vertices=vertices, faces=faces)


def test_build_mesh_payload_welded_tetrahedron_two_elements_sharing_shape():
    """welded shape(頂点数 != 面数*3)を共有する2要素で、per-element
    vertex_start/vertex_count/tri_start/tri_count が正しく割り振られることを保証する。
    """
    shapes = {"tet": _tetrahedron_shape("tet")}
    elements = [
        _make_element(
            "GID1", "IfcWall", "Wall A", "L1", "tet", _translation_matrix(0, 0, 0)
        ),
        _make_element(
            "GID2", "IfcWall", "Wall B", "L1", "tet", _translation_matrix(5, 0, 0)
        ),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, positions, indices = _parse_payload(payload)

    assert meta["vertex_count"] == 8  # 4 verts x 2 elements, not shared
    assert meta["triangle_count"] == 8  # 4 faces x 2 elements
    assert len(meta["elements"]) == 2

    e0, e1 = meta["elements"]
    assert e0["global_id"] == "GID1"
    assert e0["vertex_start"] == 0
    assert e0["vertex_count"] == 4
    assert e0["tri_start"] == 0
    assert e0["tri_count"] == 4

    assert e1["global_id"] == "GID2"
    assert e1["vertex_start"] == 4
    assert e1["vertex_count"] == 4
    assert e1["tri_start"] == 4
    assert e1["tri_count"] == 4

    assert positions.shape == (8, 3)


def test_build_mesh_payload_roundtrip_preserves_japanese_name_and_layer():
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element(
            "GID1",
            "IfcPipeFitting",
            "配管継手",
            "設備",
            "s1",
            _translation_matrix(0, 0, 0),
        ),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, _, _ = _parse_payload(payload)

    e0 = meta["elements"][0]
    assert e0["name"] == "配管継手"
    assert e0["layer"] == "設備"


def test_build_mesh_payload_meta_fields():
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element(
            "GID1", "IfcWall", "Wall A", "L1", "s1", _translation_matrix(0, 0, 0)
        ),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, _, _ = _parse_payload(payload)

    e0 = meta["elements"][0]
    assert set(
        [
            "global_id",
            "ifc_class",
            "name",
            "layer",
            "tri_start",
            "tri_count",
            "vertex_start",
            "vertex_count",
        ]
    ) <= set(e0.keys())
    assert e0["ifc_class"] == "IfcWall"
    assert e0["name"] == "Wall A"
    assert e0["layer"] == "L1"


def test_build_mesh_payload_includes_color_when_present():
    """色付き要素の meta には color が [r,g,b] で入る。"""
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element(
            "GID1",
            "IfcWall",
            "Wall A",
            "L1",
            "s1",
            _translation_matrix(0, 0, 0),
            color=(0.0, 0.25, 1.0),
        ),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, _, _ = _parse_payload(payload)

    e0 = meta["elements"][0]
    assert e0["color"] == [0.0, 0.25, 1.0]


def test_build_mesh_payload_color_is_none_without_style():
    """色が無い要素(ElementInfo.color=None)の meta では color が null になる。"""
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element(
            "GID1", "IfcWall", "Wall A", "L1", "s1", _translation_matrix(0, 0, 0)
        ),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, _, _ = _parse_payload(payload)

    e0 = meta["elements"][0]
    assert e0["color"] is None


def test_build_mesh_payload_skips_element_without_shape_id():
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element("GID1", "IfcWall", "Wall A", None, "s1", _translation_matrix(0, 0, 0)),
        _make_element("GID2", "IfcOpeningElement", None, None, None, None),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, _, _ = _parse_payload(payload)

    gids = [e["global_id"] for e in meta["elements"]]
    assert gids == ["GID1"]


def test_build_mesh_payload_skips_element_with_dangling_shape_id():
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element("GID1", "IfcWall", "Wall A", None, "s1", _translation_matrix(0, 0, 0)),
        _make_element(
            "GID2", "IfcWall", "Wall B", None, "missing-shape", _translation_matrix(0, 0, 0)
        ),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, _, _ = _parse_payload(payload)

    gids = [e["global_id"] for e in meta["elements"]]
    assert gids == ["GID1"]


def test_build_mesh_payload_skips_element_with_none_placement():
    shapes = {"s1": _triangle_shape("s1")}
    elements = [
        _make_element("GID1", "IfcWall", "Wall A", None, "s1", _translation_matrix(0, 0, 0)),
        _make_element("GID2", "IfcWall", "Wall B", None, "s1", None),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    payload = build_mesh_payload(model)
    meta, _, _ = _parse_payload(payload)

    gids = [e["global_id"] for e in meta["elements"]]
    assert gids == ["GID1"]


def test_build_mesh_payload_empty_model_produces_valid_empty_payload():
    model = ModelData(schema="IFC4", elements=[], shapes={})

    payload = build_mesh_payload(model)
    meta, positions, indices = _parse_payload(payload)

    assert meta["vertex_count"] == 0
    assert meta["triangle_count"] == 0
    assert meta["elements"] == []
    assert positions.shape == (0, 3)
    assert indices.shape == (0, 3)


def test_build_mesh_payload_real_small_ifc(small_ifc_path):
    """small.ifc 実データで例外なく走り、サイズを確認する統合テスト。"""
    model, _ = extract_model(small_ifc_path)

    start = time.time()
    payload = build_mesh_payload(model)
    elapsed = time.time() - start

    meta, positions, indices = _parse_payload(payload)

    assert meta["vertex_count"] == positions.shape[0]
    assert meta["triangle_count"] == indices.shape[0]
    assert indices.max() < meta["vertex_count"] if meta["triangle_count"] else True

    size_mb = len(payload) / (1024 * 1024)
    print(f"\n[test_build_mesh_payload_real_small_ifc] payload size: {size_mb:.3f} MB, elapsed: {elapsed:.2f}s")
