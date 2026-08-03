"""core/advisor.py(簡略化手法の適正判定)のTDD (OBB+適正判定フェーズ Task4)。

advise_simplify: 4規則それぞれ「発火する値/しない値/None」の3ケースと文言のliteral一致。
sample_class_geometry_metrics: 合成ModelData(ShapeInfoを直接組む)で
細長い斜め形状→obb_volume_ratio<0.5、立方体→hull_triangle_ratio==1.0(12/12)を確認。
"""

from __future__ import annotations

import numpy as np

from ifc_occam.core.advisor import advise_simplify, sample_class_geometry_metrics
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo

# 直方体(8頂点12面)の面配列。ifc_occam.core.simplify._BBOX_LOCAL_FACES と同一トポロジ。
_BOX_FACES = np.array(
    [
        [0, 2, 1], [0, 3, 2],
        [4, 5, 6], [4, 6, 7],
        [0, 5, 4], [0, 1, 5],
        [1, 6, 5], [1, 2, 6],
        [2, 7, 6], [2, 3, 7],
        [3, 4, 7], [3, 0, 4],
    ],
    dtype=np.int64,
)


def _box_verts(extent: tuple[float, float, float]) -> np.ndarray:
    x, y, z = extent
    return np.array(
        [
            [0, 0, 0], [x, 0, 0], [x, y, 0], [0, y, 0],
            [0, 0, z], [x, 0, z], [x, y, z], [0, y, z],
        ],
        dtype=np.float64,
    )


# ---------------------------------------------------------------------------
# advise_simplify: 4規則
# ---------------------------------------------------------------------------


# --- 規則1: decimateの低密度警告 ---


def test_decimate_low_density_fires_below_threshold_with_literal_message():
    msgs = advise_simplify("decimate", avg_triangles_per_shape=154.0)
    assert msgs == [
        "平均154三角形/形状の粗いメッシュのため、間引きは指定した率まで削れないことがあります。"
    ]


def test_decimate_low_density_does_not_fire_above_threshold():
    assert advise_simplify("decimate", avg_triangles_per_shape=5000.0) == []


def test_decimate_low_density_does_not_fire_when_none():
    assert advise_simplify("decimate", avg_triangles_per_shape=None) == []


def test_decimate_low_density_rule_is_restricted_to_decimate_method():
    """avg_triangles_per_shape規則はmethod=="decimate"専用。他手法では発火しない。"""
    assert advise_simplify("bbox", avg_triangles_per_shape=10.0) == []
    assert advise_simplify("convex_hull", avg_triangles_per_shape=10.0) == []
    assert advise_simplify("obb", avg_triangles_per_shape=10.0) == []


# --- 規則2: 三角形化書き戻し警告(decimate, convex_hull) ---


def test_triangle_source_other_fires_with_literal_message_for_decimate():
    msgs = advise_simplify("decimate", triangle_source="other")
    assert msgs == [
        "三角形化して書き戻すため、三角形数が減ってもファイルサイズは増えることがあります。"
        "サイズ削減が目的なら OBB/bbox か削除を検討してください。"
    ]


def test_triangle_source_other_fires_with_literal_message_for_convex_hull():
    msgs = advise_simplify("convex_hull", triangle_source="other")
    assert msgs == [
        "三角形化して書き戻すため、三角形数が減ってもファイルサイズは増えることがあります。"
        "サイズ削減が目的なら OBB/bbox か削除を検討してください。"
    ]


def test_triangle_source_tessellation_does_not_fire():
    assert advise_simplify("decimate", triangle_source="tessellation") == []
    assert advise_simplify("convex_hull", triangle_source="tessellation") == []


def test_triangle_source_none_does_not_fire():
    assert advise_simplify("decimate", triangle_source=None) == []
    assert advise_simplify("convex_hull", triangle_source=None) == []


def test_triangle_source_rule_is_restricted_to_decimate_and_convex_hull():
    assert advise_simplify("bbox", triangle_source="other") == []
    assert advise_simplify("obb", triangle_source="other") == []


# --- 規則3: ほぼ凸警告(convex_hull) ---


def test_hull_near_convex_fires_at_and_above_threshold_with_literal_message():
    msgs = advise_simplify("convex_hull", hull_triangle_ratio=0.63)
    assert msgs == [
        "ほぼ凸の形状です(サンプル判定: 凸包後も三角形の63%が残ります)。"
        "凸包の削減効果は小さい見込みです。"
    ]
    # 境界値ちょうどでも発火する(>=)
    assert advise_simplify("convex_hull", hull_triangle_ratio=0.6) != []


def test_hull_near_convex_does_not_fire_below_threshold():
    assert advise_simplify("convex_hull", hull_triangle_ratio=0.59) == []


def test_hull_near_convex_does_not_fire_when_none():
    assert advise_simplify("convex_hull", hull_triangle_ratio=None) == []


def test_hull_near_convex_rule_is_restricted_to_convex_hull_method():
    assert advise_simplify("bbox", hull_triangle_ratio=0.9) == []
    assert advise_simplify("decimate", hull_triangle_ratio=0.9) == []
    assert advise_simplify("obb", hull_triangle_ratio=0.9) == []


# --- 規則4: OBB推奨(bbox) ---


def test_obb_recommend_fires_at_and_below_threshold_with_literal_message():
    msgs = advise_simplify("bbox", obb_volume_ratio=0.143)
    assert msgs == [
        "部材が座標軸に対して斜めです(サンプル判定: OBBなら箱の体積が平均14%に縮みます)。"
        "OBB(向き付きbbox)の方が形に沿います。"
    ]
    # 境界値ちょうどでも発火する(<=)
    assert advise_simplify("bbox", obb_volume_ratio=0.5) != []


def test_obb_recommend_does_not_fire_above_threshold():
    assert advise_simplify("bbox", obb_volume_ratio=0.51) == []


def test_obb_recommend_does_not_fire_when_none():
    assert advise_simplify("bbox", obb_volume_ratio=None) == []


def test_obb_recommend_rule_is_restricted_to_bbox_method():
    assert advise_simplify("convex_hull", obb_volume_ratio=0.1) == []
    assert advise_simplify("decimate", obb_volume_ratio=0.1) == []
    assert advise_simplify("obb", obb_volume_ratio=0.1) == []


# --- 複数規則が同時に発火する場合(decimateはavg + triangle_source両方対象) ---


def test_decimate_can_fire_both_density_and_triangle_source_rules_together():
    msgs = advise_simplify(
        "decimate", avg_triangles_per_shape=100.0, triangle_source="other"
    )
    assert len(msgs) == 2


# ---------------------------------------------------------------------------
# sample_class_geometry_metrics
# ---------------------------------------------------------------------------


def _rotation_y(radians: float) -> np.ndarray:
    c, s = np.cos(radians), np.sin(radians)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def test_sample_class_geometry_metrics_thin_diagonal_box_has_low_obb_volume_ratio():
    """細長い箱を45度傾けたクラス: OBBは元の向きに沿うため体積がAABBの半分未満に縮む。"""
    verts = _box_verts((0.1, 0.1, 10.0))
    centered = verts - verts.mean(axis=0)
    rotated = centered @ _rotation_y(np.pi / 4).T + verts.mean(axis=0)

    shapes = {"s1": ShapeInfo("s1", rotated, _BOX_FACES)}
    elements = [
        ElementInfo("G1", "IFCMEMBER", "Brace-1", "s1", False, ("Tessellation",), None),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    metrics = sample_class_geometry_metrics(model)
    assert "IFCMEMBER" in metrics
    assert metrics["IFCMEMBER"]["obb_volume_ratio"] < 0.5


def test_sample_class_geometry_metrics_cube_has_hull_triangle_ratio_near_one():
    """軸平行の立方体: 凸包後も12面のまま(12/12=1.0)。"""
    verts = _box_verts((1.0, 1.0, 1.0))
    shapes = {"s1": ShapeInfo("s1", verts, _BOX_FACES)}
    elements = [
        ElementInfo("G1", "IFCWALL", "Wall-1", "s1", False, ("Tessellation",), None),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    metrics = sample_class_geometry_metrics(model)
    assert metrics["IFCWALL"]["hull_triangle_ratio"] == 1.0


def test_sample_class_geometry_metrics_same_shape_referenced_twice_is_measured_once():
    """同一形状が同クラス複数要素から参照されても1回だけ実測する
    (per_class=1でも2要素目まで測ろうとしてクラッシュしない、が主な検査対象)。"""
    verts = _box_verts((1.0, 1.0, 1.0))
    shapes = {"s1": ShapeInfo("s1", verts, _BOX_FACES)}
    elements = [
        ElementInfo("G1", "IFCWALL", "Wall-1", "s1", True, ("Tessellation",), None),
        ElementInfo("G2", "IFCWALL", "Wall-2", "s1", True, ("Tessellation",), None),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    metrics = sample_class_geometry_metrics(model, per_class=1)
    assert metrics["IFCWALL"]["hull_triangle_ratio"] == 1.0


def test_sample_class_geometry_metrics_skips_elements_without_shape():
    elements = [ElementInfo("G1", "IFCWALL", None, None, False, (), None)]
    model = ModelData(schema="IFC4", elements=elements, shapes={})
    assert sample_class_geometry_metrics(model) == {}


def test_sample_class_geometry_metrics_picks_lowest_shape_ids_deterministically():
    """per_class未満に絞られる場合、shape_id昇順で先頭per_class件だけ実測する
    (挿入順に依存しない決定性)。s1_degenerate(先に挿入、退化=1点で計測不能)より
    昇順で後になる s0_normal(正常な立方体)を、辞書順ソートにより先頭に選び直す
    ケース。per_class=1固定で「挿入順のまま先頭を取る」実装だと退化形状しか
    測れずhull_triangle_ratioがメトリクスに現れない(このテストが失敗する)。"""
    box = _box_verts((1.0, 1.0, 1.0))
    degenerate = np.array([[0.0, 0.0, 0.0]])
    shapes = {
        "s1_degenerate": ShapeInfo("s1_degenerate", degenerate, np.zeros((0, 3), dtype=np.int64)),
        "s0_normal": ShapeInfo("s0_normal", box, _BOX_FACES),
    }
    elements = [
        ElementInfo("G1", "IFCWALL", None, "s1_degenerate", False, ("Tessellation",), None),
        ElementInfo("G2", "IFCWALL", None, "s0_normal", False, ("Tessellation",), None),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)

    metrics = sample_class_geometry_metrics(model, per_class=1)
    assert metrics["IFCWALL"]["hull_triangle_ratio"] == 1.0
