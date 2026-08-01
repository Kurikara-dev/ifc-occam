"""サーバAPI 追加分のテスト (phase3 plan Task5)。

extract_model/open_ifc_file/apply_operations/compute_delete_closure/
count_shared_elements を monkeypatch し、実IFCなしで高速に検証する。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import ifc_occam.server.app as app_module
from ifc_occam.core.cascade import CascadeItem, DeleteClosure
from ifc_occam.core.export import ExportReport, SkippedItem
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo

FAKE_IFC_FILE = object()


def _synthetic_model() -> tuple[ModelData, list[str]]:
    tet_f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
    v = np.eye(4, 3)
    # s1/s2 は幾何が同一(重複) -> duplicate_groups に1件入る。s3 は単独。
    shapes = {
        "s1": ShapeInfo("s1", v, tet_f),
        "s2": ShapeInfo("s2", v, tet_f),
        "s3": ShapeInfo("s3", v * 5.0, tet_f),
    }
    identity = np.eye(4)
    elements = [
        ElementInfo("G1", "IfcWall", "Wall-1", "s1", False, ("SweptSolid",), "Layer-A", placement=identity),
        ElementInfo("G2", "IfcWall", "Wall-2", "s2", False, ("SweptSolid",), "Layer-A", placement=identity),
        ElementInfo("G3", "IfcDoor", "Door-1", "s3", False, ("SweptSolid",), "Layer-B", placement=identity),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)
    return model, ["warn-1"]


def _wait_for_status(client: TestClient, target_states: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get("/api/status").json()
        if last["state"] in target_states:
            return last
        time.sleep(0.02)
    raise TimeoutError(f"status did not reach {target_states} within {timeout}s: last={last}")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: FAKE_IFC_FILE)
    monkeypatch.setattr(app_module, "extract_model", lambda source: _synthetic_model())
    app = app_module.create_app()
    with TestClient(app) as c:
        yield c


def _load_and_wait(client: TestClient) -> None:
    resp = client.post("/api/load", json={"path": "dummy.ifc"})
    assert resp.status_code == 202
    status = _wait_for_status(client, {"ready", "error"})
    assert status["state"] == "ready"


# --- ops roundtrip ---------------------------------------------------------


def test_ops_get_empty_before_any_post(client):
    _load_and_wait(client)
    resp = client.get("/api/ops")
    assert resp.status_code == 200
    assert resp.json() == {"operations": []}


def test_ops_post_then_get_roundtrip(client):
    _load_and_wait(client)
    body = {
        "operations": [
            {"op": "delete", "targets": ["G1"], "scope": "element", "params": {}},
            {"op": "simplify", "targets": ["G3"], "scope": "shared", "params": {"method": "bbox"}},
        ]
    }
    post_resp = client.post("/api/ops", json=body)
    assert post_resp.status_code == 200
    assert post_resp.json()["warnings"] == []

    get_resp = client.get("/api/ops")
    assert get_resp.status_code == 200
    assert get_resp.json() == {"operations": body["operations"]}


def test_ops_post_with_unknown_gid_returns_warning_but_stores(client):
    _load_and_wait(client)
    body = {"operations": [{"op": "delete", "targets": ["G-UNKNOWN"], "scope": "element", "params": {}}]}
    resp = client.post("/api/ops", json=body)
    assert resp.status_code == 200
    assert len(resp.json()["warnings"]) == 1

    get_resp = client.get("/api/ops")
    assert get_resp.json()["operations"] == body["operations"]


def test_ops_post_before_model_ready_returns_409(client):
    resp = client.post("/api/ops", json={"operations": []})
    assert resp.status_code == 409


def test_ops_post_malformed_op_returns_422(client):
    _load_and_wait(client)
    resp = client.post(
        "/api/ops",
        json={"operations": [{"op": "not-a-real-op", "targets": ["G1"]}]},
    )
    assert resp.status_code == 422


def test_ops_post_malformed_scope_returns_422(client):
    _load_and_wait(client)
    resp = client.post(
        "/api/ops",
        json={"operations": [{"op": "delete", "targets": ["G1"], "scope": "not-a-real-scope"}]},
    )
    assert resp.status_code == 422


# --- preview-delete ---------------------------------------------------------


def test_preview_delete_returns_cascade_result(client, monkeypatch):
    _load_and_wait(client)

    captured = {}

    def fake_closure(ifc_file, gids):
        captured["ifc_file"] = ifc_file
        captured["gids"] = gids
        return DeleteClosure(
            direct=list(gids),
            cascaded=[CascadeItem(global_id="G-OPEN", ifc_class="IfcOpeningElement", name=None, reason="開口")],
            all_gids=set(gids) | {"G-OPEN"},
        )

    monkeypatch.setattr(app_module, "compute_delete_closure", fake_closure)

    resp = client.post("/api/ops/preview-delete", json={"targets": ["G1"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["direct"] == 1
    assert body["total"] == 2
    assert body["cascaded"] == [
        {"global_id": "G-OPEN", "ifc_class": "IfcOpeningElement", "name": None, "reason": "開口"}
    ]
    assert captured["ifc_file"] is FAKE_IFC_FILE
    assert captured["gids"] == ["G1"]


def test_preview_delete_surfaces_keep_overridden_by_cascade(client, monkeypatch):
    """連鎖削除はkeep指定に優先するが、上書きされるkeep対象をプレビューで明示する
    こと(Final Review Fix2)。effective(現在保存中の操作リスト)でop=="keep"の
    連鎖メンバーだけを keep_overridden に含める。"""
    _load_and_wait(client)

    def fake_closure(ifc_file, gids):
        return DeleteClosure(
            direct=list(gids),
            cascaded=[
                CascadeItem(global_id="G-OPEN", ifc_class="IfcOpeningElement", name=None, reason="開口"),
                CascadeItem(global_id="G2", ifc_class="IfcWindow", name="Win-1", reason="開口の充填要素"),
            ],
            all_gids=set(gids) | {"G-OPEN", "G2"},
        )

    monkeypatch.setattr(app_module, "compute_delete_closure", fake_closure)

    # G2 (窓) に keep を明示指定済み。G-OPEN には何も指定なし。
    ops_resp = client.post(
        "/api/ops",
        json={"operations": [{"op": "keep", "targets": ["G2"], "scope": "element", "params": {}}]},
    )
    assert ops_resp.status_code == 200

    resp = client.post("/api/ops/preview-delete", json={"targets": ["G1"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["keep_overridden"] == [
        {"global_id": "G2", "ifc_class": "IfcWindow", "name": "Win-1"}
    ]


def test_preview_delete_keep_overridden_empty_when_no_stored_keep(client, monkeypatch):
    _load_and_wait(client)

    def fake_closure(ifc_file, gids):
        return DeleteClosure(
            direct=list(gids),
            cascaded=[CascadeItem(global_id="G-OPEN", ifc_class="IfcOpeningElement", name=None, reason="開口")],
            all_gids=set(gids) | {"G-OPEN"},
        )

    monkeypatch.setattr(app_module, "compute_delete_closure", fake_closure)

    resp = client.post("/api/ops/preview-delete", json={"targets": ["G1"]})
    assert resp.status_code == 200
    assert resp.json()["keep_overridden"] == []


def test_preview_delete_unknown_gid_returns_400(client, monkeypatch):
    _load_and_wait(client)
    called = {"n": 0}
    monkeypatch.setattr(
        app_module,
        "compute_delete_closure",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    resp = client.post("/api/ops/preview-delete", json={"targets": ["G-UNKNOWN"]})
    assert resp.status_code == 400
    assert called["n"] == 0


def test_preview_delete_before_ready_returns_409(client):
    resp = client.post("/api/ops/preview-delete", json={"targets": ["G1"]})
    assert resp.status_code == 409


# --- sharing -----------------------------------------------------------------


def test_sharing_returns_count_shared_elements(client, monkeypatch):
    _load_and_wait(client)

    captured = {}

    def fake_count(ifc_file, gid):
        captured["ifc_file"] = ifc_file
        captured["gid"] = gid
        return 2

    monkeypatch.setattr(app_module, "count_shared_elements", fake_count)

    resp = client.get("/api/element/G1/sharing")
    assert resp.status_code == 200
    assert resp.json() == {"shared_count": 2}
    assert captured["ifc_file"] is FAKE_IFC_FILE
    assert captured["gid"] == "G1"


def test_sharing_unknown_gid_returns_404(client, monkeypatch):
    _load_and_wait(client)
    monkeypatch.setattr(app_module, "count_shared_elements", lambda *a, **k: 0)
    resp = client.get("/api/element/G-UNKNOWN/sharing")
    assert resp.status_code == 404


def test_sharing_before_ready_returns_409(client):
    resp = client.get("/api/element/G1/sharing")
    assert resp.status_code == 409


# --- sharing batch -----------------------------------------------------------


def test_sharing_batch_returns_counts_for_known_and_unknown(client, monkeypatch):
    _load_and_wait(client)

    captured = []

    def fake_count(ifc_file, gid):
        captured.append(gid)
        assert ifc_file is FAKE_IFC_FILE
        return {"G1": 3, "G2": 1}.get(gid, 0)

    def fake_siblings(ifc_file, gid):
        assert ifc_file is FAKE_IFC_FILE
        return {"G1": ["G2", "G4"], "G2": ["G1"]}.get(gid, [])

    monkeypatch.setattr(app_module, "count_shared_elements", fake_count)
    monkeypatch.setattr(app_module, "get_shared_element_gids", fake_siblings)

    resp = client.post("/api/elements/sharing", json={"gids": ["G1", "G2", "G-UNKNOWN"]})
    assert resp.status_code == 200
    assert resp.json() == {
        "counts": {"G1": 3, "G2": 1, "G-UNKNOWN": 0},
        "siblings": {"G1": ["G2", "G4"], "G2": ["G1"], "G-UNKNOWN": []},
    }
    # 未知gidは count_shared_elements/get_shared_element_gids を呼ばずcount=0/siblings=[]で返す
    assert captured == ["G1", "G2"]


def test_sharing_batch_empty_gids_returns_empty_counts(client):
    _load_and_wait(client)
    resp = client.post("/api/elements/sharing", json={"gids": []})
    assert resp.status_code == 200
    assert resp.json() == {"counts": {}, "siblings": {}}


def test_sharing_batch_before_ready_returns_409(client):
    resp = client.post("/api/elements/sharing", json={"gids": ["G1"]})
    assert resp.status_code == 409


# --- export ------------------------------------------------------------------


def test_export_202_then_ready_with_result(client, monkeypatch):
    _load_and_wait(client)

    def fake_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        return ExportReport(
            deleted=["G1"],
            simplified=["G3"],
            skipped=[SkippedItem(global_id="G9", reason="not found")],
            warnings=["some warning"],
            output_path=str(output_path),
            consolidated_groups=2,
            consolidated_elements=5,
        )

    monkeypatch.setattr(app_module, "apply_operations", fake_apply)

    resp = client.post("/api/export", json={"output_path": "out.ifc"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "exporting"

    status = _wait_for_status(client, {"ready", "error"})
    assert status["state"] == "ready"
    result = status["export_result"]
    assert result["deleted"] == 1
    assert result["simplified"] == 1
    assert result["skipped"] == 1
    assert result["output_path"] == "out.ifc"
    assert result["warnings"] == ["some warning"]
    assert result["consolidated_groups"] == 2
    assert result["consolidated_elements"] == 5


def test_export_result_includes_stage_seconds(client, monkeypatch):
    """export_result に stage_seconds (既知のステージキー) が含まれること(phase4 Task5)。"""
    _load_and_wait(client)

    def fake_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        return ExportReport(
            deleted=["G1"],
            simplified=["G3"],
            output_path=str(output_path),
            stage_seconds={
                "open": 0.1,
                "deletes": 0.2,
                "simplify": 0.3,
                "reextract_duplicates": 0.4,
                "consolidate": 0.5,
                "write": 0.6,
            },
        )

    monkeypatch.setattr(app_module, "apply_operations", fake_apply)

    resp = client.post("/api/export", json={"output_path": "out.ifc"})
    assert resp.status_code == 202

    status = _wait_for_status(client, {"ready", "error"})
    assert status["state"] == "ready"
    stage_seconds = status["export_result"]["stage_seconds"]
    for key in (
        "open",
        "deletes",
        "simplify",
        "reextract_duplicates",
        "consolidate",
        "write",
    ):
        assert key in stage_seconds


def test_export_consolidate_defaults_false_and_is_forwarded(client, monkeypatch):
    """POST /api/export の consolidate は既定 false で、apply_operations に渡されること。"""
    _load_and_wait(client)

    received = {}

    def fake_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        received["consolidate"] = consolidate
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", fake_apply)

    resp = client.post("/api/export", json={"output_path": "out.ifc"})
    assert resp.status_code == 202
    _wait_for_status(client, {"ready", "error"})
    assert received["consolidate"] is False


def test_export_consolidate_false_is_forwarded(client, monkeypatch):
    _load_and_wait(client)

    received = {}

    def fake_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        received["consolidate"] = consolidate
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", fake_apply)

    resp = client.post("/api/export", json={"output_path": "out.ifc", "consolidate": False})
    assert resp.status_code == 202
    _wait_for_status(client, {"ready", "error"})
    assert received["consolidate"] is False


def test_export_geometry_cleanup_defaults_gc_and_is_forwarded(client, monkeypatch):
    """POST /api/export の geometry_cleanup は既定 "gc" で、apply_operations に
    渡されること(GUI省メモリ書き出し、carry-forward Phase D)。"""
    _load_and_wait(client)

    received = {}

    def fake_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        received["geometry_cleanup"] = geometry_cleanup
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", fake_apply)

    resp = client.post("/api/export", json={"output_path": "out.ifc"})
    assert resp.status_code == 202
    _wait_for_status(client, {"ready", "error"})
    assert received["geometry_cleanup"] == "gc"


def test_export_geometry_cleanup_inline_is_forwarded(client, monkeypatch):
    _load_and_wait(client)

    received = {}

    def fake_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        received["geometry_cleanup"] = geometry_cleanup
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", fake_apply)

    resp = client.post(
        "/api/export", json={"output_path": "out.ifc", "geometry_cleanup": "inline"}
    )
    assert resp.status_code == 202
    _wait_for_status(client, {"ready", "error"})
    assert received["geometry_cleanup"] == "inline"


def test_export_geometry_cleanup_rejects_unknown_value(client, monkeypatch):
    """"gc"/"inline" 以外は pydantic バリデーションで 422 になり、
    apply_operations は一度も呼ばれない(export は開始されない)こと。"""
    _load_and_wait(client)

    called = {"count": 0}

    def counting_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        called["count"] += 1
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", counting_apply)

    resp = client.post(
        "/api/export", json={"output_path": "out.ifc", "geometry_cleanup": "banana"}
    )
    assert resp.status_code == 422
    assert called["count"] == 0


def test_export_before_ready_returns_409(client):
    resp = client.post("/api/export", json={"output_path": "out.ifc"})
    assert resp.status_code == 409


def test_export_while_exporting_returns_409(client, monkeypatch):
    _load_and_wait(client)

    gate = threading.Event()

    def slow_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        gate.wait(timeout=5.0)
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", slow_apply)

    first = client.post("/api/export", json={"output_path": "out.ifc"})
    assert first.status_code == 202

    second = client.post("/api/export", json={"output_path": "out2.ifc"})
    assert second.status_code == 409

    gate.set()
    _wait_for_status(client, {"ready", "error"})


def test_export_failure_returns_to_ready_and_surfaces_error(client, monkeypatch):
    """export失敗はstate="error"(終端)にせず、"ready"に復帰しつつ export_result に
    エラーを記録すること(Final Review Fix1)。復帰後は診断も再exportも可能なこと。"""
    _load_and_wait(client)

    def boom_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        raise ValueError("boom: apply_operations failed")

    monkeypatch.setattr(app_module, "apply_operations", boom_apply)

    resp = client.post("/api/export", json={"output_path": "out.ifc"})
    assert resp.status_code == 202

    status = _wait_for_status(client, {"ready", "error"})
    assert status["state"] == "ready"
    assert status["export_result"]["error"]
    assert "boom" in status["export_result"]["error"]

    # /api/diagnostics は依然使える(model/opsが無傷であること)
    diag_resp = client.get("/api/diagnostics")
    assert diag_resp.status_code == 200

    # apply_operations を正常版に戻せば再exportは成功する
    def fake_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", fake_apply)

    resp2 = client.post("/api/export", json={"output_path": "out2.ifc"})
    assert resp2.status_code == 202
    status2 = _wait_for_status(client, {"ready", "error"})
    assert status2["state"] == "ready"
    assert status2["export_result"]["output_path"] == "out2.ifc"
    assert "error" not in status2["export_result"]


def test_load_while_exporting_returns_409(client, monkeypatch):
    _load_and_wait(client)

    gate = threading.Event()

    def slow_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        gate.wait(timeout=5.0)
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", slow_apply)

    first = client.post("/api/export", json={"output_path": "out.ifc"})
    assert first.status_code == 202

    reload_resp = client.post("/api/load", json={"path": "dummy.ifc"})
    assert reload_resp.status_code == 409

    gate.set()
    _wait_for_status(client, {"ready", "error"})


def test_ops_while_exporting_returns_409(client, monkeypatch):
    _load_and_wait(client)

    gate = threading.Event()

    def slow_apply(src_path, operations, output_path, consolidate=False, geometry_cleanup="gc"):
        gate.wait(timeout=5.0)
        return ExportReport(output_path=str(output_path))

    monkeypatch.setattr(app_module, "apply_operations", slow_apply)

    first = client.post("/api/export", json={"output_path": "out.ifc"})
    assert first.status_code == 202

    ops_resp = client.post("/api/ops", json={"operations": []})
    assert ops_resp.status_code == 409

    gate.set()
    _wait_for_status(client, {"ready", "error"})


# --- pydantic validation on existing /api/load --------------------------------


def test_load_without_path_returns_422(client):
    resp = client.post("/api/load", json={})
    assert resp.status_code == 422


# --- duplicate_groups element_gids ---------------------------------------------


def test_diagnostics_duplicate_groups_have_element_gids(client):
    _load_and_wait(client)
    resp = client.get("/api/diagnostics")
    assert resp.status_code == 200
    groups = resp.json()["duplicate_groups"]
    assert len(groups) == 1
    group = groups[0]
    assert set(group["shape_ids"]) == {"s1", "s2"}
    gids_flat = {gid for sub in group["element_gids"] for gid in sub}
    assert gids_flat == {"G1", "G2"}
    # 各 sub-list はその shape_id を参照する要素の GlobalId
    for shape_id, gids in zip(group["shape_ids"], group["element_gids"]):
        expected = {"s1": ["G1"], "s2": ["G2"]}[shape_id]
        assert gids == expected


# --- load stage timings in status message --------------------------------------


def test_status_message_after_ready_contains_stage_timings(client):
    _load_and_wait(client)
    status = client.get("/api/status").json()
    for stage in ("extract", "diagnose", "duplicates", "meshpack"):
        assert stage in status["message"]
