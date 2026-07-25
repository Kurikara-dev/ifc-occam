"""メッシュペイロードのシリアライズ (design.md §4.4 / phase2 plan §前提契約)。

バイナリ形式(little-endian):
    [uint32: json_len]
    [json_len bytes: UTF-8 JSON meta]
    [float32 x vertex_count*3: positions(世界座標、要素順に連結、要素間で頂点共有なし)]
    [uint32  x triangle_count*3: indices(連結後の頂点番号)]

meta:
    {"vertex_count": N, "triangle_count": M,
     "elements": [{"global_id", "ifc_class", "name", "layer",
                   "tri_start", "tri_count", "vertex_start", "vertex_count"}, ...]}
    elements は tri_start 昇順(要素の走査順そのまま)。
    頂点は要素間では共有しないが、要素内では溶接済み(vertex_count は
    tri_count*3 と一致しない)。要素単位の色変更は vertex_start/vertex_count
    の範囲を塗ること。
"""

import json
import struct

import numpy as np

from ifc_occam.core.types import ModelData, ShapeInfo


def world_vertices(shape: ShapeInfo, placement: np.ndarray) -> np.ndarray:
    """shape のローカル頂点に placement(4,4 同次変換)を適用し (n,3) 世界座標を返す。"""
    n = shape.vertices.shape[0]
    homogeneous = np.hstack([shape.vertices, np.ones((n, 1), dtype=np.float64)])
    world = (placement @ homogeneous.T).T
    return world[:, :3]


def build_mesh_payload(model: ModelData) -> bytes:
    """ModelData から単一マージメッシュのバイナリペイロードを構築する。

    幾何なし要素(shape_id=None)・shapes に無い shape_id・placement=None の要素は
    スキップし meta に含めない。頂点は要素間で共有しないが、要素内では
    溶接済み頂点を用いる。
    """
    positions_parts: list[np.ndarray] = []
    indices_parts: list[np.ndarray] = []
    elements_meta: list[dict] = []

    vertex_offset = 0
    tri_offset = 0

    for element in model.elements:
        if element.shape_id is None or element.placement is None:
            continue
        shape = model.shapes.get(element.shape_id)
        if shape is None:
            continue

        verts = world_vertices(shape, element.placement)
        faces = shape.faces + vertex_offset

        positions_parts.append(verts)
        indices_parts.append(faces)

        tri_count = int(shape.faces.shape[0])
        vertex_count = int(verts.shape[0])
        elements_meta.append(
            {
                "global_id": element.global_id,
                "ifc_class": element.ifc_class,
                "name": element.name,
                "layer": element.layer,
                "tri_start": tri_offset,
                "tri_count": tri_count,
                "vertex_start": vertex_offset,
                "vertex_count": vertex_count,
            }
        )

        vertex_offset += verts.shape[0]
        tri_offset += tri_count

    if positions_parts:
        positions = np.concatenate(positions_parts, axis=0).astype("<f4")
    else:
        positions = np.zeros((0, 3), dtype="<f4")

    if indices_parts:
        indices = np.concatenate(indices_parts, axis=0).astype("<u4")
    else:
        indices = np.zeros((0, 3), dtype="<u4")

    meta = {
        "vertex_count": int(positions.shape[0]),
        "triangle_count": int(indices.shape[0]),
        "elements": elements_meta,
    }
    meta_bytes = json.dumps(meta).encode("utf-8")
    header = struct.pack("<I", len(meta_bytes))

    return header + meta_bytes + positions.tobytes() + indices.tobytes()
