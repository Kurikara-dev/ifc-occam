"""IFCの参照グラフを検査するテスト用ヘルパ。

「表現から到達できない幾何」を数える。IfcStyledItem だけが旧形状を掴んでいて
remove_deep2 が消せずに残る、という不具合を検出するために使う。
"""

from collections import Counter

import ifcopenshell

# 親から必ず辿られるはずの幾何プリミティブ。表現から到達できないなら死荷重。
TRACKED_TYPES = (
    "IfcClosedShell",
    "IfcOpenShell",
    "IfcFace",
    "IfcPolyLoop",
    "IfcFacetedBrep",
    "IfcTriangulatedFaceSet",
)


def _iter_entities(value):
    """属性値からエンティティだけを取り出す(入れ子のタプル/リストを平坦化する)。"""
    if isinstance(value, ifcopenshell.entity_instance):
        yield value
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _iter_entities(item)


def unreachable_geometry(ifc_file) -> dict[str, int]:
    """IfcShapeRepresentation / IfcRepresentationMap から到達できない幾何を型別に数える。

    IfcStyledItem からは先へ辿らない。スタイルだけが旧形状を掴んで残しているケースを
    「到達不能」として検出したいため(スタイル経由で辿ると健全に見えてしまう)。
    戻り値は0件の型を含まない辞書。全て健全なら空辞書。
    """
    reachable: set[int] = set()
    stack = list(ifc_file.by_type("IfcShapeRepresentation"))
    stack += list(ifc_file.by_type("IfcRepresentationMap"))
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
