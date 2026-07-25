import socket

import numpy as np

import ifc_occam.cli as cli
from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.duplicates import find_duplicates
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo
from ifc_occam.cli import _find_free_port, format_report, main


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


# --- cui サブコマンド (cui-design.md §6 cli.py節、task-6-brief.md) ---
#
# repl.run自体のTDDはtest_repl.pyが担う。ここではargparseの配線
# (`cui <path> [--output <path>] [--scan-only]` → repl.run呼び出し)だけを確認する。


def test_cui_subcommand_calls_run_cui_with_parsed_args(monkeypatch):
    captured = {}

    def _fake_run_cui(path, *, output=None, scan_only=False):
        captured["path"] = path
        captured["output"] = output
        captured["scan_only"] = scan_only

    monkeypatch.setattr(cli, "run_cui", _fake_run_cui)

    main(["cui", "model.ifc", "--output", "out.ifc", "--scan-only"])

    assert captured == {"path": "model.ifc", "output": "out.ifc", "scan_only": True}


def test_cui_subcommand_defaults_output_none_and_scan_only_false(monkeypatch):
    captured = {}

    def _fake_run_cui(path, *, output=None, scan_only=False):
        captured["path"] = path
        captured["output"] = output
        captured["scan_only"] = scan_only

    monkeypatch.setattr(cli, "run_cui", _fake_run_cui)

    main(["cui", "model.ifc"])

    assert captured == {"path": "model.ifc", "output": None, "scan_only": False}


def test_cui_scan_only_on_real_small_ifc_prints_ranking_via_cli_main(small_ifc_path, capsys):
    """task-6-brief.md 統合テスト: `cui --scan-only` (CLI経由) が small.ifc で
    ランキングを出力する。repl.run自体の詳細はtest_repl.pyが担うため、ここでは
    argparse配線を通してもこの結果が変わらないことだけを確認する。"""
    main(["cui", str(small_ifc_path), "--scan-only"])

    out = capsys.readouterr().out
    assert "クラス別ランキング" in out
    assert "スキーマ" in out
