"""core/consolidate.py のテスト (design.md §5.4, Phase4 Task2)。

TDD: 平行移動コピー3要素の群 → consolidate 後、IfcRepresentationMap が1つ、
各要素が IfcMappedItem 参照、再抽出で各要素のワールドbboxが不変、ファイル内の
実体形状(IfcTriangulatedFaceSet)が1つに減ることを検証する。
"""

from __future__ import annotations

import re

import numpy as np
import pytest

import ifcopenshell
import ifcopenshell.geom

from ifc_occam.core.consolidate import ConsolidateReport, consolidate_duplicates
from ifc_occam.core.duplicates import DuplicateGroup, find_duplicates
from ifc_occam.core.extract import extract_model
from tests.fixtures_ifc import (
    build_n_translated_copies_ifc,
    build_three_translated_copies_ifc,
)


def _count_entities(ifc_file) -> int:
    """ファイル全体のエンティティ数(STEP行数)を返す(サイズ削減の簡易指標)。"""
    return len(re.findall(r"^#\d+=", ifc_file.to_string(), flags=re.MULTILINE))


def _serialized_size(ifc_file) -> int:
    """シリアライズしたSTEPテキストのバイト数を返す(ファイルサイズ削減の指標)。

    consolidateは「共有ソース1つ+要素ごとの薄いIfcMappedItem」という固定オーバー
    ヘッドを払うため、エンティティ数だけを見ると小さな群では純増になり得る
    (座標配列は1エンティティに収まるため個数が増えない)。実際のファイルサイズ
    (バイト数)で見れば、複製ぶんの座標配列そのものが削減されるため、こちらが
    「ファイルサイズ削減」の実測に対応する指標になる。
    """
    return len(ifc_file.to_string().encode("utf-8"))


def _world_bbox(ifc_file, gid: str) -> tuple[np.ndarray, np.ndarray]:
    """gid要素のワールド座標bbox(min, max)を再抽出して求める。"""
    element = ifc_file.by_guid(gid)
    settings = ifcopenshell.geom.settings()
    settings.set("weld-vertices", True)
    settings.set("use-world-coords", True)
    shape = ifcopenshell.geom.create_shape(settings, element)
    verts = np.array(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
    return verts.min(axis=0), verts.max(axis=0)


def _write_and_reopen(f, tmp_path, name="out.ifc"):
    path = tmp_path / name
    f.write(str(path))
    return ifcopenshell.open(str(path))


def test_three_translated_copies_consolidate_into_one_map(tmp_path):
    f = build_three_translated_copies_ifc()
    elements = f.by_type("IfcBuildingElementProxy")
    gids = [e.GlobalId for e in elements]

    # 変換前のワールドbboxを記録
    before_bboxes = {gid: _world_bbox(f, gid) for gid in gids}

    model, warnings = extract_model(f)
    assert warnings == []
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1
    assert len(groups[0].shape_ids) == 3

    # このフィクスチャは頂点数4の極小形状(マッピング機構の検証が目的で、選別ルール
    # の対象ではない)。min_benefit_ratio=0でサイズ選別を無効化して機構だけを見る。
    report = consolidate_duplicates(f, groups, model, min_benefit_ratio=0)
    assert isinstance(report, ConsolidateReport)
    assert report.groups_applied == 1
    assert report.elements_remapped == 3

    # ファイル内: RepresentationMapが1つ、実体形状(TriangulatedFaceSet)も1つに減る
    assert len(f.by_type("IfcRepresentationMap")) == 1
    assert len(f.by_type("IfcTriangulatedFaceSet")) == 1

    for element in elements:
        body_rep = element.Representation.Representations[0]
        assert len(body_rep.Items) == 1
        assert body_rep.Items[0].is_a("IfcMappedItem")

    reopened = _write_and_reopen(f, tmp_path)
    for gid in gids:
        before_min, before_max = before_bboxes[gid]
        after_min, after_max = _world_bbox(reopened, gid)
        np.testing.assert_allclose(before_min, after_min, atol=1e-6)
        np.testing.assert_allclose(before_max, after_max, atol=1e-6)


def test_group_with_deleted_member_is_skipped(tmp_path):
    f = build_three_translated_copies_ifc()
    elements = f.by_type("IfcBuildingElementProxy")
    gids = [e.GlobalId for e in elements]

    model, _ = extract_model(f)
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1

    # 1要素を削除済みとして扱う(root.remove_productでファイルから除去)
    import ifcopenshell.api

    ifcopenshell.api.run("root.remove_product", f, product=elements[0])

    report = consolidate_duplicates(f, groups, model)
    assert report.groups_applied == 0
    assert report.elements_remapped == 0
    assert any("削除済み" in w or "見つかりません" in w for w in report.warnings)

    # 残存要素のBody表現は変更されない(元のTessellationのまま)
    remaining = f.by_guid(gids[1])
    body_rep = remaining.Representation.Representations[0]
    assert body_rep.Items[0].is_a("IfcTriangulatedFaceSet")


def test_singleton_group_is_not_applicable():
    f = build_three_translated_copies_ifc()
    model, _ = extract_model(f)

    # 単独メンバーの偽群(shape_idが1つだけ)を手作業で作る
    only_shape_id = next(iter(model.shapes))
    fake_group = DuplicateGroup(
        shape_ids=[only_shape_id], triangle_count=4, savable_triangles=0
    )

    report = consolidate_duplicates(f, [fake_group], model)
    assert report.groups_applied == 0
    assert report.elements_remapped == 0


# ---------------------------------------------------------------------------
# IfcStyledItem の移送(Phase4 Task2追補: 色の保持+旧アイテム掃除の解禁)
# ---------------------------------------------------------------------------


def test_three_same_colored_copies_consolidate_transfers_style_and_shrinks():
    """3要素とも同じ色(RGB一致、entityは別)のとき、consolidate後に幾何実体・
    IfcStyledItemがそれぞれ1つに減り、ファイル全体のエンティティ数も減ること。"""
    colors = [(1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
    f = build_three_translated_copies_ifc(colors=colors)

    size_before = _serialized_size(f)
    assert len(f.by_type("IfcStyledItem")) == 3

    model, warnings = extract_model(f)
    assert warnings == []
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1

    # このフィクスチャは頂点数4の極小形状(スタイル移送機構の検証が目的で、選別
    # ルールの対象ではない)。min_benefit_ratio=0でサイズ選別を無効化する。
    report = consolidate_duplicates(f, groups, model, min_benefit_ratio=0)
    assert report.groups_applied == 1
    assert report.elements_remapped == 3

    assert len(f.by_type("IfcTriangulatedFaceSet")) == 1
    styled_items = f.by_type("IfcStyledItem")
    assert len(styled_items) == 1
    assert styled_items[0].Item.is_a("IfcTriangulatedFaceSet")

    size_after = _serialized_size(f)
    assert size_after < size_before


def test_member_with_different_color_is_skipped_from_consolidation():
    """3番目の要素だけ色が異なる場合、その要素は共有化対象外としてスキップされ、
    残り2要素だけがconsolidateされること。"""
    colors = [(1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    f = build_three_translated_copies_ifc(colors=colors)
    elements = f.by_type("IfcBuildingElementProxy")
    gids = [e.GlobalId for e in elements]

    model, _ = extract_model(f)
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1

    # このフィクスチャは頂点数4の極小形状(色フィルタ機構の検証が目的で、選別
    # ルールの対象ではない)。min_benefit_ratio=0でサイズ選別を無効化する。
    report = consolidate_duplicates(f, groups, model, min_benefit_ratio=0)
    assert report.groups_applied == 1
    assert report.elements_remapped == 2
    assert any("色が異なるため共有化対象外" in w for w in report.warnings)

    # 色違いの要素(1つ)は元のTriangulatedFaceSetのまま(共有化されていない)
    unshared_count = sum(
        1
        for gid in gids
        if f.by_guid(gid).Representation.Representations[0].Items[0].is_a(
            "IfcTriangulatedFaceSet"
        )
    )
    assert unshared_count == 1

    # 残り2要素は共有マップ(IfcMappedItem)を参照
    shared_count = sum(
        1
        for gid in gids
        if f.by_guid(gid).Representation.Representations[0].Items[0].is_a("IfcMappedItem")
    )
    assert shared_count == 2


# ---------------------------------------------------------------------------
# 選別ルール(Phase4 Task2追補: サイズ純増を防ぐsavings/overhead比較)
# ---------------------------------------------------------------------------


def test_small_group_with_tiny_shape_is_skipped_for_no_savings():
    """2要素×3頂点(三角形1枚)のような小さい群は、共有化の固定オーバーヘッド
    (IfcMappedItem+IfcCartesianTransformationOperator3D+IfcCartesianPointなど)が
    座標削減分を上回るため、consolidateされずスキップされること。"""
    f = build_n_translated_copies_ifc(n_members=2, n_verts=3)

    model, warnings = extract_model(f)
    assert warnings == []
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1

    report = consolidate_duplicates(f, groups, model)
    assert report.groups_applied == 0
    assert report.elements_remapped == 0
    assert any("節約見込みなし" in w for w in report.warnings)

    # 要素は元のTriangulatedFaceSetのまま(共有化されていない)
    for element in f.by_type("IfcBuildingElementProxy"):
        body_rep = element.Representation.Representations[0]
        assert body_rep.Items[0].is_a("IfcTriangulatedFaceSet")


def test_large_group_with_big_shape_is_consolidated():
    """3要素×500頂点のような大きい群は、座標削減分がオーバーヘッドを大きく上回る
    ため、consolidateされること(min_benefit_ratioの安全マージンを満たす)。"""
    f = build_n_translated_copies_ifc(n_members=3, n_verts=500)

    model, warnings = extract_model(f)
    assert warnings == []
    groups = find_duplicates(model.shapes)
    assert len(groups) == 1

    report = consolidate_duplicates(f, groups, model)
    assert report.groups_applied == 1
    assert report.elements_remapped == 3
    assert len(f.by_type("IfcRepresentationMap")) == 1
