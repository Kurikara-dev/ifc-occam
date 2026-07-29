import socket

import numpy as np

import ifc_occam.cli as cli
from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.duplicates import find_duplicates
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo
from ifc_occam.cli import _find_free_port, format_report, main, resolve_entry_argv


def _model():
    tet_f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
    v = np.eye(4, 3)
    shapes = {"s1": ShapeInfo("s1", v, tet_f), "s2": ShapeInfo("s2", v + 5, tet_f)}
    elements = [
        ElementInfo("G1", "IfcWall", None, "s1", False, (), None),
        ElementInfo("G2", "IfcWall", None, "s2", False, (), None),
    ]
    return ModelData(schema="IFC4", elements=elements, shapes=shapes)


def test_format_report_contains_key_figures():
    m = _model()
    text = format_report(m, aggregate_by_class(m),
                         find_duplicates(m.shapes), warnings=["w1"])
    assert "IFC4" in text
    assert "IfcWall" in text
    assert "8" in text        # 総三角形数 4×2
    assert "警告: 1" in text


def test_format_report_shows_duplicate_groups():
    m = _model()
    text = format_report(m, aggregate_by_class(m),
                         find_duplicates(m.shapes), warnings=[])
    assert "重複" in text


# --- _find_free_port ---------------------------------------------------


def test_find_free_port_returns_start_when_free():
    # start をランダムな高ポートにして、他のテストと衝突しないようにする。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert _find_free_port(free_port) == free_port


def test_find_free_port_skips_occupied_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        occupied_port = occupied.getsockname()[1]

        result = _find_free_port(occupied_port)

        assert result != occupied_port
        assert result >= occupied_port


# --- cui サブコマンド (cui-design.md §6 cli.py節、docs/plans/2026-07-24-cui-phase1.md Task 6) ---
#
# repl.run自体のTDDはtest_repl.pyが担う。ここではargparseの配線
# (`cui <path> [--output <path>] [--scan-only]` → repl.run呼び出し)だけを確認する。


def test_cui_subcommand_calls_run_cui_with_parsed_args(monkeypatch):
    captured = {}

    def _fake_run_cui(path, *, output=None, scan_only=False, text=False):
        captured["path"] = path
        captured["output"] = output
        captured["scan_only"] = scan_only

    monkeypatch.setattr(cli, "run_cui", _fake_run_cui)

    main(["cui", "model.ifc", "--output", "out.ifc", "--scan-only"])

    assert captured == {"path": "model.ifc", "output": "out.ifc", "scan_only": True}


def test_cui_subcommand_defaults_output_none_and_scan_only_false(monkeypatch):
    captured = {}

    def _fake_run_cui(path, *, output=None, scan_only=False, text=False):
        captured["path"] = path
        captured["output"] = output
        captured["scan_only"] = scan_only

    monkeypatch.setattr(cli, "run_cui", _fake_run_cui)

    main(["cui", "model.ifc"])

    assert captured == {"path": "model.ifc", "output": None, "scan_only": False}


def test_cui_subcommand_forwards_text_flag_to_run_cui(monkeypatch):
    """--text フラグが run_cui(= repl.run)の text= に渡ること
    (docs/plans/2026-07-25-cui-phase3.md Task5)。既存フラグ(output/scan_only)
    は無変更のまま同時に渡ることも確認する。"""
    captured = {}

    def _fake_run_cui(path, *, output=None, scan_only=False, text=False):
        captured["path"] = path
        captured["output"] = output
        captured["scan_only"] = scan_only
        captured["text"] = text

    monkeypatch.setattr(cli, "run_cui", _fake_run_cui)

    main(["cui", "model.ifc", "--text"])

    assert captured == {"path": "model.ifc", "output": None, "scan_only": False, "text": True}


def test_cui_scan_only_on_real_small_ifc_prints_ranking_via_cli_main(small_ifc_path, capsys):
    """docs/plans/2026-07-24-cui-phase1.md Task 6 統合テスト: `cui --scan-only` (CLI経由) が small.ifc で
    ランキングを出力する。repl.run自体の詳細はtest_repl.pyが担うため、ここでは
    argparse配線を通してもこの結果が変わらないことだけを確認する。"""
    main(["cui", str(small_ifc_path), "--scan-only"])

    out = capsys.readouterr().out
    assert "クラス別ランキング" in out
    assert "スキーマ" in out


def test_resolve_entry_argv_defaults_to_serve_when_empty():
    """無引数(start-exe.bat のダブルクリック)は GUI を起動する。"""
    assert resolve_entry_argv([]) == ["serve"]


def test_resolve_entry_argv_prepends_serve_for_bare_options():
    """従来の使い方(exe --port 8100)を壊さない(serve への素通し)。"""
    assert resolve_entry_argv(["--port", "8100"]) == ["serve", "--port", "8100"]
    assert resolve_entry_argv(["--no-browser"]) == ["serve", "--no-browser"]


def test_resolve_entry_argv_passes_known_subcommands_through():
    """cui / diagnose / serve は exe からそのまま使える(このタスクの目的)。"""
    assert resolve_entry_argv(["cui", "small.ifc", "--scan-only"]) == [
        "cui", "small.ifc", "--scan-only"
    ]
    assert resolve_entry_argv(["diagnose", "x.ifc"]) == ["diagnose", "x.ifc"]
    assert resolve_entry_argv(["serve", "--port", "8100"]) == ["serve", "--port", "8100"]


def test_resolve_entry_argv_passes_help_through():
    """-h / --help はトップレベルのヘルプ(全サブコマンド一覧)を出す。
    serve を前置すると serve のヘルプになってしまい、cui の存在が見えない。"""
    assert resolve_entry_argv(["-h"]) == ["-h"]
    assert resolve_entry_argv(["--help"]) == ["--help"]


def test_resolve_entry_argv_prepends_serve_for_unknown_first_arg():
    """未知の第1引数(例: IFCファイルをexeにドラッグ)は serve に渡し、
    argparse の usage エラー(exit 2)に落とす。黙って何かを推測しない。"""
    assert resolve_entry_argv(["model.ifc"]) == ["serve", "model.ifc"]


def test_entry_subcommands_stay_in_sync_with_the_parser(capsys):
    """_ENTRY_SUBCOMMANDS(exe用の手書きリスト)と main() の add_parser 群の一致を固定する。

    両者は単一のソースを持たない二重管理になっている(レビュー指摘 Minor)。
    parser にサブコマンドを足して _ENTRY_SUBCOMMANDS を忘れると、python -m では
    動くのに exe からだけ serve が前置されて usage エラーで落ちる。この非対称は
    手元では再現しないため、ズレた瞬間にここで赤くする。
    """
    import re

    import pytest

    with pytest.raises(SystemExit):
        main(["-h"])
    help_text = capsys.readouterr().out
    match = re.search(r"\{([a-z,]+)\}", help_text)
    assert match is not None, f"ヘルプにサブコマンド一覧が見つからない: {help_text!r}"
    assert set(match.group(1).split(",")) == set(cli._ENTRY_SUBCOMMANDS)
