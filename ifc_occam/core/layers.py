"""レイヤー別集計 (design.md §3 / GUI改修計画 Task3)。診断層。"""

from collections import defaultdict
from dataclasses import dataclass

from ifc_occam.core.types import ModelData


@dataclass
class LayerStats:
    """診断: レイヤー別集計行。"""

    layer: str
    element_count: int
    unique_shape_count: int
    total_triangles: int  # Σ(要素ごとの形状三角形数) = 展開後総三角形数


def aggregate_by_layer(model: ModelData, strict: bool = True) -> list[LayerStats]:
    """要素を layer でグループ化し、レイヤー別の集計を返す。

    total_triangles 降順でソートされる。layer が None の要素(レイヤー未設定)は
    結果に含めない。幾何なし要素(shape_id=None)は element_count に数えるが
    triangles は 0 として扱う。

    契約(shape_id が model.shapes に存在しない場合):
      - strict=True(既定): KeyError をそのまま伝播させる。データ不整合を
        黙殺しない。呼び出し側が対処するまで気付けるようにするための仕様。
      - strict=False: 該当要素を三角形集計から skip する(shape 由来の
        集計は0扱い・unique_shape_countにも数えない)。ただし element_count
        には通常通り数える(要素自体は存在するため)。
      - **layer が None の要素はこの契約の対象外**: 集計そのものから除外
        されるため、shape_id が不整合でも strict=True で例外にならない。
        `aggregate_by_class` は ifc_class が非nullableなので全要素が検証を
        通り、この抜けが無い——両者の唯一の意味的な差なので明記しておく
        (Task 3 レビュー Important-1)。「結果に出てこない要素のために集計
        全体を落とす」方が筋が悪いと判断してこの非対称を選んでいる。
        なお本番経路では、load 時に `aggregate_by_class(model)` が全要素に
        対して strict=True で走る(`server/app.py` の `_run_load`)ため、
        不整合はそちらで先に検出される。
    """
    element_counts: dict[str, int] = defaultdict(int)
    shape_ids: dict[str, set[str]] = defaultdict(set)
    total_triangles: dict[str, int] = defaultdict(int)

    for elem in model.elements:
        layer = elem.layer
        if layer is None:
            continue
        element_counts[layer] += 1

        if elem.shape_id is not None:
            if strict:
                shape = model.shapes[elem.shape_id]
            else:
                shape = model.shapes.get(elem.shape_id)
            if shape is not None:
                shape_ids[layer].add(elem.shape_id)
                total_triangles[layer] += shape.triangle_count

    stats = [
        LayerStats(
            layer=layer,
            element_count=count,
            unique_shape_count=len(shape_ids[layer]),
            total_triangles=total_triangles[layer],
        )
        for layer, count in element_counts.items()
    ]

    return sorted(stats, key=lambda s: s.total_triangles, reverse=True)
