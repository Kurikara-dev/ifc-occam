"""テキストモード削除の pset 残置回収(textops/plan.py)のテスト。

削除対象を参照する IfcRelDefinesByProperties は汎用 IFCREL* パッチで
RelatedObjects から死んだ id が抜かれるが、空になっても rel 自身は残り、
RelatingPropertyDefinition(pset)と IfcPropertySingleValue 群を掴み
続けていた(donuts 全削除の実測で 912 pset + 922 value ≒ 残存 214KB の
大半)。full-open 削除は root.remove_product が pset まで消すため、
テキストモードだけが太る既知の差分だった。修正は「related が全滅した
RelDefines 自身を dead にし、解放された pset サブグラフを sweep で回収」。
"""

import ifcopenshell
import ifcopenshell.api

from tests.fixtures_ifc import build_single_consumer_mapped_child_styled_brep_ifc
from tests.test_textops_annotations import (
    _count,
    _make_wall_with_own_brep,
    _text_delete,
)


def _add_pset(f, element, name):
    """element に pset(IfcPropertySingleValue 1件入り)を付け、
    (rel, pset) を返す。ifcopenshell.api の pset モジュールで作る
    (IfcRelDefinesByProperties + IfcPropertySet + value が生成される)。"""
    pset = ifcopenshell.api.run("pset.add_pset", f, product=element, name=name)
    ifcopenshell.api.run(
        "pset.edit_pset", f, pset=pset, properties={"合成キー": "合成値"}
    )
    rel = next(
        r
        for r in f.by_type("IfcRelDefinesByProperties")
        if r.RelatingPropertyDefinition == pset
    )
    return rel, pset


def test_text_delete_reaps_empty_rel_defines_and_exclusive_pset(tmp_path):
    """クラス全削除で、その要素の pset・値・RelDefines が全て出力から消える。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    _add_pset(f, proxy, "合成Pset")

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcBuildingElementProxy") == 0
    assert _count(out, "IfcRelDefinesByProperties") == 0
    assert _count(out, "IfcPropertySet") == 0
    assert _count(out, "IfcPropertySingleValue") == 0


def test_shared_pset_survives_while_any_user_lives(tmp_path):
    """2つの RelDefines(要素ごとに1本)が同一 pset を共有。片方のクラス
    だけ削除したら、死んだ側の rel は消えるが pset と値は残る。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    body_ctx = proxy.Representation.Representations[0].ContextOfItems
    wall, _rep, _shell = _make_wall_with_own_brep(f, body_ctx)
    _rel, pset = _add_pset(f, proxy, "共有Pset")
    f.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=[wall],
        RelatingPropertyDefinition=pset,
    )

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcRelDefinesByProperties") == 1
    assert _count(out, "IfcPropertySet") == 1
    assert _count(out, "IfcPropertySingleValue") == 1
    assert list(out.by_type("IfcRelDefinesByProperties")[0].RelatedObjects)[
        0
    ].is_a("IfcWall")

    out2, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy", "IfcWall"])
    assert _count(out2, "IfcRelDefinesByProperties") == 0
    assert _count(out2, "IfcPropertySet") == 0
    assert _count(out2, "IfcPropertySingleValue") == 0


def test_partially_dead_rel_defines_is_patched_not_reaped(tmp_path):
    """1本の RelDefines が2クラスの要素を束ねる。片方のクラスだけ削除したら、
    rel は生き残り、死んだ要素だけが RelatedObjects から抜ける(既存の
    汎用パッチの挙動が退行していないこと)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    body_ctx = proxy.Representation.Representations[0].ContextOfItems
    wall, _rep, _shell = _make_wall_with_own_brep(f, body_ctx)
    rel, _pset = _add_pset(f, proxy, "束ねPset")
    rel.RelatedObjects = [proxy, wall]

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    rels = out.by_type("IfcRelDefinesByProperties")
    assert len(rels) == 1
    related = list(rels[0].RelatedObjects)
    assert len(related) == 1
    assert related[0].is_a("IfcWall")
    assert _count(out, "IfcPropertySet") == 1
    assert _count(out, "IfcPropertySingleValue") == 1


def test_reaped_rel_defines_referrer_chain_is_patched(tmp_path):
    """(合成・スキーマ外データ)RelDefines 自身が別の IfcRel から参照されて
    いても dangling を作らないこと。従来は patch 段の規則3(空になった rel の
    drop)が plan から見えないところで rel を消すため、参照元がパッチ候補に
    ならず dangling になっていた(本フェーズの RED で発覚した既存ギャップ)。
    plan 段で reap すれば参照元は _generic_rel_candidates のパッチ候補に
    入り、空になった参照元自身は規則3が落とす——連鎖全体が矛盾なく消える。
    dangling が増えていないことは _text_delete 内の raw 再スキャンが検査する。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    proxy = f.by_type("IfcBuildingElementProxy")[0]
    rel, _pset = _add_pset(f, proxy, "被参照Pset")
    group = f.create_entity("IfcGroup", GlobalId=ifcopenshell.guid.new(), Name="G")
    f.create_entity(
        "IfcRelAssignsToGroup",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=[rel],
        RelatingGroup=group,
    )

    out, _ = _text_delete(tmp_path, f, ["IfcBuildingElementProxy"])

    assert _count(out, "IfcRelDefinesByProperties") == 0
    assert _count(out, "IfcPropertySet") == 0
    assert _count(out, "IfcPropertySingleValue") == 0
    # 参照元も RelatedObjects が空になり規則3で落ち、グループ本体は残る
    # (孤児レコードにはなるが dangling は無い)
    assert _count(out, "IfcRelAssignsToGroup") == 0
    assert _count(out, "IfcGroup") == 1
