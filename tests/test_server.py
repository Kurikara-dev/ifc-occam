"""FastAPI サーバのテスト (phase2 task 3)。extract_model を monkeypatch して高速化する。"""

from __future__ import annotations

import struct
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import ifc_occam.server.app as app_module
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo
from ifc_occam.server.meshpack import build_mesh_payload


def _synthetic_model() -> tuple[ModelData, list[str]]:
    tet_f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
    v = np.eye(4, 3)
    shapes = {"s1": ShapeInfo("s1", v, tet_f)}
    identity = np.eye(4)
    elements = [
        ElementInfo(
            "G1", "IfcWall", "Wall-1", "s1", False, ("SweptSolid",), "Layer-A",
            placement=identity,
        ),
        ElementInfo(
            "G2", "IfcDoor", "Door-1", "s1", False, ("SweptSolid",), "Layer-B",
            placement=identity,
        ),
        ElementInfo(
            "G3", "IfcWall", None, None, False, (), None,
            placement=None,
        ),
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
    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: object())
    monkeypatch.setattr(app_module, "extract_model", lambda source: _synthetic_model())
    app = app_module.create_app()
    with TestClient(app) as c:
        yield c


def test_load_returns_202_and_transitions_to_ready(client):
    resp = client.post("/api/load", json={"path": "dummy.ifc"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "loading"

    status = _wait_for_status(client, {"ready", "error"})
    assert status["state"] == "ready"
    assert "elapsed_sec" in status


def test_diagnostics_and_mesh_409_before_ready(client):
    assert client.get("/api/diagnostics").status_code == 409
    assert client.get("/api/mesh").status_code == 409


def test_diagnostics_after_ready_contains_expected_fields(client):
    client.post("/api/load", json={"path": "dummy.ifc"})
    _wait_for_status(client, {"ready", "error"})

    resp = client.get("/api/diagnostics")
    assert resp.status_code == 200
    body = resp.json()

    assert body["schema"] == "IFC4"
    assert body["element_count"] == 3
    assert isinstance(body["total_triangles"], int)
    assert body["warnings"] == ["warn-1"]
    assert body["layers"] == ["Layer-A", "Layer-B"]

    class_names = {c["ifc_class"] for c in body["class_stats"]}
    assert class_names == {"IfcWall", "IfcDoor"}
    for c in body["class_stats"]:
        for key, val in c.items():
            if key != "ifc_class":
                assert isinstance(val, int), f"{key} is not plain int: {type(val)}"

    assert body["duplicate_groups"] == []


def test_mesh_matches_build_mesh_payload(client):
    client.post("/api/load", json={"path": "dummy.ifc"})
    _wait_for_status(client, {"ready", "error"})

    resp = client.get("/api/mesh")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"

    model, _ = _synthetic_model()
    expected = build_mesh_payload(model)
    assert resp.content == expected

    # sanity: parse header
    json_len = struct.unpack_from("<I", resp.content, 0)[0]
    assert json_len > 0


def test_load_nonexistent_path_sets_error_state(monkeypatch):
    def boom(source):
        raise FileNotFoundError(f"no such file: {source}")

    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: object())
    monkeypatch.setattr(app_module, "extract_model", boom)
    app = app_module.create_app()
    with TestClient(app) as client:
        client.post("/api/load", json={"path": "does-not-exist.ifc"})
        status = _wait_for_status(client, {"ready", "error"})
        assert status["state"] == "error"
        assert "no such file" in status["message"]


def test_reload_while_loading_returns_409(monkeypatch):
    import threading

    gate = threading.Event()

    def slow_extract(source):
        gate.wait(timeout=5.0)
        return _synthetic_model()

    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: object())
    monkeypatch.setattr(app_module, "extract_model", slow_extract)
    app = app_module.create_app()
    with TestClient(app) as client:
        first = client.post("/api/load", json={"path": "dummy.ifc"})
        assert first.status_code == 202

        second = client.post("/api/load", json={"path": "dummy.ifc"})
        assert second.status_code == 409

        gate.set()
        _wait_for_status(client, {"ready", "error"})


def test_root_without_web_dir_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: object())
    monkeypatch.setattr(app_module, "extract_model", lambda source: _synthetic_model())
    monkeypatch.setattr(app_module, "WEB_DIR", tmp_path / "no-such-web-dir")
    app = app_module.create_app()
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 404


def test_static_response_has_no_store_cache_control(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


def test_api_response_does_not_have_no_store_cache_control(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"
