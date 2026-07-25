"""削除連鎖の計算 (design.md §5.3)。IFCグラフ上のみで動作し、ifc_file を変更しない。

連鎖規則:
  - 削除対象要素の IfcRelVoidsElement による開口(IfcOpeningElement)
  - その開口を充填する要素(IfcRelFillsElement の窓・ドア等)
  - 削除対象要素の IfcRelAggregates による子部材(再帰。子がさらに開口や
    子部材を持つ場合はそこからも連鎖する)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CascadeItem:
    """連鎖的に削除される1要素。"""

    global_id: str
    ifc_class: str
    name: str | None
    reason: str


@dataclass
class DeleteClosure:
    """削除クロージャ。direct(明示指定) + cascaded(連鎖由来) + all_gids(重複なし)。"""

    direct: list[str]
    cascaded: list[CascadeItem] = field(default_factory=list)
    all_gids: set[str] = field(default_factory=set)


def _cascade_from_element(element, seen: set[str]) -> list[CascadeItem]:
    """1要素から連鎖する子要素を(開口→充填→再帰的な集約子)の順で洗い出す。

    seen: これまでに direct/cascaded として確定済みの GlobalId 集合。破壊的に更新される。
    """
    items: list[CascadeItem] = []

    # IfcRelVoidsElement: 開口
    for rel in getattr(element, "HasOpenings", None) or ():
        opening = rel.RelatedOpeningElement
        if opening.GlobalId not in seen:
            seen.add(opening.GlobalId)
            items.append(
                CascadeItem(
                    global_id=opening.GlobalId,
                    ifc_class=opening.is_a(),
                    name=opening.Name,
                    reason="開口(親要素の削除)",
                )
            )
            items.extend(_cascade_from_element(opening, seen))

        # IfcRelFillsElement: 開口を充填する要素(窓・ドア等)
        for fills_rel in getattr(opening, "HasFillings", None) or ():
            filling = fills_rel.RelatedBuildingElement
            if filling.GlobalId not in seen:
                seen.add(filling.GlobalId)
                items.append(
                    CascadeItem(
                        global_id=filling.GlobalId,
                        ifc_class=filling.is_a(),
                        name=filling.Name,
                        reason="開口の充填要素",
                    )
                )
                items.extend(_cascade_from_element(filling, seen))

    # IfcRelAggregates: 集約の子部材(再帰)
    for rel in getattr(element, "IsDecomposedBy", None) or ():
        if not rel.is_a("IfcRelAggregates"):
            continue
        for child in rel.RelatedObjects:
            if child.GlobalId not in seen:
                seen.add(child.GlobalId)
                items.append(
                    CascadeItem(
                        global_id=child.GlobalId,
                        ifc_class=child.is_a(),
                        name=child.Name,
                        reason="集約の子部材",
                    )
                )
                items.extend(_cascade_from_element(child, seen))

    return items


def compute_delete_closure(ifc_file, gids: list[str]) -> DeleteClosure:
    """削除対象 gids から、開口/充填/集約子を連鎖的にたどった DeleteClosure を返す。

    読み取り専用。ifc_file は一切変更しない。
    direct は入力順を保持する(重複があっても保持)。cascaded は direct に含まれる
    要素を含まない。all_gids は direct + cascaded の重複なし集合。
    """
    seen: set[str] = set(gids)
    cascaded: list[CascadeItem] = []

    for gid in gids:
        element = ifc_file.by_guid(gid)
        cascaded.extend(_cascade_from_element(element, seen))

    all_gids = set(gids) | {item.global_id for item in cascaded}

    return DeleteClosure(direct=list(gids), cascaded=cascaded, all_gids=all_gids)
