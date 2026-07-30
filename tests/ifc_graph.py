"""IFCの参照グラフを検査するテスト用ヘルパ。

「表現から到達できない幾何」を数える。IfcStyledItem だけが旧形状を掴んでいて
remove_deep2 が消せずに残る、という不具合を検出するために使う。
"""

from collections import Counter

import ifcopenshell

# 親から必ず辿られるはずの幾何。製品から到達できないなら死荷重。
# IfcShapeRepresentation / IfcRepresentationMap / IfcMappedItem は
# 「孤児になった器」自体を検出するために追加(2026-07-29)。
TRACKED_TYPES = (
    "IfcClosedShell",
    "IfcOpenShell",
    "IfcFace",
    "IfcPolyLoop",
    "IfcFacetedBrep",
    "IfcTriangulatedFaceSet",
    "IfcShapeRepresentation",
    "IfcRepresentationMap",
    "IfcMappedItem",
)


def _iter_entities(value):
    """属性値からエンティティだけを取り出す(入れ子のタプル/リストを平坦化する)。"""
    if isinstance(value, ifcopenshell.entity_instance):
        yield value
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _iter_entities(item)


def unreachable_geometry(ifc_file) -> dict[str, int]:
    """製品(IfcProduct)から到達できない幾何を型別に数える。

    到達性のルートは「製品が Representation 経由で参照する IfcRepresentation」と
    「IfcTypeProduct.RepresentationMaps の IfcRepresentationMap」だけ。
    以前は全 IfcShapeRepresentation / IfcRepresentationMap をルートにしていた
    ため、どの製品からも参照されない孤児 rep 自体がルート扱いになり、
    レイヤー割当が旧repをピン留めして残す不具合の出力でも空辞書を返していた
    (オラクルの穴、2026-07-29)。共有マップは製品側の IfcMappedItem →
    MappingSource 経由で到達できるため、ルートに直接加えるのは型定義側だけで
    足りる。

    IfcStyledItem からは先へ辿らない(従来どおり。forward 属性からは実質
    到達しないので保険)。戻り値は0件の型を含まない辞書。全て健全なら空辞書。
    """
    stack = []
    for product in ifc_file.by_type("IfcProduct"):
        shape = getattr(product, "Representation", None)
        if shape is None:
            continue
        stack.extend(getattr(shape, "Representations", None) or [])
    for type_product in ifc_file.by_type("IfcTypeProduct"):
        stack.extend(getattr(type_product, "RepresentationMaps", None) or [])

    reachable: set[int] = set()
    while stack:
        node = stack.pop()
        node_id = node.id()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        if node.is_a("IfcStyledItem"):
            continue
        for value in node:
            stack.extend(_iter_entities(value))

    counts: Counter = Counter()
    for type_name in TRACKED_TYPES:
        for instance in ifc_file.by_type(type_name):
            if instance.id() not in reachable:
                counts[type_name] += 1
    return dict(counts)
