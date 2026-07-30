"""書き出し時GC(textops/gc.py)のテスト。

simplify が孤児化させた旧形状を、書き出したファイルへの mark-and-sweep で
一括除去する経路(docs/plans/2026-07-30-simplify-cleanup-speedup.md)。
要素ごとの remove_deep2 は donuts 族データで47秒/要素かかるため、
このGC(約80秒の固定費、要素数非依存)が既定の掃除になる。
"""

import numpy as np
import pytest

import ifcopenshell

from ifc_occam.scan.fullgraph import FullGraph, scan_full_graph
from ifc_occam.textops.gc import GcReport, _mark_reachable, gc_rewrite
from ifc_occam.textops.plan import TextDeletePlan
from ifc_occam.textops.rewrite import rewrite_without
from tests.fixtures_ifc import (
    attach_layer_assignment,
    build_single_consumer_mapped_child_styled_brep_ifc,
)


def test_rewrite_without_can_skip_header_stamping(tmp_path):
    """stamp_header_lines=False でヘッダが素通しになる(GCはfat側で刻印済みの
    ファイルを扱うため、二重刻印になってはならない)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    src = tmp_path / "src.ifc"
    out = tmp_path / "out.ifc"
    f.write(str(src))

    graph = scan_full_graph(str(src))
    plan = TextDeletePlan(
        drop_ids=np.empty(0, dtype=np.int64),
        patch_rel_ids=np.empty(0, dtype=np.int64),
        stats={"seeds": 0, "cascade": 0, "swept": 0, "rels_dropped": 0, "rels_patched": 0},
    )
    rewrite_without(str(src), str(out), plan, graph, "src.ifc", stamp_header_lines=False)

    text = out.read_text(encoding="utf-8")
    assert "IFC Occam" not in text  # 刻印文言が一切足されていない


def _write_layered_orphan_fixture(tmp_path):
    """donuts 構造(専有マップ+rep直付けPLA)を作り、要素のPDSから旧repを
    切り離して孤児化させた状態で書き出す(simplifyが残すゴミの最小再現)。
    戻り値: (fatパス, 孤児化した旧rep id, 新repを持たない素の要素)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    old_rep_id = body_rep.id()
    attach_layer_assignment(f, [body_rep])

    # 新しい空repに差し替え、旧repを孤児化させる(PLAは新repへ引き継ぎ)
    new_rep = f.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_rep.ContextOfItems,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=[
            f.create_entity(
                "IfcTriangulatedFaceSet",
                Coordinates=f.create_entity(
                    "IfcCartesianPointList3D",
                    CoordList=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                ),
                CoordIndex=[(1, 2, 3)],
            )
        ],
    )
    element.Representation.Representations = [new_rep]
    la = f.by_type("IfcPresentationLayerAssignment")[0]
    la.AssignedItems = [new_rep]
    # 旧repの中の styled item も新アイテムへ付け替え(simplify後の状態を再現)
    styled = f.by_type("IfcStyledItem")[0]
    styled.Item = new_rep.Items[0]

    fat = tmp_path / "fat.ifc"
    f.write(str(fat))
    return fat, old_rep_id


def test_gc_drops_exactly_the_orphan_subtree(tmp_path):
    """孤児化した旧rep(とその配下のマップ・brep・面・点)だけが消え、
    生きている要素・PLA・新形状は残る。"""
    fat, old_rep_id = _write_layered_orphan_fixture(tmp_path)
    out = tmp_path / "out.ifc"

    report = gc_rewrite(fat, out, [old_rep_id], "src.ifc")

    assert report.aborted is False
    assert report.doomed_survivors == []
    assert report.records_dropped > 0
    f2 = ifcopenshell.open(str(out))
    assert f2.by_type("IfcFacetedBrep") == []
    assert f2.by_type("IfcRepresentationMap") == []
    assert len(f2.by_type("IfcTriangulatedFaceSet")) == 1
    assert len(f2.by_type("IfcShapeRepresentation")) == 1
    assert len(f2.by_type("IfcPresentationLayerAssignment")) == 1
    assert len(f2.by_type("IfcBuildingElementProxy")) == 1


def test_gc_reports_a_doomed_root_something_still_references(tmp_path):
    """doomed root がまだ参照されている場合、削除せず参照元クラスつきで報告する
    (前フェーズの残置警告と同じ検出線)。"""
    f = build_single_consumer_mapped_child_styled_brep_ifc()
    element = f.by_type("IfcBuildingElementProxy")[0]
    body_rep = element.Representation.Representations[0]
    old_rep_id = body_rep.id()
    # PLA が旧repを掴んだまま(引き継ぎ忘れの再現)
    attach_layer_assignment(f, [body_rep])
    fat = tmp_path / "fat.ifc"
    f.write(str(fat))
    out = tmp_path / "out.ifc"

    report = gc_rewrite(fat, out, [old_rep_id], "src.ifc")

    assert report.aborted is False
    assert len(report.doomed_survivors) == 1
    record_id, class_name, referrers = report.doomed_survivors[0]
    assert record_id == old_rep_id
    assert class_name == "IFCSHAPEREPRESENTATION"
    assert "IFCPRESENTATIONLAYERASSIGNMENT" in referrers
    # 参照されているものは消さない
    f2 = ifcopenshell.open(str(out))
    assert len(f2.by_type("IfcShapeRepresentation")) == 2


def test_mark_reachable_on_a_hand_built_graph():
    """mark の中身を手組みグラフで固定する: 0番(root)→1→2、3(doomed root)→4、
    5(doomed rootだが 0 から参照される=生き残り)。"""
    ids = np.array([10, 20, 30, 40, 50, 60], dtype=np.int64)
    #        row0    row1  row2  row3    row4  row5
    # refs:  [1, 5]  [2]   []    [4]     []    []
    ref_targets = np.array([1, 5, 2, 4], dtype=np.int64)
    ref_indptr = np.array([0, 2, 3, 3, 4, 4, 4], dtype=np.int64)
    in_degree = np.array([0, 1, 1, 0, 1, 1], dtype=np.int64)
    graph = FullGraph(
        ids=ids,
        class_codes=np.zeros(6, dtype=np.int32),
        class_table=["IFCX"],
        ref_indptr=ref_indptr,
        ref_targets=ref_targets,
        in_degree=in_degree,
        record_count=6,
    )

    keep = _mark_reachable(graph, [40, 60])

    assert keep.tolist() == [True, True, True, False, False, True]


def test_kept_to_dropped_violations_counts_cross_references():
    """安全弁の単体固定: keep側からdrop側への参照を数える(手組みグラフ+
    手組みkeepマスク。markの結果からは原理上作れない形なので直接与える)。
    レビュー指摘(GC Task1 Important-1): この関数を「常に0」に改竄しても
    どのテストも赤くならなかったため、退行検知線として追加。"""
    from ifc_occam.textops.gc import _kept_to_dropped_violations

    ids = np.array([10, 20, 30], dtype=np.int64)
    # row0 -> row1, row2 / row1 -> row2 / row2 -> (なし)
    ref_targets = np.array([1, 2, 2], dtype=np.int64)
    ref_indptr = np.array([0, 2, 3, 3], dtype=np.int64)
    graph = FullGraph(
        ids=ids,
        class_codes=np.zeros(3, dtype=np.int32),
        class_table=["IFCX"],
        ref_indptr=ref_indptr,
        ref_targets=ref_targets,
        in_degree=np.array([0, 1, 2], dtype=np.int64),
        record_count=3,
    )

    keep = np.array([True, True, False])
    assert _kept_to_dropped_violations(graph, keep) == 2  # row0->row2 と row1->row2

    keep_all = np.array([True, True, True])
    assert _kept_to_dropped_violations(graph, keep_all) == 0


def test_gc_rewrite_aborts_and_keeps_the_fat_file_on_violation(tmp_path, monkeypatch):
    """安全検査が違反を報告したらGCを中止し、fat(ゴミ込み)をそのまま出力へ
    移して aborted=True を返す(dangling を作るくらいなら太い方がまし)。
    実グラフでは原理上違反が作れないため、検査関数を monkeypatch で違反に
    差し替えて中止経路そのものを固定する。"""
    import ifc_occam.textops.gc as gc_mod

    fat, old_rep_id = _write_layered_orphan_fixture(tmp_path)
    fat_bytes = fat.read_bytes()
    out = tmp_path / "out.ifc"
    monkeypatch.setattr(gc_mod, "_kept_to_dropped_violations", lambda graph, keep: 1)

    report = gc_mod.gc_rewrite(fat, out, [old_rep_id], "src.ifc")

    assert report.aborted is True
    assert report.records_dropped == 0
    assert report.doomed_survivors == []
    assert out.read_bytes() == fat_bytes  # fat がそのまま出力になっている
    assert not fat.exists()  # move なので元の場所には残らない
