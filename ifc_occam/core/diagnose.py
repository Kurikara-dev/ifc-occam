"""クラス別集計 (design.md §3)。診断層。"""

from collections import defaultdict

from ifc_occam.core.types import ClassStats, ModelData


def aggregate_by_class(model: ModelData, strict: bool = True) -> list[ClassStats]:
    """要素を ifc_class でグループ化し、クラス別の集計を返す。

    total_triangles 降順でソートされる。幾何なし要素(shape_id=None)は
    element_count に数えるが triangles は 0 として扱う。

    契約(shape_id が model.shapes に存在しない場合):
      - strict=True(既定): KeyError をそのまま伝播させる。データ不整合を
        黙殺しない。呼び出し側が対処するまで気付けるようにするための仕様。
      - strict=False: 該当要素を三角形集計から skip する(shape 由来の
        集計は0扱い・unique_shape_countにも数えない)。ただし element_count
        には通常通り数える(要素自体は存在するため)。
    """
    element_counts: dict[str, int] = defaultdict(int)
    shape_ids: dict[str, set[str]] = defaultdict(set)
    total_triangles: dict[str, int] = defaultdict(int)
    mapped_counts: dict[str, int] = defaultdict(int)
    max_single_shape: dict[str, int] = defaultdict(int)

    for elem in model.elements:
        cls = elem.ifc_class
        element_counts[cls] += 1

        if elem.shape_id is not None:
            if strict:
                shape = model.shapes[elem.shape_id]
            else:
                shape = model.shapes.get(elem.shape_id)
            if shape is not None:
                shape_ids[cls].add(elem.shape_id)
                total_triangles[cls] += shape.triangle_count
                max_single_shape[cls] = max(max_single_shape[cls], shape.triangle_count)

        if elem.is_mapped:
            mapped_counts[cls] += 1

    stats = [
        ClassStats(
            ifc_class=cls,
            element_count=count,
            unique_shape_count=len(shape_ids[cls]),
            total_triangles=total_triangles[cls],
            mapped_count=mapped_counts[cls],
            max_single_shape_triangles=max_single_shape[cls],
        )
        for cls, count in element_counts.items()
    ]

    return sorted(stats, key=lambda s: s.total_triangles, reverse=True)
