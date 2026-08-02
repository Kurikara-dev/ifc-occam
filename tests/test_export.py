"""core/export.py のテスト (design.md Phase3 Task4)。"""

from __future__ import annotations

import datetime
import re
import shutil
from pathlib import Path
from unittest.mock import Mock

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.geom
import ifcopenshell.util.element
import pytest

from ifc_occam import __version__
from ifc_occam.core import export as export_module
from ifc_occam.core.export import (
    ExportReport,
    apply_operations,
    resolve_output_path,
    verify_no_dangling,
)
from ifc_occam.core.ops import Operation
from tests.fixtures_ifc import (
    build_hybrid_direct_and_mapped_share_ifc,
    build_ifc2x3_single_element_ifc,
    build_many_minimal_products_ifc,
    build_many_walls_with_openings_ifc,
    build_n_translated_copies_ifc,
    build_three_elements_sharing_mapped_shape_ifc,
    build_three_translated_copies_ifc,
    build_two_elements_sharing_mapped_shape_ifc,
    build_two_elements_sharing_representation_directly_ifc,
    build_two_maps_sharing_mapped_representation_ifc,
    build_wall_with_window_ifc,
)


def _gid(entity) -> str:
    return entity.GlobalId


def _write_fixture(f: ifcopenshell.file, tmp_path, name: str = "src.ifc") -> str:
    path = tmp_path / name
    f.write(str(path))
    return str(path)


def _n_triangles(element) -> int:
    settings = ifcopenshell.geom.settings()
    shape = ifcopenshell.geom.create_shape(settings, element)
    return len(shape.geometry.faces) // 3


def _progress_recorder():
    """apply_operations(progress=...) 用のテスト計測器。(stage, done, total) を蓄積する。"""
    calls: list[tuple[str, int, int]] = []

    def record(stage: str, done: int, total: int) -> None:
        calls.append((stage, done, total))

    return calls, record


# ---------------------------------------------------------------------------
# delete: 壁削除 → 壁・開口・窓が消え、関係にdanglingなし
# ---------------------------------------------------------------------------


def test_delete_wall_cascades_and_leaves_no_dangling_refs(tmp_path):
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    opening = f.by_type("IfcOpeningElement")[0]
    window = f.by_type("IfcWindow")[0]
    assembly = f.by_type("IfcElementAssembly")[0]

    wall_gid, opening_gid, window_gid = _gid(wall), _gid(opening), _gid(window)
    assembly_gid = _gid(assembly)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [Operation(op="delete", targets=[wall_gid])]
    report = apply_operations(src_path, ops, out_path)

    assert isinstance(report, ExportReport)
    assert set(report.deleted) == {wall_gid, opening_gid, window_gid}
    assert report.output_path == out_path

    reopened = ifcopenshell.open(out_path)
    for gid in (wall_gid, opening_gid, window_gid):
        with pytest.raises(RuntimeError):
            reopened.by_guid(gid)

    # 無関係の要素(アセンブリ+子部材)は残存する
    assert reopened.by_guid(assembly_gid) is not None

    dangling = verify_no_dangling(reopened, {wall_gid, opening_gid, window_gid})
    assert dangling == []

    # 生の関係エンティティにも削除済みGlobalIdが残っていないこと
    for rel in reopened.by_type("IfcRelVoidsElement"):
        assert rel.RelatingBuildingElement.GlobalId not in (wall_gid, opening_gid, window_gid)
        assert rel.RelatedOpeningElement.GlobalId not in (wall_gid, opening_gid, window_gid)
    for rel in reopened.by_type("IfcRelFillsElement"):
        assert rel.RelatingOpeningElement.GlobalId not in (wall_gid, opening_gid, window_gid)
        assert rel.RelatedBuildingElement.GlobalId not in (wall_gid, opening_gid, window_gid)


def test_delete_assembly_child_leaves_sibling_and_rel_intact(tmp_path):
    f = build_wall_with_window_ifc()
    assembly = f.by_type("IfcElementAssembly")[0]
    member1, member2 = f.by_type("IfcBeam")

    member1_gid, member2_gid = _gid(member1), _gid(member2)
    assembly_gid = _gid(assembly)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [Operation(op="delete", targets=[member1_gid])]
    report = apply_operations(src_path, ops, out_path)

    assert report.deleted == [member1_gid]

    reopened = ifcopenshell.open(out_path)
    with pytest.raises(RuntimeError):
        reopened.by_guid(member1_gid)

    remaining_member2 = reopened.by_guid(member2_gid)
    assert remaining_member2 is not None

    rels = reopened.by_type("IfcRelAggregates")
    assert len(rels) == 1
    assert [o.GlobalId for o in rels[0].RelatedObjects] == [member2_gid]

    dangling = verify_no_dangling(reopened, {member1_gid})
    assert dangling == []


# ---------------------------------------------------------------------------
# simplify: small.ifc の実要素で bbox → 三角形数12、GlobalId不変
# ---------------------------------------------------------------------------


def test_simplify_bbox_on_real_element_reduces_to_12_triangles(tmp_path, small_ifc_path):
    src_copy = tmp_path / "small_src.ifc"
    shutil.copy(small_ifc_path, src_copy)

    model = ifcopenshell.open(str(src_copy))
    target = next(
        p
        for p in model.by_type("IfcProduct")
        if getattr(p, "Representation", None) is not None
    )
    target_gid = target.GlobalId
    target_name = target.Name
    target_class = target.is_a()

    out_path = str(tmp_path / "out.ifc")
    ops = [Operation(op="simplify", targets=[target_gid], params={"method": "bbox"})]
    report = apply_operations(str(src_copy), ops, out_path)

    assert report.simplified == [target_gid]
    assert report.deleted == []

    reopened = ifcopenshell.open(out_path)
    element2 = reopened.by_guid(target_gid)
    assert element2.Name == target_name
    assert element2.is_a() == target_class
    assert _n_triangles(element2) == 12

    # 原本は非破壊
    original = ifcopenshell.open(str(small_ifc_path))
    assert original.by_guid(target_gid) is not None


# ---------------------------------------------------------------------------
# keep: 何も変化しない
# ---------------------------------------------------------------------------


def test_keep_leaves_element_unchanged(tmp_path):
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    wall_gid = _gid(wall)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [Operation(op="keep", targets=[wall_gid])]
    report = apply_operations(src_path, ops, out_path)

    assert report.deleted == []
    assert report.simplified == []

    reopened = ifcopenshell.open(out_path)
    assert reopened.by_guid(wall_gid) is not None
    assert reopened.by_guid(wall_gid).Name == "Wall1"


# ---------------------------------------------------------------------------
# delete → keep (後方が勝つ) → 要素は残る
# ---------------------------------------------------------------------------


def test_delete_then_keep_last_wins_element_remains(tmp_path):
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    wall_gid = _gid(wall)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="delete", targets=[wall_gid]),
        Operation(op="keep", targets=[wall_gid]),
    ]
    report = apply_operations(src_path, ops, out_path)

    assert wall_gid not in report.deleted

    reopened = ifcopenshell.open(out_path)
    assert reopened.by_guid(wall_gid) is not None


# ---------------------------------------------------------------------------
# simplify対象が別の削除によりcascade削除された場合はスキップされる
# ---------------------------------------------------------------------------


def test_simplify_target_cascade_deleted_by_other_delete_is_skipped(tmp_path):
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    window = f.by_type("IfcWindow")[0]
    wall_gid, window_gid = _gid(wall), _gid(window)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="delete", targets=[wall_gid]),
        Operation(op="simplify", targets=[window_gid], params={"method": "bbox"}),
    ]
    report = apply_operations(src_path, ops, out_path)

    assert window_gid in report.deleted
    assert report.simplified == []
    assert any(item.global_id == window_gid for item in report.skipped)

    reopened = ifcopenshell.open(out_path)
    with pytest.raises(RuntimeError):
        reopened.by_guid(window_gid)


# ---------------------------------------------------------------------------
# verify_no_dangling: 正常なファイルでは違反なし
# ---------------------------------------------------------------------------


def test_verify_no_dangling_returns_empty_for_untouched_fixture():
    f = build_wall_with_window_ifc()
    assert verify_no_dangling(f, set()) == []


# ---------------------------------------------------------------------------
# scope="shared": 同一 IfcRepresentationMap を参照する複数gidに対する
# simplifyは、共有形状を1回だけ書き換える(重複処理でin-place編集が
# 複合しないこと)
# ---------------------------------------------------------------------------


def test_simplify_shared_scope_processes_shared_map_only_once(tmp_path):
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(
            op="simplify",
            targets=[gid1, gid2],
            scope="shared",
            params={"method": "bbox"},
        )
    ]
    report = apply_operations(src_path, ops, out_path)

    assert set(report.simplified) == {gid1, gid2}
    assert report.skipped == []

    reopened = ifcopenshell.open(out_path)
    tfs_list = reopened.by_type("IfcTriangulatedFaceSet")
    assert len(tfs_list) == 1

    e1 = reopened.by_guid(gid1)
    e2 = reopened.by_guid(gid2)
    assert _n_triangles(e1) == 12
    assert _n_triangles(e2) == 12


# ---------------------------------------------------------------------------
# フェーズ最終レビューI-2: フォールバックした要素が共有マップを「処理済み」に
# マークしてしまい、同一マップの兄弟が黙ってスキップされないこと
# ---------------------------------------------------------------------------


def test_shared_simplify_with_first_sibling_fallback_still_reaches_others(
    tmp_path, monkeypatch
):
    """1マップを3要素が共有し、最初に処理される要素だけMappingTargetを安全に
    逆変換できずscope="element"にフォールバックする場合でも、残り2要素には
    共有適用が実際に届くこと(フェーズ最終レビューI-2)。

    修正前は processed_shared_maps へのマークが _apply_simplify の**前**に
    (成功/フォールバックを問わず)行われるため、フォールバックした要素1つが
    共有マップを「処理済み」にしてしまい、残りの兄弟は実際には形状が
    変わらないまま simplified に計上されるだけになる(黙ったスキップ)。
    """
    import ifc_occam.core.simplify as simplify_module

    f = build_three_elements_sharing_mapped_shape_ifc()
    elem1, elem2, elem3 = f.by_type("IfcBuildingElementProxy")
    gid1, gid2, gid3 = _gid(elem1), _gid(elem2), _gid(elem3)

    # 最初の呼び出し(=最初に処理される要素)だけMappingTargetを安全に逆変換
    # できないことにし、以降は本物の実装(恒等)に委ねる。
    original_matrix_fn = simplify_module._transform_operator_matrix
    call_count = {"n": 0}

    def _matrix_none_on_first_call(op):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return original_matrix_fn(op)

    monkeypatch.setattr(
        simplify_module, "_transform_operator_matrix", _matrix_none_on_first_call
    )

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(
            op="simplify",
            targets=[gid1, gid2, gid3],
            scope="shared",
            params={"method": "bbox"},
        )
    ]
    report = apply_operations(src_path, ops, out_path)

    assert set(report.simplified) == {gid1, gid2, gid3}
    assert any("フォールバック" in w for w in report.warnings)

    reopened = ifcopenshell.open(out_path)
    # 三角形1枚(1面)→bboxで直方体(12三角形)。兄弟2・3にも共有適用が届いていれば
    # 12三角形になる(修正前は元の1面のまま=黙ってスキップされている)。
    assert _n_triangles(reopened.by_guid(gid2)) == 12
    assert _n_triangles(reopened.by_guid(gid3)) == 12


# ---------------------------------------------------------------------------
# carry-forward Phase M(操作表記の統一、CF-A最終レビューM-1): 先勝ち警告に
# 使う _method_desc を日本語主表記+英語併記にする。CUIの操作リスト
# (session.py _op_label/_SET_OP_LABELS: bbox軽量化/凸包化/間引き)と同じ
# 語彙に揃える(この警告文はGUI/CUI双方のexport経路から出るため)。
# ---------------------------------------------------------------------------


def test_method_desc_decimate_with_ratio_shows_japanese_label_and_ratio():
    assert export_module._method_desc("decimate", 0.1) == "間引き(decimate, ratio=0.1)"


def test_method_desc_bbox_without_ratio_shows_japanese_label():
    assert export_module._method_desc("bbox", None) == "bbox軽量化(bbox)"


def test_method_desc_convex_hull_without_ratio_shows_japanese_label():
    assert export_module._method_desc("convex_hull", None) == "凸包化(convex_hull)"


def test_method_desc_unknown_method_passes_through_unchanged():
    """_METHOD_LABELS に日本語ラベルの無い未知のmethodは、従来どおり
    英語表記のまま素通しする(防御。将来simplifyにmethodが増えても
    ここでの分岐漏れがexportを止めない)。"""
    assert export_module._method_desc("mystery", None) == "mystery"
    assert export_module._method_desc("mystery", 0.3) == "mystery(ratio=0.3)"


# ---------------------------------------------------------------------------
# フェーズ最終レビューI-1: 同一共有マップへ異なるsimplify操作が到達したとき、
# 先勝ちの挙動自体は変えず、無視された側に警告を出すこと
# ---------------------------------------------------------------------------


def test_shared_simplify_conflicting_ops_on_same_map_warns_about_first_come_wins(
    tmp_path,
):
    """1つの共有マップを指す2要素に、異なるsimplify操作(bbox→先着、
    decimate 0.1→後着)を同時に適用すると、後着側は先着の方法で処理済みの
    ため適用されず、その旨の警告が出ること(フェーズ最終レビューI-1)。
    先勝ちという挙動自体は変更しない(可視化のみ)。
    """
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="simplify", targets=[gid1], scope="shared", params={"method": "bbox"}),
        Operation(
            op="simplify",
            targets=[gid2],
            scope="shared",
            params={"method": "decimate", "ratio": 0.1},
        ),
    ]
    report = apply_operations(src_path, ops, out_path)

    assert set(report.simplified) == {gid1, gid2}
    expected_warning = (
        "共有形状は先行の bbox軽量化(bbox) で処理済みのため、"
        f"この要素(GlobalId={gid2})への 間引き(decimate, ratio=0.1) は適用されません"
        "(共有波及の先勝ち)。"
    )
    assert expected_warning in report.warnings

    reopened = ifcopenshell.open(out_path)
    # 先勝ち: 両要素ともbbox(12三角形)になり、decimateは効いていない
    # (挙動自体は従来どおり。警告で可視化されるようになった点のみが変化)。
    assert _n_triangles(reopened.by_guid(gid1)) == 12
    assert _n_triangles(reopened.by_guid(gid2)) == 12


def test_shared_simplify_same_method_different_ratio_warning_shows_both_ratios(
    tmp_path,
):
    """同一 method(decimate)で ratio だけ違う衝突では、警告に双方の ratio が
    出ること(フェーズ最終レビュー carry-forward)。ratio なしでは
    「先行の decimate で処理済みのため decimate は適用されません」となり
    何が無視されたのか読めない。"""
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(
            op="simplify",
            targets=[gid1],
            scope="shared",
            params={"method": "decimate", "ratio": 0.1},
        ),
        Operation(
            op="simplify",
            targets=[gid2],
            scope="shared",
            params={"method": "decimate", "ratio": 0.5},
        ),
    ]
    report = apply_operations(src_path, ops, out_path)

    expected_warning = (
        "共有形状は先行の 間引き(decimate, ratio=0.1) で処理済みのため、"
        f"この要素(GlobalId={gid2})への 間引き(decimate, ratio=0.5) は適用されません"
        "(共有波及の先勝ち)。"
    )
    assert expected_warning in report.warnings


def test_shared_simplify_same_method_same_ratio_stays_silent(tmp_path):
    """同一 (method, ratio) での再到達は従来どおり無言(先勝ち警告を出さない)。
    フェーズ最終レビュー時はレビュアの手動プローブのみだった挙動の番人化。"""
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(
            op="simplify",
            targets=[gid1],
            scope="shared",
            params={"method": "decimate", "ratio": 0.1},
        ),
        Operation(
            op="simplify",
            targets=[gid2],
            scope="shared",
            params={"method": "decimate", "ratio": 0.1},
        ),
    ]
    report = apply_operations(src_path, ops, out_path)

    assert set(report.simplified) == {gid1, gid2}
    assert [w for w in report.warnings if "先勝ち" in w] == []


def test_directly_shared_rep_conflicting_ops_first_come_wins(tmp_path):
    """IfcMappedItem を介さない直接共有でも、同一 rep への2操作は先勝ちで
    dedup され、警告で可視化されること(フェーズ最終レビューI-3の
    carry-forward)。dedup が無いと同じ rep に bbox→decimate が重ねがけ
    され、無警告で幾何が二重に劣化する(2026-08-01 実測)。"""
    f = build_two_elements_sharing_representation_directly_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="simplify", targets=[gid1], scope="shared", params={"method": "bbox"}),
        Operation(
            op="simplify",
            targets=[gid2],
            scope="shared",
            params={"method": "decimate", "ratio": 0.1},
        ),
    ]
    report = apply_operations(src_path, ops, out_path)

    assert set(report.simplified) == {gid1, gid2}
    expected_warning = (
        "共有形状は先行の bbox軽量化(bbox) で処理済みのため、"
        f"この要素(GlobalId={gid2})への 間引き(decimate, ratio=0.1) は適用されません"
        "(共有波及の先勝ち)。"
    )
    assert expected_warning in report.warnings

    reopened = ifcopenshell.open(out_path)
    # 先勝ち: 両要素とも bbox(12三角形)。decimate は重ねがけされていない。
    assert _n_triangles(reopened.by_guid(gid1)) == 12
    assert _n_triangles(reopened.by_guid(gid2)) == 12


def test_exclusively_owned_rep_still_returns_no_shared_key(tmp_path):
    """専有 rep(参照1本)の要素は従来どおり鍵 None(dedup 辞書を太らせず、
    先勝ち警告も出ない)。直接共有フィクスチャの片側を専有化して確認する。"""
    f = build_two_elements_sharing_representation_directly_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    # elem2 を専有の別 rep に付け替える(elem1 の rep は参照1本になる)
    shared_rep = elem1.Representation.Representations[0]
    coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 2.0)],
    )
    tfs = f.create_entity(
        "IfcTriangulatedFaceSet", Coordinates=coord_list, CoordIndex=[(1, 2, 3)]
    )
    own_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=shared_rep.ContextOfItems,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[tfs],
    )
    elem2.Representation = f.create_entity(
        "IfcProductDefinitionShape", Representations=[own_rep]
    )

    from ifc_occam.core.export import _shared_map_key

    assert _shared_map_key(f, elem1) is None
    assert _shared_map_key(f, elem2) is None


# ---------------------------------------------------------------------------
# CF-G Task1: _shared_map_key の鍵をMappedRepresentationのidに統一する
# (CF-C最終レビューI-1: マップidを鍵にすると複数マップ共有・ハイブリッド
# 構成で鍵が分裂し、二重適用が残る)
# ---------------------------------------------------------------------------


def test_shared_map_key_unifies_two_maps_sharing_one_mapped_representation():
    """別々のIfcRepresentationMap経由で同一MappedRepresentationを共有する2要素の
    鍵が一致すること(CF-C最終レビューI-1: マップidを鍵にすると分裂し、
    同じrepがマップの数だけ書き換えられる=二重適用)。鍵は「実際に書き換わる
    rep」= MappingSource.MappedRepresentation の id。"""
    from ifc_occam.core.export import _shared_map_key

    f = build_two_maps_sharing_mapped_representation_ifc()
    e1 = f.by_type("IfcWall")[0]
    e2 = f.by_type("IfcColumn")[0]
    k1 = _shared_map_key(f, e1)
    k2 = _shared_map_key(f, e2)
    assert k1 is not None
    assert k1 == k2
    mapped_rep = (
        e1.Representation.Representations[0].Items[0].MappingSource.MappedRepresentation
    )
    assert k1 == mapped_rep.id()


def test_shared_map_key_unifies_hybrid_direct_and_mapped_reference():
    """同一のIfcShapeRepresentationを直接参照する要素とIfcMappedItem経由で
    参照する要素の鍵が一致すること(ハイブリッド構成。直接側はrep自体のid、
    mapped側もMappedRepresentation=同じrepのidになる)。"""
    from ifc_occam.core.export import _shared_map_key

    f = build_hybrid_direct_and_mapped_share_ifc()
    direct_elem = f.by_type("IfcWall")[0]
    mapped_elem = f.by_type("IfcColumn")[0]
    k_direct = _shared_map_key(f, direct_elem)
    k_mapped = _shared_map_key(f, mapped_elem)
    assert k_direct is not None
    assert k_direct == k_mapped


def test_apply_operations_dedups_across_two_maps_sharing_one_mapped_representation(
    tmp_path,
):
    """2つのマップ経由で同一repを共有する2クラスへ異パラメータのsimplifyを
    適用したとき、鍵統一により後続が先勝ちdedupに掛かり、(i) 先勝ち警告が
    1件出る、(ii) 両要素とも simplified に積まれる、(iii) repのItemsは先行
    パラメータの結果1回分だけになっている、ことを固定する(修正前は両方が
    別鍵で実際に書き換わり警告0件のまま二重適用だった)。"""
    f = build_two_maps_sharing_mapped_representation_ifc()
    elem1 = f.by_type("IfcWall")[0]
    elem2 = f.by_type("IfcColumn")[0]
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="simplify", targets=[gid1], scope="shared", params={"method": "bbox"}),
        Operation(
            op="simplify",
            targets=[gid2],
            scope="shared",
            params={"method": "decimate", "ratio": 0.5},
        ),
    ]
    report = apply_operations(src_path, ops, out_path)

    assert set(report.simplified) == {gid1, gid2}
    expected_warning = (
        "共有形状は先行の bbox軽量化(bbox) で処理済みのため、"
        f"この要素(GlobalId={gid2})への 間引き(decimate, ratio=0.5) は適用されません"
        "(共有波及の先勝ち)。"
    )
    assert expected_warning in report.warnings

    reopened = ifcopenshell.open(out_path)
    # 先勝ち: 両要素ともbbox(12三角形)。decimateは重ねがけされていない
    # (二重適用の不在こそが本丸——鍵統一前は別鍵で警告0件のまま両方が
    # 実際に書き換わっていた)。
    assert _n_triangles(reopened.by_guid(gid1)) == 12
    assert _n_triangles(reopened.by_guid(gid2)) == 12


def test_apply_operations_dedups_hybrid_direct_and_mapped_share(tmp_path):
    """直接参照+mapped参照のハイブリッド構成でも同様に先勝ちdedupが効く
    ことを固定する(修正前は直接側=repのid、mapped側=マップidで鍵が分裂)。"""
    f = build_hybrid_direct_and_mapped_share_ifc()
    elem1 = f.by_type("IfcWall")[0]
    elem2 = f.by_type("IfcColumn")[0]
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="simplify", targets=[gid1], scope="shared", params={"method": "bbox"}),
        Operation(
            op="simplify",
            targets=[gid2],
            scope="shared",
            params={"method": "decimate", "ratio": 0.5},
        ),
    ]
    report = apply_operations(src_path, ops, out_path)

    assert set(report.simplified) == {gid1, gid2}
    expected_warning = (
        "共有形状は先行の bbox軽量化(bbox) で処理済みのため、"
        f"この要素(GlobalId={gid2})への 間引き(decimate, ratio=0.5) は適用されません"
        "(共有波及の先勝ち)。"
    )
    assert expected_warning in report.warnings

    reopened = ifcopenshell.open(out_path)
    assert _n_triangles(reopened.by_guid(gid1)) == 12
    assert _n_triangles(reopened.by_guid(gid2)) == 12


# ---------------------------------------------------------------------------
# Phase G 最終レビューM-1: MappedRepresentation欠落(スキーマ違反)のとき
# _shared_map_key がAttributeErrorでexport全体を中断させないこと
# ---------------------------------------------------------------------------


def _corrupt_first_representation_map_to_null(path: str) -> None:
    """`IFCREPRESENTATIONMAP(#x,#y)` の第2引数(MappedRepresentation)を
    `$` に書き換える(スキーマ違反を再現)。1件目にマッチした行だけを
    書き換える。"""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8")
    new_text, n = re.subn(
        r"(IFCREPRESENTATIONMAP\(#\d+,)#\d+(\);)", r"\1$\2", text, count=1
    )
    assert n == 1, "IFCREPRESENTATIONMAP行が見つからない(フィクスチャ側の変更を確認)"
    with open(path, "wb") as fh:
        fh.write(new_text.encode("utf-8"))


def test_shared_simplify_on_schema_violating_mapped_representation_does_not_abort_export(
    tmp_path,
):
    """MappedRepresentation=$(スキーマ違反)のIfcRepresentationMapを共有する
    要素へ scope="shared" simplify をかけても、export全体が中断せず完走する
    こと(Phase G最終レビューM-1)。

    `_shared_map_key` は mapped分岐で
    `items[0].MappingSource.MappedRepresentation.id()` を直接呼ぶため、
    MappedRepresentationが$(None)のスキーマ違反ファイルではNoneに対して
    `.id()` を呼びAttributeErrorになる。この呼び出しは要素単位の
    try/except(`_apply_simplify`)の外にあるため、1要素の欠陥がexport
    全体を落とす(旧実装は要素skipで完走していた=退行)。
    """
    f = build_two_elements_sharing_mapped_shape_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    gid1, gid2 = _gid(elem1), _gid(elem2)

    src_path = _write_fixture(f, tmp_path)
    _corrupt_first_representation_map_to_null(src_path)

    # 破損後もifcopenshell.openで開けること(STEPレベルでは合法。
    # スキーマの必須属性チェックはopen時に強制されない)を前提として確認する。
    corrupted = ifcopenshell.open(src_path)
    mapped_rep = (
        corrupted.by_guid(gid1)
        .Representation.Representations[0]
        .Items[0]
        .MappingSource.MappedRepresentation
    )
    assert mapped_rep is None

    out_path = str(tmp_path / "out.ifc")
    ops = [
        Operation(
            op="simplify",
            targets=[gid1, gid2],
            scope="shared",
            params={"method": "bbox"},
        )
    ]

    # 中断しないこと(修正前はAttributeErrorがここで飛ぶ=RED)。
    report = apply_operations(src_path, ops, out_path)

    # 共有実体を特定できない=専有扱いとして扱われるため、両要素は
    # スキップされるかそれぞれ個別に処理される。いずれにせよexportは
    # 完走し、GlobalIdの取りこぼしは無い(スキップ扱いなら理由付きで
    # skippedに、成功なら重複なくsimplifiedに積まれる)。
    accounted = set(report.simplified) | {s.global_id for s in report.skipped}
    assert accounted == {gid1, gid2}

    ifcopenshell.open(out_path)  # 出力自体も壊れていないこと


# ---------------------------------------------------------------------------
# simplify中の例外は要素単位でスキップし、export全体は継続すること
# ---------------------------------------------------------------------------


def test_simplify_failure_on_one_element_does_not_abort_export(
    tmp_path, monkeypatch, small_ifc_path
):
    src_copy = tmp_path / "small_src.ifc"
    shutil.copy(small_ifc_path, src_copy)

    model = ifcopenshell.open(str(src_copy))
    products = [
        p for p in model.by_type("IfcProduct") if getattr(p, "Representation", None) is not None
    ]
    target = products[0]
    other = products[1]
    target_gid, other_gid = target.GlobalId, other.GlobalId

    out_path = str(tmp_path / "out.ifc")

    def _boom(*args, **kwargs):
        raise ValueError("boom: degenerate geometry")

    monkeypatch.setattr("ifc_occam.core.export.convex_hull_mesh", _boom)

    ops = [
        Operation(op="simplify", targets=[target_gid], params={"method": "convex_hull"}),
        Operation(op="delete", targets=[other_gid]),
    ]

    report = apply_operations(str(src_copy), ops, out_path)

    assert report.simplified == []
    assert any(item.global_id == target_gid for item in report.skipped)
    assert any(
        "boom" in item.reason for item in report.skipped if item.global_id == target_gid
    )
    assert any("boom" in w for w in report.warnings)
    assert other_gid in report.deleted

    reopened = ifcopenshell.open(out_path)
    with pytest.raises(RuntimeError):
        reopened.by_guid(other_gid)
    assert reopened.by_guid(target_gid) is not None


# ---------------------------------------------------------------------------
# validate_operationsを前段で呼び、未知gidは警告+skippedへ回すこと
# ---------------------------------------------------------------------------


def test_unknown_gid_in_operations_is_skipped_with_warning(tmp_path, small_ifc_path):
    src_copy = tmp_path / "small_src.ifc"
    shutil.copy(small_ifc_path, src_copy)

    model = ifcopenshell.open(str(src_copy))
    target = next(
        p
        for p in model.by_type("IfcProduct")
        if getattr(p, "Representation", None) is not None
    )
    target_gid = target.GlobalId
    unknown_gid = "UNKNOWN_GLOBAL_ID_0000000000"

    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="delete", targets=[unknown_gid]),
        Operation(op="simplify", targets=[target_gid], params={"method": "bbox"}),
    ]

    report = apply_operations(str(src_copy), ops, out_path)

    assert any(unknown_gid in w for w in report.warnings)
    assert any(item.global_id == unknown_gid for item in report.skipped)
    assert report.simplified == [target_gid]

    reopened = ifcopenshell.open(out_path)
    assert reopened.by_guid(target_gid) is not None


# ---------------------------------------------------------------------------
# 出力先の既定改善 (Phase4 Task6-2): 相対パスはsrc_pathのディレクトリ基準で解決し、
# 親ディレクトリが無ければ作成する
# ---------------------------------------------------------------------------


def test_resolve_output_path_relative_resolves_against_src_dir(tmp_path):
    src_path = tmp_path / "sub" / "model.ifc"
    src_path.parent.mkdir(parents=True)
    src_path.write_text("dummy")

    resolved = resolve_output_path(str(src_path), "model_light.ifc")

    assert resolved == (tmp_path / "sub" / "model_light.ifc").resolve()


def test_resolve_output_path_absolute_is_used_as_is(tmp_path):
    src_path = tmp_path / "model.ifc"
    src_path.write_text("dummy")
    abs_out = tmp_path / "elsewhere" / "out.ifc"

    resolved = resolve_output_path(str(src_path), str(abs_out))

    assert resolved == abs_out


def test_export_with_relative_output_path_writes_next_to_source(tmp_path):
    f = build_wall_with_window_ifc()
    src_path = _write_fixture(f, tmp_path, name="model.ifc")

    report = apply_operations(src_path, [], "model_light.ifc")

    expected = str((tmp_path / "model_light.ifc").resolve())
    assert report.output_path == expected
    assert ifcopenshell.open(expected) is not None


def test_export_creates_missing_output_parent_directory(tmp_path):
    f = build_wall_with_window_ifc()
    src_path = _write_fixture(f, tmp_path, name="model.ifc")

    out_path = str(tmp_path / "does_not_exist_yet" / "out.ifc")
    assert not (tmp_path / "does_not_exist_yet").exists()

    report = apply_operations(src_path, [], out_path)

    assert report.output_path == out_path
    assert ifcopenshell.open(out_path) is not None


def test_apply_operations_does_not_mutate_source_file(tmp_path):
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    wall_gid = _gid(wall)
    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    apply_operations(src_path, [Operation(op="delete", targets=[wall_gid])], out_path)

    src_reopened = ifcopenshell.open(src_path)
    assert src_reopened.by_guid(wall_gid) is not None


# ---------------------------------------------------------------------------
# 出力先=入力先の禁止(原本非破壊。フェーズ最終レビューのC1をフルオープン経路へ
# 水平展開したもの。テキスト経路は入力を truncate するが、フルオープン経路は
# 全体をメモリに読んでから書くため truncate はしない——それでも原本を軽量化
# 結果で上書きするのは同じく契約違反なので、両経路で拒否する)
# ---------------------------------------------------------------------------


def test_apply_operations_refuses_output_equal_to_source(tmp_path):
    """出力先が入力ファイルと同一実体なら ValueError で拒否し、原本を
    1バイトも変えないこと。"""
    import hashlib

    f = build_wall_with_window_ifc()
    wall_gid = _gid(f.by_type("IfcWall")[0])
    src_path = _write_fixture(f, tmp_path)
    before_size = Path(src_path).stat().st_size
    before_hash = hashlib.sha256(Path(src_path).read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="同一"):
        apply_operations(src_path, [Operation(op="delete", targets=[wall_gid])], src_path)

    assert Path(src_path).stat().st_size == before_size
    assert hashlib.sha256(Path(src_path).read_bytes()).hexdigest() == before_hash


def test_apply_operations_refuses_output_equal_to_source_before_opening(tmp_path, monkeypatch):
    """拒否はフルオープンより**前**に行うこと(開いて削除まで終えてから
    write 直前で落ちると、その時間が丸ごと無駄になる)。"""
    f = build_wall_with_window_ifc()
    wall_gid = _gid(f.by_type("IfcWall")[0])
    src_path = _write_fixture(f, tmp_path)

    def _boom_open(*a, **kw):
        raise AssertionError("同一パスガードより前に ifcopenshell.open が呼ばれた")

    monkeypatch.setattr(ifcopenshell, "open", _boom_open)

    with pytest.raises(ValueError, match="同一"):
        apply_operations(src_path, [Operation(op="delete", targets=[wall_gid])], src_path)


def test_apply_operations_refuses_output_equal_to_source_via_relative_name(tmp_path):
    """相対パス指定でも拒否すること(`resolve_output_path` は相対パスを入力
    ファイルと同じディレクトリ基準で解決するため、入力ファイル名だけを
    入力すると同一実体になる。GUI の export モーダルはこの運用)。"""
    f = build_wall_with_window_ifc()
    wall_gid = _gid(f.by_type("IfcWall")[0])
    src_path = _write_fixture(f, tmp_path)

    with pytest.raises(ValueError, match="同一"):
        apply_operations(
            src_path, [Operation(op="delete", targets=[wall_gid])], Path(src_path).name
        )


# ---------------------------------------------------------------------------
# consolidate: 出力時共有形状化 (Phase4 Task2)
# ---------------------------------------------------------------------------


def test_export_with_consolidate_true_shares_duplicate_shapes(tmp_path):
    f = build_three_translated_copies_ifc()
    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    # このフィクスチャは頂点数4の極小形状(export配線の検証が目的で、選別ルールの
    # 対象ではない)。consolidate_min_benefit_ratio=0でサイズ選別を無効化する。
    report = apply_operations(
        str(src_path), [], out_path, consolidate=True, consolidate_min_benefit_ratio=0
    )

    assert report.consolidated_groups == 1
    assert report.consolidated_elements == 3

    reopened = ifcopenshell.open(out_path)
    assert len(reopened.by_type("IfcRepresentationMap")) == 1
    assert len(reopened.by_type("IfcTriangulatedFaceSet")) == 1


def test_export_with_consolidate_false_default_leaves_shapes_unshared(tmp_path):
    f = build_three_translated_copies_ifc()
    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    report = apply_operations(str(src_path), [], out_path)

    assert report.consolidated_groups == 0
    assert report.consolidated_elements == 0

    reopened = ifcopenshell.open(out_path)
    assert len(reopened.by_type("IfcRepresentationMap")) == 0
    assert len(reopened.by_type("IfcTriangulatedFaceSet")) == 3


def test_consolidate_excludes_simplified_element_from_its_group(tmp_path):
    """群の1要素をsimplifyで幾何変更すると、その要素は同一性が崩れて群対象外になる
    (残り2要素だけが consolidate される)。"""
    f = build_three_translated_copies_ifc()
    elements = f.by_type("IfcBuildingElementProxy")
    simplified_gid = elements[0].GlobalId

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [
        Operation(op="simplify", targets=[simplified_gid], params={"method": "bbox"}),
    ]
    # このフィクスチャは頂点数4の極小形状(除外機構の検証が目的で、選別ルールの
    # 対象ではない)。consolidate_min_benefit_ratio=0でサイズ選別を無効化する。
    report = apply_operations(
        str(src_path), ops, out_path, consolidate=True, consolidate_min_benefit_ratio=0
    )

    assert report.simplified == [simplified_gid]
    # 残り2要素(元の四面体4三角形のまま)は共有化される
    assert report.consolidated_groups == 1
    assert report.consolidated_elements == 2

    reopened = ifcopenshell.open(out_path)
    assert len(reopened.by_type("IfcRepresentationMap")) == 1

    simplified_element = reopened.by_guid(simplified_gid)
    body_rep = simplified_element.Representation.Representations[0]
    assert body_rep.Items[0].is_a("IfcTriangulatedFaceSet")  # bboxの個別形状(共有化されない)


# ---------------------------------------------------------------------------
# src の多相化 (CUI Phase1 Task5): ifcopenshell.file を直接渡せる・再オープンなし
# (design.md §5-1)。既存の path 経路のテスト(上記すべて)は無変更のまま green。
# ---------------------------------------------------------------------------


def test_apply_operations_accepts_file_object_without_reopening(tmp_path, monkeypatch):
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    wall_gid = _gid(wall)
    out_path = str(tmp_path / "out.ifc")

    def _boom(*args, **kwargs):
        raise AssertionError("ifcopenshell.open は file オブジェクト src では呼ばれてはならない")

    monkeypatch.setattr(ifcopenshell, "open", _boom)

    ops = [Operation(op="delete", targets=[wall_gid])]
    report = apply_operations(f, ops, out_path)

    assert wall_gid in report.deleted
    assert report.output_path == out_path
    assert (tmp_path / "out.ifc").exists()

    # 渡した file オブジェクトそのものが直接変更されている(再オープンしていない証拠)
    with pytest.raises(RuntimeError):
        f.by_guid(wall_gid)


def test_apply_operations_with_file_object_src_resolves_relative_output_against_cwd(
    tmp_path, monkeypatch
):
    """src が file オブジェクトの場合、相対 output_path の基準ディレクトリ(元パス)が
    存在しないため cwd 基準で解決する(Path.resolve() の既定動作と同じ)。"""
    f = build_wall_with_window_ifc()
    monkeypatch.chdir(tmp_path)

    report = apply_operations(f, [], "out_relative.ifc")

    expected = str((tmp_path / "out_relative.ifc").resolve())
    assert report.output_path == expected
    assert ifcopenshell.open(expected) is not None


def test_apply_operations_file_object_src_still_supports_consolidate(tmp_path):
    """src多相化がconsolidate経路(内部でextract_modelを呼ぶ)を壊していないこと。"""
    f = build_three_translated_copies_ifc()
    out_path = str(tmp_path / "out.ifc")

    report = apply_operations(f, [], out_path, consolidate=True, consolidate_min_benefit_ratio=0)

    assert report.consolidated_groups == 1
    assert report.consolidated_elements == 3


# ---------------------------------------------------------------------------
# progress callback (CUI Phase1 Task5, design.md §5-2): 削除・simplifyループ中に
# (stage, done, total) で通知する。既定 None なら無通知(既存呼び出しは無変更)。
# ---------------------------------------------------------------------------


def test_apply_operations_progress_reports_delete_stage_with_cascade(tmp_path):
    f = build_wall_with_window_ifc()
    wall_gid = _gid(f.by_type("IfcWall")[0])
    opening_gid = _gid(f.by_type("IfcOpeningElement")[0])
    window_gid = _gid(f.by_type("IfcWindow")[0])

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    calls, progress = _progress_recorder()
    ops = [Operation(op="delete", targets=[wall_gid])]
    report = apply_operations(src_path, ops, out_path, progress=progress)

    assert set(report.deleted) == {wall_gid, opening_gid, window_gid}

    delete_calls = [c for c in calls if c[0] == "delete"]
    # 直接指定(壁)+連鎖(開口・窓)の3件、すべて total=3 で done は 1..3 を尽くす
    assert len(delete_calls) == 3
    assert all(total == 3 for _stage, _done, total in delete_calls)
    assert sorted(done for _stage, done, _total in delete_calls) == [1, 2, 3]


def test_apply_operations_progress_not_called_when_no_deletes(tmp_path):
    f = build_wall_with_window_ifc()
    wall_gid = _gid(f.by_type("IfcWall")[0])
    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    calls, progress = _progress_recorder()
    ops = [Operation(op="keep", targets=[wall_gid])]
    apply_operations(src_path, ops, out_path, progress=progress)

    assert calls == []


def test_apply_operations_progress_reports_simplify_stage(tmp_path, small_ifc_path):
    src_copy = tmp_path / "small_src.ifc"
    shutil.copy(small_ifc_path, src_copy)

    model = ifcopenshell.open(str(src_copy))
    products = [
        p for p in model.by_type("IfcProduct") if getattr(p, "Representation", None) is not None
    ]
    gid1, gid2 = products[0].GlobalId, products[1].GlobalId
    out_path = str(tmp_path / "out.ifc")

    calls, progress = _progress_recorder()
    ops = [
        Operation(op="simplify", targets=[gid1], params={"method": "bbox"}),
        Operation(op="simplify", targets=[gid2], params={"method": "bbox"}),
    ]
    report = apply_operations(str(src_copy), ops, out_path, progress=progress)

    assert set(report.simplified) == {gid1, gid2}

    simplify_calls = [c for c in calls if c[0] == "simplify"]
    assert len(simplify_calls) == 2
    assert all(total == 2 for _stage, _done, total in simplify_calls)
    assert sorted(done for _stage, done, _total in simplify_calls) == [1, 2]


def test_apply_operations_progress_reports_both_stages_in_one_call(tmp_path):
    """delete対象1件+simplify対象1件を同時に指定すると、両ステージが個別に通知される。"""
    f = build_wall_with_window_ifc()
    member1 = f.by_type("IfcBeam")[0]
    wall_gid = _gid(f.by_type("IfcWall")[0])
    member1_gid = _gid(member1)

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    calls, progress = _progress_recorder()
    ops = [
        Operation(op="delete", targets=[wall_gid]),
        # メンバーはBody表現を持たないため simplify 自体は失敗するが、
        # progressはループに乗った時点(成否を問わず)で呼ばれる契約を確認する。
        Operation(op="simplify", targets=[member1_gid], params={"method": "bbox"}),
    ]
    apply_operations(src_path, ops, out_path, progress=progress)

    stages_seen = {c[0] for c in calls}
    assert stages_seen == {"delete", "simplify"}
    assert [c for c in calls if c[0] == "simplify"] == [("simplify", 1, 1)]


# ---------------------------------------------------------------------------
# 大量削除の高速経路 (_mass_delete, CUI Phase1 Task7 Stage B, docs/plans/2026-07-24-cui-phase1.md Task 7):
# closure確定後の対象件数が _MASS_DELETE_THRESHOLD(=1,000)を超えたら
# ifcopenshell.util.element.batch_remove_deep2/unbatch_remove_deep2 経路が発動する。
# 閾値以下は現行(per-remove)経路のまま変わらない。
# ---------------------------------------------------------------------------


class _InjectedFailure(Exception):
    """テスト専用の注入例外(他所で偶然キャッチされる通常の例外型と混同しないため)。"""


def test_mass_delete_threshold_constant_is_1000():
    """ブリーフの契約値(対象>1,000件で発動)を固定する回帰テスト。"""
    assert export_module._MASS_DELETE_THRESHOLD == 1000


def test_mass_delete_triggers_only_when_closure_exceeds_threshold(tmp_path, monkeypatch):
    """閾値ちょうど(境界値)ではbatch経路は呼ばれず、閾値+1では1回だけ呼ばれる。"""
    threshold = export_module._MASS_DELETE_THRESHOLD

    real_batch = ifcopenshell.util.element.batch_remove_deep2
    real_unbatch = ifcopenshell.util.element.unbatch_remove_deep2
    batch_spy = Mock(side_effect=real_batch)
    unbatch_spy = Mock(side_effect=real_unbatch)
    monkeypatch.setattr(ifcopenshell.util.element, "batch_remove_deep2", batch_spy)
    monkeypatch.setattr(ifcopenshell.util.element, "unbatch_remove_deep2", unbatch_spy)

    f_at = build_many_minimal_products_ifc(n_targets=threshold, n_keep=2)
    at_gids = [e.GlobalId for e in f_at.by_type("IfcBuildingElementProxy")]
    report_at = apply_operations(
        f_at, [Operation(op="delete", targets=at_gids)], str(tmp_path / "out_at.ifc")
    )
    assert set(report_at.deleted) == set(at_gids)
    assert batch_spy.call_count == 0
    assert unbatch_spy.call_count == 0

    f_over = build_many_minimal_products_ifc(n_targets=threshold + 1, n_keep=2)
    over_gids = [e.GlobalId for e in f_over.by_type("IfcBuildingElementProxy")]
    report_over = apply_operations(
        f_over, [Operation(op="delete", targets=over_gids)], str(tmp_path / "out_over.ifc")
    )
    assert set(report_over.deleted) == set(over_gids)
    assert batch_spy.call_count == 1
    assert unbatch_spy.call_count == 1


def test_mass_delete_deletes_all_targets_and_leaves_others_with_no_dangling(tmp_path):
    """閾値超(1,500件)の合成データ: 対象が全て消え、非対象は残り、danglingは0件。"""
    f = build_many_minimal_products_ifc(n_targets=1500, n_keep=5)
    target_gids = {e.GlobalId for e in f.by_type("IfcBuildingElementProxy")}
    keep_gids = {e.GlobalId for e in f.by_type("IfcWall")}
    assert len(target_gids) == 1500
    assert len(keep_gids) == 5

    out_path = str(tmp_path / "out.ifc")
    ops = [Operation(op="delete", targets=sorted(target_gids))]
    report = apply_operations(f, ops, out_path)

    # (a) 対象が全て消える
    assert set(report.deleted) == target_gids

    reopened = ifcopenshell.open(out_path)
    for gid in target_gids:
        with pytest.raises(RuntimeError):
            reopened.by_guid(gid)

    # (c) 非対象が残る
    for gid in keep_gids:
        assert reopened.by_guid(gid) is not None
    assert len(reopened.by_type("IfcWall")) == 5

    # (b) verify_no_dangling == []
    assert verify_no_dangling(reopened, target_gids) == []


def test_mass_delete_calls_unbatch_even_if_loop_raises_mid_delete(tmp_path, monkeypatch):
    """例外経路: ループ中に例外を注入しても unbatch_remove_deep2 は必ず呼ばれる
    (try/finally の証明)。batch状態のまま file オブジェクトが残らないことの安全策。"""
    real_unbatch = ifcopenshell.util.element.unbatch_remove_deep2
    unbatch_spy = Mock(side_effect=real_unbatch)
    monkeypatch.setattr(ifcopenshell.util.element, "unbatch_remove_deep2", unbatch_spy)

    real_run = ifcopenshell.api.run
    call_count = {"n": 0}

    def _flaky_run(usecase_path, *args, **kwargs):
        if usecase_path == "root.remove_product":
            call_count["n"] += 1
            if call_count["n"] == 5:
                raise _InjectedFailure("boom: injected mid-loop failure")
        return real_run(usecase_path, *args, **kwargs)

    monkeypatch.setattr(ifcopenshell.api, "run", _flaky_run)

    f = build_many_minimal_products_ifc(n_targets=1500, n_keep=2)
    target_gids = [e.GlobalId for e in f.by_type("IfcBuildingElementProxy")]
    out_path = str(tmp_path / "out.ifc")

    with pytest.raises(_InjectedFailure):
        apply_operations(f, [Operation(op="delete", targets=target_gids)], out_path)

    assert unbatch_spy.call_count == 1


def test_mass_delete_progress_fires_per_element(tmp_path):
    """batch経路でも progress は要素ごとに発火する(Task5契約の回帰確認)。"""
    n = 1200
    f = build_many_minimal_products_ifc(n_targets=n, n_keep=0)
    target_gids = [e.GlobalId for e in f.by_type("IfcBuildingElementProxy")]
    out_path = str(tmp_path / "out.ifc")

    calls, progress = _progress_recorder()
    ops = [Operation(op="delete", targets=target_gids)]
    report = apply_operations(f, ops, out_path, progress=progress)

    assert set(report.deleted) == set(target_gids)

    delete_calls = [c for c in calls if c[0] == "delete"]
    assert len(delete_calls) == n
    assert all(total == n for _stage, _done, total in delete_calls)
    assert sorted(done for _stage, done, _total in delete_calls) == list(range(1, n + 1))


# ---------------------------------------------------------------------------
# Phase1 final review Finding1: batch経路(_mass_delete) × 削除連鎖の被覆ギャップ。
# 上記の既存batch経路テスト群はすべてbuild_many_minimal_products_ifc(関係を
# 一切持たない最小要素)のみを使っており、連鎖(IfcRelVoidsElement/
# IfcRelFillsElement)を持つ要素をbatch経路に通した検証が無かった。
# ---------------------------------------------------------------------------


def test_mass_delete_batch_path_cascades_relations_and_leaves_no_dangling(tmp_path, monkeypatch):
    """壁クラス全件(閾値超)delete → batch経路発動(既存スパイ手法で確認) →
    (a) 連鎖対象(開口・充填窓・rel レコード)も削除される (b) 出力を
    write→ifcopenshell.open で再読込し verify_no_dangling == [] (c) 非対象
    (keep_class)要素が残存する、をまとめて検証する。"""
    real_batch = ifcopenshell.util.element.batch_remove_deep2
    real_unbatch = ifcopenshell.util.element.unbatch_remove_deep2
    batch_spy = Mock(side_effect=real_batch)
    unbatch_spy = Mock(side_effect=real_unbatch)
    monkeypatch.setattr(ifcopenshell.util.element, "batch_remove_deep2", batch_spy)
    monkeypatch.setattr(ifcopenshell.util.element, "unbatch_remove_deep2", unbatch_spy)

    n_walls = export_module._MASS_DELETE_THRESHOLD + 1  # 1001件、閾値超を保証
    f = build_many_walls_with_openings_ifc(n_walls=n_walls, n_keep=5)

    wall_gids = {e.GlobalId for e in f.by_type("IfcWall")}
    opening_gids = {e.GlobalId for e in f.by_type("IfcOpeningElement")}
    window_gids = {e.GlobalId for e in f.by_type("IfcWindow")}
    keep_gids = {e.GlobalId for e in f.by_type("IfcColumn")}
    assert len(wall_gids) == n_walls
    assert 0 < len(window_gids) < n_walls  # 「一部」が実際に一部(全件でも0件でもない)
    assert len(keep_gids) == 5

    expected_deleted = wall_gids | opening_gids | window_gids
    assert len(expected_deleted) > export_module._MASS_DELETE_THRESHOLD

    out_path = str(tmp_path / "out.ifc")
    ops = [Operation(op="delete", targets=sorted(wall_gids))]
    report = apply_operations(f, ops, out_path)

    # batch経路(_mass_delete)が実際に発動したこと(閾値超であることの直接証拠)
    assert batch_spy.call_count == 1
    assert unbatch_spy.call_count == 1

    # (a) 連鎖対象(開口・充填窓)も削除される
    assert set(report.deleted) == expected_deleted

    reopened = ifcopenshell.open(out_path)
    for gid in expected_deleted:
        with pytest.raises(RuntimeError):
            reopened.by_guid(gid)

    # rel レコードそのものも削除される(壁は全件削除対象のため、これらの関係を
    # 参照している側は1件も残らない)
    assert reopened.by_type("IfcRelVoidsElement") == []
    assert reopened.by_type("IfcRelFillsElement") == []

    # (b) verify_no_dangling == []
    assert verify_no_dangling(reopened, expected_deleted) == []

    # (c) 非対象(keep_class)要素が残存する
    assert len(reopened.by_type("IfcColumn")) == 5
    for gid in keep_gids:
        assert reopened.by_guid(gid) is not None


# ---------------------------------------------------------------------------
# 由来刻印 (CUI Phase2 Task1, docs/plans/2026-07-25-cui-phase2.md): 出力ヘッダの
# FILE_DESCRIPTION.description に非正本マークを3行追記する(既存エントリは保存)。
# あわせて FILE_NAME.originating_system を設定する。IFC2X3/IFC4 両フィクスチャ、
# source_name の明示渡し/既定導出(path→ファイル名 / fileオブジェクト→"(in-memory)")
# の両方を検証する。
# ---------------------------------------------------------------------------


def _expected_stamp_lines(source_name: str, deleted_count: int, simplified_count: int) -> tuple:
    """ブリーフ記載の刻印テンプレートをverbatimで再現する(実装と独立に契約を固定)。"""
    today = datetime.date.today().isoformat()
    return (
        f"Lightweighted by IFC Occam {__version__} on {today}"
        " - non-authoritative derivative; verify against the source model",
        f"Source: {source_name}",
        f"Deleted {deleted_count} elements (incl. cascade); simplified {simplified_count}",
    )


def test_export_stamps_provenance_header_ifc4_and_preserves_existing_description(tmp_path):
    f = build_wall_with_window_ifc()
    wall_gid = _gid(f.by_type("IfcWall")[0])
    existing_description = ("ViewDefinition [CoordinationView]", "Some other pre-existing note")
    f.header.file_description.description = existing_description

    src_path = _write_fixture(f, tmp_path)
    out_path = str(tmp_path / "out.ifc")

    ops = [Operation(op="delete", targets=[wall_gid])]
    report = apply_operations(src_path, ops, out_path)

    reopened = ifcopenshell.open(out_path)
    description = reopened.header.file_description.description

    # (b) 元からあるdescriptionエントリが保存されている(先頭にそのまま残る)
    assert description[: len(existing_description)] == existing_description

    # (a) 由来情報3行が追記されている。(d) source_name未指定→pathの既定導出。
    expected_stamp = _expected_stamp_lines(
        source_name=Path(src_path).name,
        deleted_count=len(report.deleted),
        simplified_count=len(report.simplified),
    )
    assert description[len(existing_description) :] == expected_stamp

    # (c) originating_systemが設定される
    assert reopened.header.file_name.originating_system == f"IFC Occam {__version__}"


def test_export_stamps_provenance_header_ifc2x3_and_preserves_existing_description(tmp_path):
    f = build_ifc2x3_single_element_ifc()
    element_gid = _gid(f.by_type("IfcBuildingElementProxy")[0])
    existing_description = ("ViewDefinition [CoordinationView]",)
    f.header.file_description.description = existing_description

    src_path = _write_fixture(f, tmp_path, name="src_2x3.ifc")
    out_path = str(tmp_path / "out.ifc")

    ops = [Operation(op="delete", targets=[element_gid])]
    report = apply_operations(src_path, ops, out_path)

    reopened = ifcopenshell.open(out_path)
    description = reopened.header.file_description.description

    assert description[: len(existing_description)] == existing_description

    expected_stamp = _expected_stamp_lines(
        source_name=Path(src_path).name,
        deleted_count=len(report.deleted),
        simplified_count=len(report.simplified),
    )
    assert description[len(existing_description) :] == expected_stamp
    assert reopened.header.file_name.originating_system == f"IFC Occam {__version__}"


def test_export_stamps_provenance_header_uses_explicit_source_name_override(tmp_path):
    """(d) source_name明示渡し: pathから導出される元ファイル名ではなく、
    明示的に渡した値がSource:行に使われる。"""
    f = build_wall_with_window_ifc()
    src_path = _write_fixture(f, tmp_path, name="original_model.ifc")
    out_path = str(tmp_path / "out.ifc")

    apply_operations(src_path, [], out_path, source_name="explicit_override_name.ifc")

    reopened = ifcopenshell.open(out_path)
    description = reopened.header.file_description.description
    assert any(d == "Source: explicit_override_name.ifc" for d in description)
    assert not any("original_model.ifc" in d for d in description)


def test_export_stamps_provenance_header_file_object_src_defaults_to_in_memory(tmp_path):
    """(d) source_name未指定でsrcがfileオブジェクトの場合、元ファイル名の手がかりが
    無いため既定は"(in-memory)"になる(CUIはrepl.py側で明示的にsource_nameを渡すため
    この既定に頼らない。GUI/直接呼び出しでfileオブジェクトを渡すケースの保険)。"""
    f = build_wall_with_window_ifc()
    out_path = str(tmp_path / "out.ifc")

    apply_operations(f, [], out_path)

    reopened = ifcopenshell.open(out_path)
    description = reopened.header.file_description.description
    assert any(d == "Source: (in-memory)" for d in description)


def test_export_stamps_provenance_header_round_trips_non_ascii_source_name(tmp_path):
    """回帰ガード(監督者要件、docs/plans/2026-07-25-cui-phase2.md Task 3): `_stamp_provenance` は
    source_name自体のエスケープを一切行わず、ifcopenshellの標準STEPエスケープ
    (\\X2\\...\\X0\\)に委ねる設計(export.py の `_stamp_provenance` docstring
    参照)。日本語を含むsource_name(例: 図面データ.ifc)を渡しても、出力を
    ifcopenshell.open で再オープンした際に header の Source: 行が元の文字列
    そのままに復元されることを確認する — ifcopenshell側のSTEPエスケープ挙動が
    将来変わった場合の検知網(このテストのために実装変更を行うことは想定していない)。
    """
    f = build_wall_with_window_ifc()
    src_path = _write_fixture(f, tmp_path, name="original.ifc")
    out_path = str(tmp_path / "out.ifc")
    non_ascii_source_name = "図面データ.ifc"

    apply_operations(src_path, [], out_path, source_name=non_ascii_source_name)

    reopened = ifcopenshell.open(out_path)
    description = reopened.header.file_description.description
    assert any(d == f"Source: {non_ascii_source_name}" for d in description)


# ---------------------------------------------------------------------------
# carry-forward Phase I Task2: inline掃除のバッチ化撤去(残置の根本解消)。
# 旧 `_SIMPLIFY_BATCH_THRESHOLD`(=100件)を超える件数のscope="shared"
# simplifyをバッチ経路(batch_remove_deep2/unbatch_remove_deep2)に通すと、
# 複数の異なる旧サブツリーが下位で共有する末端ジオメトリ(ここでは
# IfcCartesianPointList3D)が、どの呼び出しからも「他グループの旧サブツリー
# からまだ参照されている」と誤判定され、誰の to_delete にも積まれず残置する
# (.superpowers/sdd/cfi-probe-report.md A節、根本原因・実測。バッチ回避で
# gcとビット完全一致することを確認済み)。閾値定数は撤去対象のため、旧値+1の
# 件数をリテラルで固定する。
# ---------------------------------------------------------------------------


def _build_many_shared_leaf_groups_ifc(n: int) -> tuple[ifcopenshell.file, list[str]]:
    """n個の独立した共有マップグループを持つ合成IFC4を返す((file, gidリスト))。

    各グループは自分専用の IfcRepresentationMap を持つため `_shared_map_key` の
    鍵が全グループで異なり、dedupされず n 件とも個別に scope="shared" の
    in-place書き換え対象になる(probeの178件中73件の異なるMappedRepresentation
    に相当)。ただし全グループの IfcTriangulatedFaceSet は"1つの共通の
    IfcCartesianPointList3D"を末端として共有する(probeの
    IfcCartesianPoint/IfcDirection/IfcAxis2Placement3D共有の最小再現)。
    """
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        f,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=ctx,
    )

    shared_coord_list = f.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0)],
    )

    gids: list[str] = []
    for i in range(n):
        tfs = f.create_entity(
            "IfcTriangulatedFaceSet",
            Coordinates=shared_coord_list,
            CoordIndex=[(1, 2, 3)],
        )
        mapped_representation = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="Tessellation",
            Items=[tfs],
        )
        identity = f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        )
        rep_map = f.create_entity(
            "IfcRepresentationMap",
            MappingOrigin=identity,
            MappedRepresentation=mapped_representation,
        )
        element = ifcopenshell.api.run(
            "root.create_entity", f, ifc_class="IfcBuildingElementProxy", name=f"E{i}"
        )
        mapped_item = f.create_entity(
            "IfcMappedItem",
            MappingSource=rep_map,
            MappingTarget=f.create_entity(
                "IfcCartesianTransformationOperator3D",
                Axis1=None,
                Axis2=None,
                LocalOrigin=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
                Scale=None,
                Axis3=None,
            ),
        )
        body_rep = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="MappedRepresentation",
            Items=[mapped_item],
        )
        element.Representation = f.create_entity(
            "IfcProductDefinitionShape", Representations=[body_rep]
        )
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=element)
        gids.append(element.GlobalId)

    return f, gids


def test_inline_shared_simplify_over_batch_threshold_matches_gc_data_lines(tmp_path):
    """旧閾値(100件)を超えるscope="shared" simplifyをgeometry_cleanup="inline"/
    "gc"それぞれで実行すると、出力のDATAレコード集合が完全一致する(バッチ化
    撤去前は、inline側だけ共有末端(IfcCartesianPointList3D)が残置してRED)。"""
    from ifc_occam.scan.reader import iter_records

    n = 101  # 旧 _SIMPLIFY_BATCH_THRESHOLD(=100)超を保証する件数(リテラル固定)
    f, gids = _build_many_shared_leaf_groups_ifc(n)
    src_path = _write_fixture(f, tmp_path)

    ops = [Operation(op="simplify", targets=gids, scope="shared", params={"method": "bbox"})]

    out_gc = str(tmp_path / "out_gc.ifc")
    out_inline = str(tmp_path / "out_inline.ifc")
    report_gc = apply_operations(src_path, ops, out_gc, geometry_cleanup="gc")
    report_inline = apply_operations(src_path, ops, out_inline, geometry_cleanup="inline")

    assert set(report_gc.simplified) == set(gids)
    assert set(report_inline.simplified) == set(gids)

    gc_records = set(iter_records(out_gc))
    inline_records = set(iter_records(out_inline))
    assert inline_records == gc_records


def test_inline_shared_simplify_over_batch_threshold_matches_gc_byte_for_byte(tmp_path):
    """Phase I Task2(inlineバッチ化撤去)+Task3(rewrite.pyのCRLF統一)が
    両方入ると、同一入力・同一simplify操作なら geometry_cleanup="gc" と
    "inline" の出力ファイルはsha256完全一致になる(刻印は
    build_provenance_lines が日付単位でしか変わらないため、同日実行なら
    同一になる)。Task3未実装(rewrite.pyがDATA本体をLFで書く)の間は、
    gc経路(rewrite_without経由)だけ改行がLFになり、inline経路(常時
    ifcopenshell.file.write=CRLF)と食い違うためRED。"""
    import hashlib

    from ifc_occam.scan.reader import iter_records

    n = 101  # 旧 _SIMPLIFY_BATCH_THRESHOLD(=100)超を保証する件数(リテラル固定)
    f, gids = _build_many_shared_leaf_groups_ifc(n)
    src_path = _write_fixture(f, tmp_path)

    ops = [Operation(op="simplify", targets=gids, scope="shared", params={"method": "bbox"})]

    out_gc = str(tmp_path / "out_gc2.ifc")
    out_inline = str(tmp_path / "out_inline2.ifc")
    apply_operations(src_path, ops, out_gc, geometry_cleanup="gc")
    apply_operations(src_path, ops, out_inline, geometry_cleanup="inline")

    # 前提(Task2成果): レコード集合そのものは既に一致しているはず。
    assert set(iter_records(out_gc)) == set(iter_records(out_inline))

    gc_hash = hashlib.sha256(Path(out_gc).read_bytes()).hexdigest()
    inline_hash = hashlib.sha256(Path(out_inline).read_bytes()).hexdigest()
    assert gc_hash == inline_hash


# ---------------------------------------------------------------------------
# carry-forward Phase L: consolidate=ON 出力の実行ごと非決定性(決定性の番人)。
# フェーズ最終レビュー I-1(.superpowers/sdd/cfi-phase-final-review.md)の実測:
# extract_model のマルチスレッドgeometry iteratorがshapes dictへの挿入順を
# 実行ごとに変え、find_duplicatesのバケツ内代表選択がその順序に依存するため、
# consolidateが「どの形状を共有先に選ぶか」が実行ごとに揺れてsha256が一致しない
# (86件のIFCSHAPEREPRESENTATIONが別のIfcMappedItemを共有先に選ぶ、等価だが
# 再現性がない)。
# ---------------------------------------------------------------------------


def test_consolidate_output_is_deterministic_across_reopened_runs_synthetic(tmp_path):
    """同一入力・同一操作(consolidate=ON、simplifyなし)でapply_operationsを
    毎回ファイルから開き直して2回実行すると、出力のsha256が一致すること。

    build_n_translated_copies_ifcは各メンバーが自分専用のBody representation
    (別entity)を持つ平行移動コピーで、抽出後は全員が1つの重複群に入る
    (tests/test_consolidate.pyの選別ルールテストで使っているのと同じフィクスチャ)。
    どのメンバーが共有先(代表)に選ばれるかはfind_duplicatesのバケツ内到着順に
    依存するため、この構成自体がフェーズ最終レビューI-1の再現条件そのもの。
    min_benefit_ratio=0でサイズ選別を無効化し、機構だけを見る。"""
    import hashlib

    f = build_n_translated_copies_ifc(n_members=30, n_verts=4)
    src_path = _write_fixture(f, tmp_path)

    hashes = []
    for i in range(2):
        out_path = str(tmp_path / f"out_{i}.ifc")
        apply_operations(
            src_path, [], out_path, consolidate=True, consolidate_min_benefit_ratio=0
        )
        hashes.append(hashlib.sha256(Path(out_path).read_bytes()).hexdigest())

    assert hashes[0] == hashes[1]


def test_consolidate_output_is_deterministic_across_reopened_runs_real_data(
    small_ifc_path, tmp_path
):
    """small.ifc(実データ)でも同じ決定性を確認する(合成フィクスチャでは非決定性が
    顕在化しない環境があり得るため、フェーズ最終レビューI-1が実際に反証した条件
    ―real dataでのconsolidate=ON二重実行―を直接踏む)。small.ifcが無ければ
    (conftestのskip設計に従い)スキップする。原本は読み取り専用: tmp_pathへ
    コピーしたものに対して実行する。"""
    import hashlib
    import shutil

    src_path = tmp_path / "small.ifc"
    shutil.copy(small_ifc_path, src_path)

    hashes = []
    for i in range(2):
        out_path = str(tmp_path / f"out_{i}.ifc")
        apply_operations(str(src_path), [], out_path, consolidate=True)
        hashes.append(hashlib.sha256(Path(out_path).read_bytes()).hexdigest())

    assert hashes[0] == hashes[1]
