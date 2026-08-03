"""簡略化手法の適正判定 (design.md OBB+適正判定フェーズ Task4)。

密度(decimateの間引き余地)・表現種別(三角形化書き戻しのサイズ増リスク)・
凸性(凸包の削減効果)・斜め度合い(OBBの優位性)をサンプル実測から判定し、
GUI/CUIに注意文を出す。core配下の純粋モジュールであり、ifcopenshell を
import しない(ModelData/ShapeInfo は core.types の定義。抽出層への依存を持たない)。
"""

from __future__ import annotations

import numpy as np

from ifc_occam.core.simplify import bbox_mesh, convex_hull_mesh, obb_mesh
from ifc_occam.core.types import ModelData, ShapeInfo

__all__ = ["advise_simplify", "metrics_from_shapes", "sample_class_geometry_metrics"]

# ---------------------------------------------------------------------------
# 判定閾値(実測根拠は各定数のdocstringに記載。task-4-brief.md 記載の値)
# ---------------------------------------------------------------------------

_DECIMATE_LOW_DENSITY_THRESHOLD = 500.0
"""実測: 平均154tri/形状のモデルでdecimate 0.1が18%減どまり。"""

_HULL_NEAR_CONVEX_RATIO = 0.6
"""実測: パイプ主体モデルで凸包後63%残存。"""

_OBB_RECOMMEND_RATIO = 0.5
"""実測: 斜材モデルで平均0.143。"""


def advise_simplify(
    method: str,
    *,
    avg_triangles_per_shape: float | None = None,
    triangle_source: str | None = None,
    hull_triangle_ratio: float | None = None,
    obb_volume_ratio: float | None = None,
) -> list[str]:
    """method(適用予定の簡略化手法)と実測メトリクスから注意文のリストを返す。

    メトリクスがNone(サンプル実測なし等で判定不能)の規則は発火しない
    (判定不能は沈黙、task-4-brief.md)。文言はliteral固定(f-string部を除く)。
    """
    messages: list[str] = []

    if method == "decimate" and avg_triangles_per_shape is not None:
        if avg_triangles_per_shape < _DECIMATE_LOW_DENSITY_THRESHOLD:
            messages.append(
                f"平均{avg_triangles_per_shape:.0f}三角形/形状の粗いメッシュのため、"
                "間引きは指定した率まで削れないことがあります。"
            )

    if method in ("decimate", "convex_hull") and triangle_source is not None:
        if triangle_source != "tessellation":
            messages.append(
                "三角形化して書き戻すため、三角形数が減ってもファイルサイズは増えることが"
                "あります。サイズ削減が目的なら OBB/bbox か削除を検討してください。"
            )

    if method == "convex_hull" and hull_triangle_ratio is not None:
        if hull_triangle_ratio >= _HULL_NEAR_CONVEX_RATIO:
            messages.append(
                f"ほぼ凸の形状です(サンプル判定: 凸包後も三角形の{hull_triangle_ratio:.0%}が"
                "残ります)。凸包の削減効果は小さい見込みです。"
            )

    if method == "bbox" and obb_volume_ratio is not None:
        if obb_volume_ratio <= _OBB_RECOMMEND_RATIO:
            messages.append(
                f"部材が座標軸に対して斜めです(サンプル判定: OBBなら箱の体積が平均"
                f"{obb_volume_ratio:.0%}に縮みます)。OBB(向き付きbbox)の方が形に沿います。"
            )

    return messages


def _mesh_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """符号付き体積(発散定理)。tests/test_simplify.py の _mesh_volume と同じ式の
    私有実装(advisor.py内でしか使わないため二重管理を避ける共有はしない)。"""
    tri = verts[faces]
    return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def metrics_from_shapes(shapes: list[ShapeInfo]) -> dict[str, float]:
    """ShapeInfoの列から {"hull_triangle_ratio": x, "obb_volume_ratio": y} を計算する。

    sample_class_geometry_metrics の内側(shape群→メトリクス変換)を公開関数として
    抽出したもの(CUI確認2でのサンプル実測がModelData/shape_idを持たず、
    create_shapeで直接得たShapeInfoの列を渡したいため、Task4-CUIで公開化)。

    hull_triangle_ratio = Σ(凸包後tri)/Σ(元tri)(渡された列全体での集計比)。
    obb_volume_ratio = 平均(OBB実体積/AABB体積)(形状ごとの比の平均)。
    OBB実体積は obb_mesh の返り値verts/facesから符号付きメッシュ体積(発散定理)の
    絶対値で測る。AABB体積が0(平面形状)の形状はobb_volume_ratio計算から
    スキップする(分母ゼロ)。凸包・OBBが例外を出す形状もその計測からスキップし、
    分母に入れない。測れる指標が1つもなければ空dictを返す(スキップ規則は
    sample_class_geometry_metrics と同一)。
    """
    hull_tri_sum = 0
    orig_tri_sum = 0
    obb_ratios: list[float] = []

    for shape in shapes:
        verts = shape.vertices
        faces = shape.faces

        try:
            _hull_verts, hull_faces = convex_hull_mesh(verts)
        except Exception:  # noqa: BLE001 - 退化形状はサンプルからスキップする
            pass
        else:
            orig_tri_sum += len(faces)
            hull_tri_sum += len(hull_faces)

        aabb_verts, _aabb_faces = bbox_mesh(verts)
        aabb_ext = aabb_verts.max(axis=0) - aabb_verts.min(axis=0)
        aabb_vol = float(np.prod(aabb_ext))
        if aabb_vol <= 0.0:
            continue  # 平面形状(AABB体積0)はobb_volume_ratioの計測をスキップ
        try:
            obb_verts, obb_faces = obb_mesh(verts)
        except Exception:  # noqa: BLE001 - 退化形状はサンプルからスキップする
            continue
        obb_vol = abs(_mesh_volume(obb_verts, obb_faces))
        obb_ratios.append(obb_vol / aabb_vol)

    metrics: dict[str, float] = {}
    if orig_tri_sum > 0:
        metrics["hull_triangle_ratio"] = hull_tri_sum / orig_tri_sum
    if obb_ratios:
        metrics["obb_volume_ratio"] = sum(obb_ratios) / len(obb_ratios)
    return metrics


def sample_class_geometry_metrics(
    model: ModelData, per_class: int = 20
) -> dict[str, dict[str, float]]:
    """ifc_class → {"hull_triangle_ratio": x, "obb_volume_ratio": y} のサンプル実測。

    決定性: クラスごとに要素が参照するshape_idを集めて昇順ソートし、先頭per_class件を
    実測する(挿入順・スレッド到着順に依存しない)。同一形状が同クラス複数要素から
    参照されても1回だけ測る(shape_id集合を使うため自然に重複除去される)。
    実際のメトリクス計算は metrics_from_shapes に委譲する(計算ロジックの二重化を
    避ける)。1形状も測れなければそのクラスのメトリクス自体を省く。
    """
    shape_ids_by_class: dict[str, set[str]] = {}
    for elem in model.elements:
        if elem.shape_id is None:
            continue
        shape_ids_by_class.setdefault(elem.ifc_class, set()).add(elem.shape_id)

    result: dict[str, dict[str, float]] = {}
    for ifc_class, shape_id_set in shape_ids_by_class.items():
        sample_ids = sorted(shape_id_set)[:per_class]
        shapes = [model.shapes[sid] for sid in sample_ids if sid in model.shapes]

        metrics = metrics_from_shapes(shapes)
        if metrics:
            result[ifc_class] = metrics

    return result
