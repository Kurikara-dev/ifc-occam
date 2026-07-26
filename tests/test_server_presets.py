"""プリセットAPI 3本のテスト (phase4 plan Task3)。

extract_model を monkeypatch した合成モデルで検証する。presets.json の実体は
tmp_path 配下に作り、create_app(presets_path=...) の注入シームを使って
本物のプロジェクトルートを汚さない。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import ifc_occam.server.app as app_module
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo


def _synthetic_model() -> tuple[ModelData, list[str]]:
    tet_f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
    v = np.eye(4, 3)
    shapes = {"s1": ShapeInfo("s1", v, tet_f)}
    identity = np.eye(4)
    elements = [
        ElementInfo("G1", "IfcLightFixture", "LF-1", "s1", False, ("SweptSolid",), "Layer-A", placement=identity),
        ElementInfo("G2", "IfcLightFixture", "LF-2", "s1", False, ("SweptSolid",), "Layer-A", placement=identity),
        ElementInfo("G3", "IfcWall", "Wall-1", "s1", False, ("SweptSolid",), "Layer-B", placement=identity),
    ]
    model = ModelData(schema="IFC4", elements=elements, shapes=shapes)
    return model, []


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
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: object())
    monkeypatch.setattr(app_module, "extract_model", lambda source: _synthetic_model())
    app = app_module.create_app(presets_path=tmp_path / "presets.json")
    with TestClient(app) as c:
        yield c


def _load_and_wait(client: TestClient) -> None:
    resp = client.post("/api/load", json={"path": "dummy.ifc"})
    assert resp.status_code == 202
    status = _wait_for_status(client, {"ready", "error"})
    assert status["state"] == "ready"


_SAMPLE_BODY = [
    {
        "name": "CFD用",
        "description": "照明を削除",
        "rules": [{"match": {"ifc_class": "IfcLightFixture"}, "op": {"op": "delete"}}],
    }
]


# --- GET /api/presets --------------------------------------------------------


def test_get_presets_returns_empty_list_when_file_missing(client):
    resp = client.get("/api/presets")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_presets_after_post_reflects_saved_content(client):
    client.post("/api/presets", json=_SAMPLE_BODY)
    resp = client.get("/api/presets")
    assert resp.status_code == 200
    assert resp.json() == _SAMPLE_BODY


# --- POST /api/presets (全置換) ----------------------------------------------


def test_post_presets_replaces_previous_content(client):
    client.post("/api/presets", json=_SAMPLE_BODY)
    second_body = [{"name": "干渉チェック用", "description": "", "rules": []}]
    resp = client.post("/api/presets", json=second_body)
    assert resp.status_code == 200

    get_resp = client.get("/api/presets")
    assert get_resp.json() == second_body


def test_post_presets_rejects_malformed_body(client):
    resp = client.post("/api/presets", json=[{"description": "missing required name"}])
    assert resp.status_code == 422


def test_post_presets_rejects_rule_missing_op(client):
    bad_body = [{"name": "p", "description": "", "rules": [{"match": {"ifc_class": "IfcWall"}}]}]
    resp = client.post("/api/presets", json=bad_body)
    assert resp.status_code == 422


# --- POST /api/presets/resolve -----------------------------------------------


def test_resolve_before_ready_returns_409(client):
    client.post("/api/presets", json=_SAMPLE_BODY)
    resp = client.post("/api/presets/resolve", json={"name": "CFD用"})
    assert resp.status_code == 409


def test_resolve_unknown_name_returns_404(client):
    _load_and_wait(client)
    client.post("/api/presets", json=_SAMPLE_BODY)
    resp = client.post("/api/presets/resolve", json={"name": "存在しない"})
    assert resp.status_code == 404


def test_resolve_returns_per_rule_match_op_count_gids(client):
    _load_and_wait(client)
    client.post("/api/presets", json=_SAMPLE_BODY)

    resp = client.post("/api/presets/resolve", json={"name": "CFD用"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["warnings"] == []
    assert len(data["rules"]) == 1
    rule = data["rules"][0]
    assert rule["match"] == {"ifc_class": "IfcLightFixture"}
    assert rule["op"] == {"op": "delete"}
    assert rule["count"] == 2
    assert sorted(rule["gids"]) == ["G1", "G2"]


def test_resolve_unknown_match_key_returns_warning(client):
    bad_preset = [
        {"name": "bad", "description": "", "rules": [{"match": {"color": "red"}, "op": {"op": "delete"}}]}
    ]
    client.post("/api/presets", json=bad_preset)
    _load_and_wait(client)

    resp = client.post("/api/presets/resolve", json={"name": "bad"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["warnings"]) == 1
    assert data["rules"][0]["count"] == 0


# --- DELETE /api/presets (削除、GUI改修Task6) ---------------------------------
#
# 監督者裁定4: 当初案の DELETE /api/presets/{name}(パスパラメータ)は、
# name に "/" を含む場合にルーティングが壊れる(Starletteの既定コンバータは
# 単一パスセグメントを "/" で区切るため、%2Fに事前デコードされた時点で
# パスセグメントが分かれてしまい404になる)。実験で確認済み
# (本テストの test_..._name_containing_slash
# で再現)。そのため本APIはクエリパラメータ方式 DELETE /api/presets?name=...
# を採用する(既存の GET /api/files?path=... と同じ house style)。

_TWO_PRESET_BODY = [
    {
        "name": "CFD用",
        "description": "照明を削除",
        "rules": [{"match": {"ifc_class": "IfcLightFixture"}, "op": {"op": "delete"}}],
    },
    {"name": "干渉チェック用", "description": "", "rules": []},
]


def test_delete_preset_removes_it_from_list(client):
    client.post("/api/presets", json=_TWO_PRESET_BODY)

    resp = client.delete("/api/presets", params={"name": "CFD用"})
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()] == ["干渉チェック用"]

    get_resp = client.get("/api/presets")
    assert [p["name"] for p in get_resp.json()] == ["干渉チェック用"]


def test_delete_preset_unknown_name_returns_404(client):
    client.post("/api/presets", json=_SAMPLE_BODY)

    resp = client.delete("/api/presets", params={"name": "存在しない"})
    assert resp.status_code == 404

    # 404でも既存のプリセットは無事であること
    get_resp = client.get("/api/presets")
    assert get_resp.json() == _SAMPLE_BODY


def test_delete_preset_does_not_affect_other_presets(client):
    client.post("/api/presets", json=_TWO_PRESET_BODY)

    client.delete("/api/presets", params={"name": "CFD用"})

    get_resp = client.get("/api/presets")
    remaining = get_resp.json()
    assert len(remaining) == 1
    assert remaining[0] == _TWO_PRESET_BODY[1]


def test_delete_preset_supports_japanese_name_with_spaces(client):
    body = [{"name": "テスト プリセット 名前", "description": "", "rules": []}]
    client.post("/api/presets", json=body)

    resp = client.delete("/api/presets", params={"name": "テスト プリセット 名前"})
    assert resp.status_code == 200
    assert resp.json() == []

    get_resp = client.get("/api/presets")
    assert get_resp.json() == []


def test_delete_preset_supports_name_containing_slash(client):
    """裁定4で switch した根拠そのものを固定するテスト: "/" を含む名前でも
    クエリパラメータ方式なら削除できる(パスパラメータ方式では404になっていた)。"""
    body = [{"name": "新規/既存 対応", "description": "", "rules": []}]
    client.post("/api/presets", json=body)

    resp = client.delete("/api/presets", params={"name": "新規/既存 対応"})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize(
    "tricky_name",
    [
        "ha#sh",
        "qu?ery",
        "am&per",
        "pe%rcent",
        "sl%2Fash",  # リテラルの "%2F"("/" のエンコード済み表記と紛らわしい)
        "sp%20ace",  # リテラルの "%20"(スペースのエンコード済み表記と紛らわしい)
        "plus+sign",
        "混合/名前#テスト?です&こう%なる スペース",
    ],
)
def test_delete_preset_matches_the_exact_name_for_url_significant_characters(
    client, tricky_name
):
    """URL上で意味を持つ文字を含む名前でも、その1件だけが正確に消えること。

    クエリパラメータ方式(裁定4)にした以上、エンコード/デコードが1段ずれると
    **別のプリセットを消す事故**になる。特に危ないのが `%` を含む名前で、
    `sl%2Fash`(リテラル)と `sl/ash` は二重デコードが起きると同一視される。
    Task 6 レビューで実機の13ケースは確認済みだが、恒久的な回帰テストが
    `/` と日本語だけだったのでここで塞ぐ(Important の引き取り)。

    巻き添え検出のため、常に無関係な番兵プリセットを2件同居させ、削除後に
    それらが名前も内容も無傷であることまで確認する。
    """
    guard_a = {"name": "guard-a", "description": "", "rules": []}
    guard_b = {"name": "guard-b", "description": "触るな", "rules": []}
    target = {"name": tricky_name, "description": "", "rules": []}
    client.post("/api/presets", json=[guard_a, target, guard_b])

    resp = client.delete("/api/presets", params={"name": tricky_name})

    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()] == ["guard-a", "guard-b"]
    assert client.get("/api/presets").json() == [guard_a, guard_b]
