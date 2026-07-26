"""ファイル一覧API (ifc_occam/server/files.py) と /api/files, /api/config のテスト
(GUI改修 Task4)。

root配下への閉じ込め検証が主眼(監督者裁定1,2)。resolve_within_root/
list_directory の単体テストと、/api/files 経由のHTTP契約(400/404含む)の
両方を検証する。/api/load も同じ閉じ込め判定を通ることを検証する
(裁定6: ダイアログだけ塞いでも手打ち欄が素通しなら意味が無い)。

閉じ込め判定は resolve() 後の実パスを Path.is_relative_to() で比較する
(監督者裁定2)。文字列の前方一致(startswith)だけの判定では `C:\\work` の
root設定時に兄弟ディレクトリ `C:\\work-secret` を誤って内側と判定してしまう
——このケースを専用テストで固定する(test_*_sibling_dir_prefix_confusion_*)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ifc_occam.server.app as app_module
from ifc_occam.server.files import list_directory, resolve_within_root

# ---------------------------------------------------------------------------
# resolve_within_root (単体)
# ---------------------------------------------------------------------------


def test_resolve_within_root_accepts_root_itself(tmp_path):
    assert resolve_within_root(tmp_path, "") == tmp_path.resolve()


def test_resolve_within_root_accepts_subpath(tmp_path):
    (tmp_path / "sub").mkdir()
    assert resolve_within_root(tmp_path, "sub") == (tmp_path / "sub").resolve()


def test_resolve_within_root_rejects_dotdot_escape(tmp_path):
    with pytest.raises(ValueError):
        resolve_within_root(tmp_path, "../..")


def test_resolve_within_root_rejects_absolute_path_outside_root(tmp_path):
    with pytest.raises(ValueError):
        resolve_within_root(tmp_path, "C:/Windows")


def test_resolve_within_root_accepts_absolute_path_actually_inside_root(tmp_path):
    """絶対パス表記そのものを理由に拒否してはならない(監督者裁定2の精神:
    実パスでの内外判定)。root配下を絶対パスで指定した場合は許可されること。"""
    (tmp_path / "inside.ifc").write_text("x")
    absolute = str(tmp_path / "inside.ifc")
    assert resolve_within_root(tmp_path, absolute) == (tmp_path / "inside.ifc").resolve()


def test_resolve_within_root_rejects_sibling_dir_sharing_name_prefix(tmp_path):
    """startswith前方一致だけで判定すると、root="...\\work" のとき兄弟
    "...\\work-secret" への脱出を「rootの中」と誤判定してしまう(監督者裁定2)。
    resolve()後の実パスをis_relative_toで比較していれば正しく拒否される。"""
    root = tmp_path / "work"
    root.mkdir()
    secret = tmp_path / "work-secret"
    secret.mkdir()
    (secret / "leaked.ifc").write_text("secret")

    # 文字列前方一致であれば通ってしまうことの確認(このテスト自体が
    # 誤検出でないことの根拠):
    naive_candidate = str(secret / "leaked.ifc")
    assert naive_candidate.startswith(str(root))  # startswithなら「内側」と誤判定される

    with pytest.raises(ValueError):
        resolve_within_root(root, "../work-secret/leaked.ifc")


def test_resolve_within_root_rejects_symlink_escaping_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.ifc").write_text("secret")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("このOS/権限ではシンボリックリンクを作成できない")

    with pytest.raises(ValueError):
        resolve_within_root(root, "escape/secret.ifc")


# ---------------------------------------------------------------------------
# list_directory (単体)
# ---------------------------------------------------------------------------


def test_list_directory_root_returns_entries(tmp_path):
    (tmp_path / "a.ifc").write_text("x")
    (tmp_path / "sub").mkdir()
    result = list_directory(tmp_path, "", (".ifc",))
    assert result["path"] == ""
    assert result["parent"] is None
    names = [e["name"] for e in result["entries"]]
    assert names == ["sub", "a.ifc"]


def test_list_directory_subdir_has_parent_pointing_up(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.ifc").write_text("x")
    result = list_directory(tmp_path, "sub", (".ifc",))
    assert result["path"] == "sub"
    assert result["parent"] == ""
    assert [e["name"] for e in result["entries"]] == ["b.ifc"]


def test_list_directory_nested_subdir_parent_is_root_relative(tmp_path):
    nested = tmp_path / "sub" / "nested"
    nested.mkdir(parents=True)
    result = list_directory(tmp_path, "sub/nested", (".ifc",))
    assert result["path"] == "sub/nested"
    assert result["parent"] == "sub"


def test_list_directory_filters_non_matching_suffix(tmp_path):
    (tmp_path / "keep.ifc").write_text("x")
    (tmp_path / "skip.txt").write_text("x")
    result = list_directory(tmp_path, "", (".ifc",))
    names = [e["name"] for e in result["entries"]]
    assert names == ["keep.ifc"]


def test_list_directory_suffix_match_is_case_insensitive(tmp_path):
    (tmp_path / "upper.IFC").write_text("x")
    result = list_directory(tmp_path, "", (".ifc",))
    names = [e["name"] for e in result["entries"]]
    assert names == ["upper.IFC"]


def test_list_directory_directories_always_shown_regardless_of_suffix(tmp_path):
    (tmp_path / "somedir").mkdir()
    (tmp_path / "other.txt").write_text("x")
    result = list_directory(tmp_path, "", (".ifc",))
    names = [e["name"] for e in result["entries"]]
    assert names == ["somedir"]  # ディレクトリは残り、.txtは消える


def test_list_directory_dirs_sort_before_files(tmp_path):
    (tmp_path / "z.ifc").write_text("x")
    (tmp_path / "a_dir").mkdir()
    result = list_directory(tmp_path, "", (".ifc",))
    entries = result["entries"]
    assert entries[0]["name"] == "a_dir"
    assert entries[0]["is_dir"] is True
    assert entries[1]["name"] == "z.ifc"
    assert entries[1]["is_dir"] is False


def test_list_directory_files_sorted_by_name_ascending(tmp_path):
    (tmp_path / "z.ifc").write_text("x")
    (tmp_path / "a.ifc").write_text("x")
    result = list_directory(tmp_path, "", (".ifc",))
    names = [e["name"] for e in result["entries"]]
    assert names == ["a.ifc", "z.ifc"]


def test_list_directory_file_entry_has_size_and_mtime(tmp_path):
    (tmp_path / "a.ifc").write_text("hello")
    result = list_directory(tmp_path, "", (".ifc",))
    entry = result["entries"][0]
    assert entry["size"] == 5
    assert isinstance(entry["mtime"], float)


def test_list_directory_missing_path_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        list_directory(tmp_path, "does-not-exist", (".ifc",))


def test_list_directory_escaping_path_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        list_directory(tmp_path, "../..", (".ifc",))


def test_list_directory_path_to_a_file_raises_file_not_found(tmp_path):
    """ディレクトリでないパス(ファイル自身)を渡した場合も404系(FileNotFoundError)
    にする。存在はするが「一覧できるディレクトリ」ではないため。"""
    (tmp_path / "a.ifc").write_text("x")
    with pytest.raises(FileNotFoundError):
        list_directory(tmp_path, "a.ifc", (".ifc",))


# ---------------------------------------------------------------------------
# HTTP: /api/files
# ---------------------------------------------------------------------------


@pytest.fixture()
def files_root(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.ifc").write_text("x")
    (tmp_path / "top.ifc").write_text("x")
    (tmp_path / "ignore.txt").write_text("x")
    return tmp_path


@pytest.fixture()
def files_client(files_root, monkeypatch):
    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: object())
    app = app_module.create_app(root=files_root)
    with TestClient(app) as c:
        yield c


def test_api_files_root_lists_entries(files_client):
    resp = files_client.get("/api/files", params={"path": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == ""
    assert body["parent"] is None
    names = {e["name"] for e in body["entries"]}
    assert names == {"sub", "top.ifc"}  # ignore.txtはフィルタされる


def test_api_files_no_path_param_defaults_to_root(files_client):
    resp = files_client.get("/api/files")
    assert resp.status_code == 200
    assert resp.json()["path"] == ""


def test_api_files_subdir_lists_and_parent(files_client):
    resp = files_client.get("/api/files", params={"path": "sub"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "sub"
    assert body["parent"] == ""
    names = {e["name"] for e in body["entries"]}
    assert names == {"nested.ifc"}


def test_api_files_dotdot_escape_returns_400(files_client):
    resp = files_client.get("/api/files", params={"path": "../.."})
    assert resp.status_code == 400


def test_api_files_absolute_path_outside_root_returns_400(files_client):
    resp = files_client.get("/api/files", params={"path": "C:/Windows"})
    assert resp.status_code == 400


def test_api_files_missing_path_returns_404(files_client):
    resp = files_client.get("/api/files", params={"path": "no-such-dir"})
    assert resp.status_code == 404


def test_api_files_sibling_dir_prefix_confusion_returns_400(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    secret = tmp_path / "work-secret"
    secret.mkdir()
    (secret / "leaked.ifc").write_text("x")
    monkeypatch.setattr(app_module, "open_ifc_file", lambda path: object())
    app = app_module.create_app(root=root)
    with TestClient(app) as client:
        resp = client.get("/api/files", params={"path": "../work-secret"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# HTTP: /api/config
# ---------------------------------------------------------------------------


def test_api_config_shape(files_client):
    resp = files_client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "fullopen_bytes_multiplier",
        "fullopen_warn_bytes",
        "load_estimate",
    }
    assert isinstance(body["fullopen_bytes_multiplier"], int)
    assert isinstance(body["fullopen_warn_bytes"], int)
    load_estimate = body["load_estimate"]
    assert set(load_estimate.keys()) == {"sec_per_mb", "base_sec", "band_low", "band_high"}


def test_api_config_matches_source_constants_no_copy_paste(files_client):
    """定数をJS側に写経しない(=Python側も二重管理しない)ことの固定:
    /api/config が返す値は、実際に aggregate.py/repl.py の定数と
    (インポートして比較する形で)一致すること。値を再入力してはならない。"""
    from ifc_occam.cui.repl import _FULLOPEN_WARN_BYTES
    from ifc_occam.scan.aggregate import FULLOPEN_BYTES_MULTIPLIER

    body = files_client.get("/api/config").json()
    assert body["fullopen_bytes_multiplier"] == FULLOPEN_BYTES_MULTIPLIER
    assert body["fullopen_warn_bytes"] == _FULLOPEN_WARN_BYTES


def test_api_config_load_estimate_values(files_client):
    body = files_client.get("/api/config").json()
    load_estimate = body["load_estimate"]
    assert load_estimate["sec_per_mb"] == pytest.approx(0.72)
    assert load_estimate["base_sec"] == pytest.approx(30.0)
    assert load_estimate["band_low"] == pytest.approx(0.5)
    assert load_estimate["band_high"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 裁定6: /api/load も同じ閉じ込め判定を通す(手打ち欄の素通し防止)
# ---------------------------------------------------------------------------


def test_load_rejects_dotdot_path_outside_root(files_client):
    resp = files_client.post("/api/load", json={"path": "../outside.ifc"})
    assert resp.status_code == 400


def test_load_rejects_absolute_path_outside_root(files_client):
    resp = files_client.post("/api/load", json={"path": "C:/Windows/system.ifc"})
    assert resp.status_code == 400


def test_load_accepts_path_inside_root(files_client):
    """rootの中を指す限り、既存の挙動(202→loading)は変わらないこと
    (パス閉じ込めチェックの追加が誤って正当な相対パスまで弾かないことの確認。
    202が即時応答されることが主眼であり、背後スレッドの成否は見ない)。"""
    resp = files_client.post("/api/load", json={"path": "top.ifc"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "loading"


# ---------------------------------------------------------------------------
# 読めないディレクトリ(Task 4 レビュー Important-1)
# ---------------------------------------------------------------------------


def test_api_files_returns_403_with_japanese_detail_when_directory_is_unreadable(
    files_client, monkeypatch
):
    """iterdir が PermissionError を投げるディレクトリで 500 にならないこと。

    修正前は `list_directory` の `target.iterdir()` が無保護で、権限拒否の
    フォルダを開くと 500 "Internal Server Error" になり、ダイアログにその
    英語がそのまま出た(レビュアが icacls で読み取り拒否フォルダを作って再現)。
    OneDrive のプレースホルダ、ネットワークドライブの一時不通、ウイルス対策
    ソフトのロックでも同じ経路を通るため、実運用で十分に起こり得る。

    権限操作は環境依存(管理者権限や icacls の可否)なので、ここでは
    `Path.iterdir` を差し替えて OS に依存せず再現する。
    """
    real_iterdir = Path.iterdir

    def _deny(self):
        if self.name == "sub":
            raise PermissionError(13, "アクセスが拒否されました。")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _deny)

    resp = files_client.get("/api/files", params={"path": "sub"})

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "フォルダを読めませんでした" in detail
    assert "PermissionError" in detail


def test_api_files_404_takes_precedence_over_the_new_oserror_branch(files_client):
    """存在しないパスは 403 ではなく 404 のままであること。

    `FileNotFoundError` は `OSError` のサブクラスなので、ハンドラの except を
    書く順序を逆にすると 404 が 403 に化ける。その退行をここで固定する。
    """
    resp = files_client.get("/api/files", params={"path": "does-not-exist"})
    assert resp.status_code == 404
