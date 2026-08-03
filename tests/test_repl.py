"""cui/repl.py(対話ループ・薄いI/O殻)のTDD (cui-design.md §6、docs/plans/2026-07-24-cui-phase1.md Task 6)。

repl.py は表示・入出力・ifcopenshellアクセスに徹する。コマンド解釈ロジックは
CuiSession(session.py)に委譲済みのため、ここでは以下だけを担保する:
  - Phase A: 起動時にスキャンし、ランキングを表示する。--scan-only はここで終了する。
  - Phase B: input() ループ。CuiSession.command() の戻り値をそのまま表示するだけ。
    quit/help/apply は repl 自身が横取りする(session.command には渡さない)。
  - Ctrl+C(KeyboardInterrupt)・EOF で安全終了する(メインループ・apply確認の
    input() 呼び出しのどちらで発生しても)。
  - apply: 確認2回(操作サマリ+est_fullopen_bytes警告 → 確認1 / フルオープン後の
    実ファイル突合+delete閉包プレビュー → 確認2)。既定では apply_operations に
    確認1で開いた ifcopenshell.file オブジェクトを直接渡す(再オープンしない)。
  - progress は間引いて表示する(監督者確定要件3: N件ごと+最終件、\\r上書き、
    ステージ完了時に改行して確定行を残す)。

scan_file/apply_operations は repl モジュールの名前空間にインポートされたものを
monkeypatch する(test_export.py の `monkeypatch.setattr(ifcopenshell, "open", ...)`
と同じ「呼び出し側の名前を差し替える」流儀)。フルオープンが絡む箇所は
fixtures_ifc.py の合成IFC(実ファイル)を使い、GlobalId突合や削除連鎖(cascade)を
本物のロジックで検証する — モックするのは apply_operations(export)だけ
(ブリーフが明示する「apply 確認2段の脚本(モック export)」の範囲)。
"""

from __future__ import annotations

import hashlib
import shutil

import numpy as np
from pathlib import Path

import ifcopenshell
import pytest

from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.export import ExportReport
from ifc_occam.core.extract import extract_model
from ifc_occam.cui import repl
from ifc_occam.scan.aggregate import ClassScanStats, ScanResult, scan_file
from ifc_occam.scan.fullgraph import scan_full_graph
from ifc_occam.textops.plan import TextDeletePlan, compute_text_delete_plan
from ifc_occam.textops.rewrite import RewriteReport
from tests.fixtures_ifc import build_wall_with_window_ifc

# --- テスト用ヘルパー ---


def _stats(ifc_class: str, element_count: int, **overrides) -> ClassScanStats:
    defaults = dict(est_faces_expanded=0, est_faces_unique=0, parametric_count=0)
    defaults.update(overrides)
    return ClassScanStats(ifc_class=ifc_class, element_count=element_count, **defaults)


def _fake_scan(stats, elements=None, **overrides) -> ScanResult:
    defaults = dict(
        path="model.ifc",
        file_size=1000,
        schema="IFC4",
        stats=list(stats),
        proxy_names=[],
        elements=elements if elements is not None else {},
        total_entities=100,
        scan_seconds=0.1,
        est_fullopen_bytes=7000,
    )
    defaults.update(overrides)
    return ScanResult(**defaults)


def _feed_input(monkeypatch, lines: list[str]):
    """builtins.input を lines から順に返すスタブに差し替える。
    枯渇したら EOFError(実際の標準入力終了と同じ挙動)。呼び出された行を記録する。"""
    it = iter(lines)
    calls: list[str] = []

    def _fake_input(prompt: str = "") -> str:
        calls.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError()

    monkeypatch.setattr("builtins.input", _fake_input)
    return calls


def _never_input(monkeypatch):
    """input() が呼ばれたら即失敗させる(--scan-only が対話に入らないことの証明用)。"""

    def _boom(prompt: str = "") -> str:
        raise AssertionError(f"input() が呼ばれてはならない(prompt={prompt!r})")

    monkeypatch.setattr("builtins.input", _boom)


def _write_ifc(f: ifcopenshell.file, tmp_path: Path, name: str = "src.ifc") -> Path:
    path = tmp_path / name
    f.write(str(path))
    return path


# --- 1. Phase A: --scan-only は対話に入らず終了する ---


def test_scan_only_prints_ranking_and_returns_without_reading_input(monkeypatch, capsys):
    scan = _fake_scan([_stats("IFCWALL", 3, est_faces_expanded=30)])
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    _never_input(monkeypatch)

    repl.run("dummy.ifc", scan_only=True)

    out = capsys.readouterr().out
    assert "IFCWALL" in out
    assert "クラス別ランキング" in out


def test_scan_only_does_not_call_apply_or_export(monkeypatch, capsys):
    """--scan-only経路でapply_operationsに触れないことの副次確認(退行防止)。"""
    scan = _fake_scan([])
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom(*a, **kw):
        raise AssertionError("apply_operations が呼ばれてはならない")

    monkeypatch.setattr(repl, "apply_operations", _boom)
    _never_input(monkeypatch)

    repl.run("dummy.ifc", scan_only=True)  # 例外が飛ばなければOK


# --- 2. Phase B: rank→delete→list→apply(確認1でno)→quit ---


def _basic_fake_scan() -> ScanResult:
    return _fake_scan(
        stats=[_stats("IFCWALL", 3, est_faces_expanded=30, est_faces_unique=30)],
        elements={"IFCWALL": ["W0", "W1", "W2"]},
    )


def test_script_rank_delete_list_apply_declined_then_quit(monkeypatch, capsys):
    scan = _basic_fake_scan()
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_open(*a, **kw):
        raise AssertionError("確認1でno答えたのにフルオープンされた")

    monkeypatch.setattr(ifcopenshell, "open", _boom_open)

    calls = _feed_input(
        monkeypatch, ["rank", "delete IFCWALL", "list", "apply", "n", "quit"]
    )

    repl.run("dummy.ifc")

    out = capsys.readouterr().out
    assert "IFCWALL" in out  # rank
    assert "削除対象に追加しました" in out  # delete IFCWALLの応答
    assert "操作リスト" in out  # list
    assert "削除" in out  # list の日本語ラベル(Task6要件2)
    assert "中断しました" in out  # apply確認1でno
    assert len(calls) == 6  # 6コマンド分すべてinput()が呼ばれ、EOFで枯渇していない


def test_apply_with_no_intents_skips_confirmation_entirely(monkeypatch, capsys):
    """操作リストが空でapplyすると、確認すら出さずに即メッセージを返す。"""
    scan = _basic_fake_scan()
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_open(*a, **kw):
        raise AssertionError("空の操作リストでフルオープンされた")

    monkeypatch.setattr(ifcopenshell, "open", _boom_open)

    calls = _feed_input(monkeypatch, ["apply", "quit"])
    repl.run("dummy.ifc")

    # "quit"がすぐ読まれている(=applyがinput()で確認を取らず即returnした)。
    assert len(calls) == 2
    out = capsys.readouterr().out
    assert "操作が指定されていません" in out


# --- 3. est_fullopen_bytes 警告判定 ---


def test_apply_warns_when_est_fullopen_bytes_exceeds_threshold(monkeypatch, capsys):
    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)],
        elements={"IFCWALL": ["W0"]},
        est_fullopen_bytes=repl._FULLOPEN_WARN_BYTES + 1,
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(
        ifcopenshell, "open", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )
    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "n", "quit"])

    repl.run("dummy.ifc")

    out = capsys.readouterr().out
    assert "警告" in out


def test_apply_does_not_warn_when_est_fullopen_bytes_is_small(monkeypatch, capsys):
    scan = _basic_fake_scan()
    assert scan.est_fullopen_bytes < repl._FULLOPEN_WARN_BYTES
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(
        ifcopenshell, "open", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )
    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "n", "quit"])

    repl.run("dummy.ifc")

    out = capsys.readouterr().out
    assert "警告" not in out


# --- 4. apply 確認2段の脚本(モック export) ---


def test_apply_two_step_confirmation_opens_once_and_calls_export(monkeypatch, capsys, tmp_path):
    """delete対象=Wall(実ファイル)。確認1→確認2両方yesでフルオープン後、
    apply_operations(モック)にfileオブジェクト(再オープンなし)で渡ることを検証する。
    Wallは開口+窓を持つ合成フィクスチャなので、削除閉包(直接1+連鎖2)が実際に
    計算されプレビューに表示されることも確認する。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)],
        path=str(src_path),
        elements={"IFCWALL": [wall_gid]},
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    captured_calls = []
    fake_report = ExportReport(
        deleted=[wall_gid],
        simplified=[],
        skipped=[],
        warnings=[],
        output_path=str(tmp_path / "src_light.ifc"),
        stage_seconds={"open": 0.01, "deletes": 0.02, "write": 0.01},
    )

    def _fake_apply_operations(src, operations, output_path, **kwargs):
        captured_calls.append((src, operations, output_path, kwargs))
        return fake_report

    monkeypatch.setattr(repl, "apply_operations", _fake_apply_operations)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "", "y", "quit"])

    repl.run(str(src_path))

    assert len(captured_calls) == 1
    call_src, call_ops, call_output_path, call_kwargs = captured_calls[0]

    # フルオープンを1回に抑える: pathではなくifcopenshell.fileオブジェクトが渡る。
    assert isinstance(call_src, ifcopenshell.file)
    assert [op.op for op in call_ops] == ["delete"]
    assert call_ops[0].targets == [wall_gid]
    assert call_output_path == str(src_path.with_name("src_light.ifc"))
    assert callable(call_kwargs.get("progress"))
    # 由来刻印(CUI Phase2 Task1): 入力ファイル名をsource_nameとして明示的に渡す
    # (srcはfileオブジェクトのため、apply_operations側の既定導出"(in-memory)"には
    # 頼れない)。
    assert call_kwargs.get("source_name") == src_path.name

    out = capsys.readouterr().out
    assert "フルオープン" in out
    assert "直接1件" in out  # Wall本体
    assert "連鎖2件" in out  # 開口+窓
    assert str(fake_report.output_path) in out  # 結果表示


def _run_apply_with_flag_and_capture_kwargs(monkeypatch, tmp_path, **run_kwargs):
    """delete 1件を確認2まで進め、apply_operations(モック)が受け取った
    kwargs を返す(geometry_cleanup 貫通テスト用の共通脚本)。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)
    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)],
        path=str(src_path),
        elements={"IFCWALL": [wall_gid]},
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    captured_kwargs = {}
    fake_report = ExportReport(
        deleted=[wall_gid], simplified=[], skipped=[], warnings=[],
        output_path=str(tmp_path / "src_light.ifc"),
        stage_seconds={"open": 0.01, "deletes": 0.02, "write": 0.01},
    )

    def _fake_apply_operations(src, operations, output_path, **kwargs):
        captured_kwargs.update(kwargs)
        return fake_report

    monkeypatch.setattr(repl, "apply_operations", _fake_apply_operations)
    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "", "y", "quit"])

    repl.run(str(src_path), **run_kwargs)
    return captured_kwargs


def test_apply_passes_geometry_cleanup_gc_by_default(monkeypatch, tmp_path):
    """inline_cleanup 未指定(既定False)なら apply_operations に
    geometry_cleanup="gc" が明示的に渡ること(carry-forward Phase E)。"""
    kwargs = _run_apply_with_flag_and_capture_kwargs(monkeypatch, tmp_path)
    assert kwargs.get("geometry_cleanup") == "gc"


def test_apply_passes_geometry_cleanup_inline_when_flag_set(monkeypatch, tmp_path):
    """inline_cleanup=True なら apply_operations に geometry_cleanup="inline"
    が渡ること(CLI --inline-cleanup → repl.run → _run_apply の貫通)。"""
    kwargs = _run_apply_with_flag_and_capture_kwargs(
        monkeypatch, tmp_path, inline_cleanup=True
    )
    assert kwargs.get("geometry_cleanup") == "inline"


def test_apply_second_confirmation_declined_does_not_call_export(monkeypatch, capsys, tmp_path):
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom(*a, **kw):
        raise AssertionError("確認2でno答えたのにapply_operationsが呼ばれた")

    monkeypatch.setattr(repl, "apply_operations", _boom)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "", "n", "quit"])
    repl.run(str(src_path))  # _boom が呼ばれず例外が飛ばなければOK

    out = capsys.readouterr().out
    assert "中断しました" in out


def test_apply_detects_drift_between_scan_time_and_real_file_gids(monkeypatch, capsys, tmp_path):
    """スキャン時のGlobalIdが実ファイルに存在しない(drift)場合、件数を表示する。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 2)],
        path=str(src_path),
        elements={"IFCWALL": [wall_gid, "STALE_GID_NOT_IN_REAL_FILE_0001"]},
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(repl, "apply_operations", lambda *a, **kw: pytest.fail("到達しない"))

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "", "n", "quit"])
    repl.run(str(src_path))

    out = capsys.readouterr().out
    # 「1件」は削除連鎖プレビュー("直接1件+連鎖2件")にも出現するため、
    # 差異検出メッセージそのもの(「異なる要素をN件検出しました」)を対象に
    # 具体的な件数(1件)で照合し、他の"1件"表記との取り違えを避ける。
    assert "異なる要素を1件検出しました" in out


def test_print_report_translates_stage_labels_and_skips_zero_duration_stages(capsys):
    """結果表示のstage_secondsはexport.py内部キー(open/deletes/simplify/
    reextract_duplicates/consolidate/write)のままでは英語混じりになるため日本語化する。
    CUIはconsolidateを使わないため常に0.0だが(export.pyのapply_operations既定値)、
    それをそのまま表示すると毎回意味のない「0.0秒」行が出るので0秒のステージは省く。"""
    report = ExportReport(
        deleted=["G1"],
        simplified=["G2"],
        skipped=[],
        warnings=[],
        output_path="out.ifc",
        stage_seconds={
            "open": 1.2,
            "deletes": 3.4,
            "simplify": 5.6,
            "reextract_duplicates": 0.0,
            "consolidate": 0.0,
            "write": 0.7,
        },
    )

    repl._print_report(report)

    out = capsys.readouterr().out
    assert "開く: 1.2秒" in out
    assert "削除: 3.4秒" in out
    assert "簡略化: 5.6秒" in out
    assert "書き込み: 0.7秒" in out
    assert "reextract_duplicates" not in out
    assert "consolidate" not in out


def test_stage_seconds_labels_cover_every_key_apply_operations_sets(tmp_path):
    """_STAGE_SECONDS_LABELS が export.apply_operations の実際のキー集合を
    網羅していることを固定する番人テスト(フェーズ最終レビューI3)。

    "gc" ステージが追加された際にこの辞書への追記が漏れ、CUI/GUIの日本語
    表示に英語キーがそのまま混ざる欠陥が実際に起きた。モックせず本物の
    apply_operations を極小フィクスチャで1回実行し、返ってきた
    stage_seconds の全キーがラベル辞書に存在することを確認する
    (キーを1つ削れば赤くなることは報告書で確認済み)。"""
    from ifc_occam.core.export import apply_operations
    from ifc_occam.core.ops import Operation

    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src = tmp_path / "src.ifc"
    f.write(str(src))
    out = tmp_path / "out.ifc"
    ops = [Operation(op="delete", targets=[wall_gid])]

    report = apply_operations(str(src), ops, str(out))

    missing = set(report.stage_seconds) - set(repl._STAGE_SECONDS_LABELS)
    assert missing == set(), f"ラベル未登録のstage_secondsキー: {missing}"


# --- 4b. 出力ファイル名プロンプト(監督者裁定2026-07-25、要件§5モック準拠) ---
#
# 確認1が通った後・フルオープンの前に「出力ファイル名 [既定値]: 」を表示する
# (design.md §6 手順1b)。空Enter=既定値の再利用、入力あり=_resolve_cui_output_path
# で既定値/--outputと同一基準(入力ファイルと同じディレクトリ)に解決する(重複実装
# しない。レビューア指摘2026-07-25で改訂: 入力値だけがcwd基準だった不整合を解消)。
# CLI --output指定時はプロンプトを出さず、従来どおりその値を使う(非対話経路は無変更)。


def _apply_output_prompt_fixture(monkeypatch, tmp_path):
    """このセクション共通のfixture: Wall1件を削除対象にしたシナリオを組み立てる。
    apply_operationsはモックし、渡されたoutput_pathをそのままcaptured_callsに記録する
    (このセクションの関心は「repl.pyがapply_operationsに何を渡すか」だけであり、
    export.py自体のcwd解決ロジック(Task 5、既存・無変更)は対象外)。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    captured_calls: list[str] = []

    def _fake_apply_operations(src, operations, output_path, **kwargs):
        captured_calls.append(output_path)
        return ExportReport(
            deleted=[wall_gid],
            simplified=[],
            skipped=[],
            warnings=[],
            output_path=output_path,
            stage_seconds={},
        )

    monkeypatch.setattr(repl, "apply_operations", _fake_apply_operations)
    return src_path, captured_calls


def test_apply_prompts_for_output_filename_after_first_confirmation(monkeypatch, capsys, tmp_path):
    """確認1(y)が通った直後・フルオープンより前に、既定値を角括弧で示した
    出力ファイル名プロンプトを表示する。"""
    src_path, _ = _apply_output_prompt_fixture(monkeypatch, tmp_path)
    default_name = f"{src_path.stem}_light{src_path.suffix}"

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "", "y", "quit"])
    repl.run(str(src_path))

    assert any(c == f"出力ファイル名 [{default_name}]: " for c in calls)


def test_apply_output_filename_prompt_blank_reuses_existing_default_logic(
    monkeypatch, capsys, tmp_path
):
    """空Enterは既定値になる。既定値は`_resolve_cui_output_path(path, None)`
    (既存の非対話時の既定値ロジック)の戻り値そのもの — 重複実装しないことの確認。"""
    src_path, captured_calls = _apply_output_prompt_fixture(monkeypatch, tmp_path)
    expected_default = repl._resolve_cui_output_path(str(src_path), None)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "", "y", "quit"])
    repl.run(str(src_path))

    assert captured_calls == [expected_default]


def test_apply_output_filename_prompt_typed_value_resolves_against_input_directory(
    monkeypatch, capsys, tmp_path
):
    """プロンプトに入力があった場合も、既定値/--outputと同じ基準
    (`_resolve_cui_output_path`、入力ファイルと同じディレクトリ)で解決済みの
    パスがapply_operationsに渡ること(レビューア指摘2026-07-25: 入力値だけが
    cwd基準になっていた不整合の修正)。"""
    src_path, captured_calls = _apply_output_prompt_fixture(monkeypatch, tmp_path)
    expected = repl._resolve_cui_output_path(str(src_path), "custom_name.ifc")

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "custom_name.ifc", "y", "quit"])
    repl.run(str(src_path))

    assert captured_calls == [expected]


def test_apply_with_explicit_cli_output_skips_filename_prompt(monkeypatch, capsys, tmp_path):
    """CLIで--outputが指定されている場合はプロンプトを出さず、従来どおりその値を
    使う(非対話経路は無変更: 入力列は旧脚本と同じ5個で足りる)。"""
    src_path, captured_calls = _apply_output_prompt_fixture(monkeypatch, tmp_path)
    explicit_output = str(tmp_path / "explicit_out.ifc")

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "y", "quit"])
    repl.run(str(src_path), output=explicit_output)

    assert not any("出力ファイル名" in c for c in calls)
    assert len(calls) == 5
    assert captured_calls == [explicit_output]


# --- 4c. テキストモード分岐(CUI Phase3 Task5、docs/plans/2026-07-25-cui-phase3.md
#         Task5、監督者裁定1-8) ---
#
# 手順1(操作サマリ+est_fullopen_bytes警告判定)の後・確認1の直前に、
# 「intents が delete のみ」かつ(--text指定 or est_fullopen_bytes警告該当)の
# 場合に限り、テキストモード(フルオープン不要)を提案する1問を割り込ませる。
# y→テキスト経路(scan_full_graph→compute_text_delete_plan→確認2→出力
# ファイル名プロンプト→rewrite_without)。n→そのまま従来の確認1に落ちる
# (挙動を1バイトも変えない)。bbox/hull/decimate/keepが1件でも混在すれば
# このプロンプト自体を出さない(delete-onlyの定義、監督者裁定1)。


def test_apply_text_mode_yes_calls_rewrite_without_and_skips_fullopen(
    monkeypatch, capsys, tmp_path
):
    """delete のみのintents + --text で、確認1直前のテキストモード問いにyと
    答えるとrewrite_without(モック)が呼ばれ、ifcopenshell.openは一度も
    呼ばれない(監督者裁定8)。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_open(*a, **kw):
        raise AssertionError("テキスト経路でifcopenshell.openが呼ばれた(監督者裁定8違反)")

    monkeypatch.setattr(ifcopenshell, "open", _boom_open)

    captured_calls = []

    def _fake_rewrite_without(src, out, plan, graph, source_name, progress=None):
        captured_calls.append((src, out, plan, graph, source_name))
        if progress is not None:
            progress("rewrite", 1, 1)
        return RewriteReport(
            records_in=10, records_dropped=3, rels_patched=1, rels_dropped=0, bytes_out=1234
        )

    monkeypatch.setattr(repl, "rewrite_without", _fake_rewrite_without)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "y", "", "quit"])
    repl.run(str(src_path), text=True)

    assert len(captured_calls) == 1
    call_src, call_out, call_plan, call_graph, call_source_name = captured_calls[0]
    assert str(call_src) == str(src_path)
    assert call_out == str(src_path.with_name("src_light.ifc"))
    assert call_source_name == src_path.name
    assert call_plan.stats["seeds"] == 1

    # 問いのプロンプト文字列自体はinput()にしか渡らずcapsysには現れないため
    # (実端末ならエコーされるが、_feed_inputのスタブはエコーしない)、
    # calls(_feed_inputが記録したinput()の各prompt引数)側で照合する。
    assert any("テキストモードで適用しますか" in c for c in calls)
    out = capsys.readouterr().out
    assert "出力ファイル: " in out


def test_text_mode_completes_with_inline_cleanup_flag_and_never_calls_apply_operations(
    monkeypatch, capsys, tmp_path
):
    """`--inline-cleanup` を付けてもテキストモード経路(_run_text_apply)には
    一切干渉しない(裁定2: テキストモードには効果を持たず、apply_operations
    にも到達しない)ことを固定する。フェーズ最終レビューM-2。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_open(*a, **kw):
        raise AssertionError("テキスト経路でifcopenshell.openが呼ばれた(監督者裁定8違反)")

    monkeypatch.setattr(ifcopenshell, "open", _boom_open)

    def _fake_rewrite_without(src, out, plan, graph, source_name, progress=None):
        if progress is not None:
            progress("rewrite", 1, 1)
        return RewriteReport(
            records_in=10, records_dropped=3, rels_patched=1, rels_dropped=0, bytes_out=1234
        )

    monkeypatch.setattr(repl, "rewrite_without", _fake_rewrite_without)

    def _boom_apply_operations(*a, **kw):
        raise AssertionError(
            "--inline-cleanup 付きでもテキスト経路でapply_operationsが呼ばれてはならない"
        )

    monkeypatch.setattr(repl, "apply_operations", _boom_apply_operations)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "y", "", "quit"])
    repl.run(str(src_path), text=True, inline_cleanup=True)

    out = capsys.readouterr().out
    assert "出力ファイル: " in out


def test_apply_text_mode_typed_output_filename_resolves_against_input_directory(
    monkeypatch, capsys, tmp_path
):
    """テキスト経路の出力ファイル名プロンプトも手順1bと同一規約であること
    (監督者裁定4): 相対パスを入力すると cwd ではなく**入力ファイルと同じ
    ディレクトリ**を基準に解決される。

    Task 5 レビューの Minor(テキスト経路側からこの規約を直接検証する
    テストが無い)を監督者が引き取って追加。cwd を入力ファイルとは別の
    ディレクトリへ移した状態で実行し、期待値を具体的な絶対パスで固定する
    ことで「cwd 基準になっていない」ことを積極的に証明する(過去に
    フルオープン経路側で入力値だけが cwd 基準になっていた不整合を
    修正した経緯があるため、テキスト経路でも再発を防ぐ)。
    """
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_dir = tmp_path / "input_dir"
    src_dir.mkdir()
    src_path = _write_ifc(f, src_dir)

    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    captured_out_paths = []

    def _fake_rewrite_without(src, out, plan, graph, source_name, progress=None):
        captured_out_paths.append(out)
        return RewriteReport(
            records_in=10, records_dropped=3, rels_patched=1, rels_dropped=0, bytes_out=1234
        )

    monkeypatch.setattr(repl, "rewrite_without", _fake_rewrite_without)

    _feed_input(
        monkeypatch, ["delete IFCWALL", "apply", "y", "y", "custom_name.ifc", "quit"]
    )
    repl.run(str(src_path), text=True)

    assert captured_out_paths == [str(src_path.with_name("custom_name.ifc"))]
    assert not (other_cwd / "custom_name.ifc").exists()


def test_apply_no_text_prompt_when_intents_include_bbox(monkeypatch, capsys):
    """bbox混在(delete-onlyでない)場合、est_fullopen_bytes警告該当でも
    テキストモード問い自体を出さない(従来経路のみ、brief記載どおり)。"""
    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1), _stats("IFCSLAB", 1)],
        elements={"IFCWALL": ["W0"], "IFCSLAB": ["S0"]},
        est_fullopen_bytes=repl._FULLOPEN_WARN_BYTES + 1,
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(
        ifcopenshell, "open", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )

    def _boom_scan_full_graph(*a, **kw):
        raise AssertionError("bbox混在なのにテキスト経路(scan_full_graph)に入った")

    monkeypatch.setattr(repl, "scan_full_graph", _boom_scan_full_graph)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "bbox IFCSLAB", "apply", "n", "quit"])
    repl.run("dummy.ifc")

    # プロンプト文字列はinput()にしか渡らないためcalls側で確認する
    # (out/capsys側では「出さなかった」ことと「input()モックがエコーしない」
    # ことを区別できず、偽陽性になる)。
    assert not any("テキストモードで適用しますか" in c for c in calls)


def test_apply_no_text_prompt_when_intents_include_keep(monkeypatch, capsys):
    """keep混在もdelete-onlyとみなさない(監督者裁定1: keepはフルオープン
    経路の閉包計算に効く明示指定であり、テキスト経路には相当機能が無いため
    黙って無視してはならない)。est_fullopen_bytes警告該当でもプロンプト
    自体を出さない。"""
    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1), _stats("IFCSLAB", 1)],
        elements={"IFCWALL": ["W0"], "IFCSLAB": ["S0"]},
        est_fullopen_bytes=repl._FULLOPEN_WARN_BYTES + 1,
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(
        ifcopenshell, "open", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )

    def _boom_scan_full_graph(*a, **kw):
        raise AssertionError("keep混在なのにテキスト経路(scan_full_graph)に入った")

    monkeypatch.setattr(repl, "scan_full_graph", _boom_scan_full_graph)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "keep IFCSLAB", "apply", "n", "quit"])
    repl.run("dummy.ifc")

    assert not any("テキストモードで適用しますか" in c for c in calls)
    out = capsys.readouterr().out
    assert "テキストモードは削除のみ対応" not in out


def test_apply_text_flag_with_non_delete_intents_falls_back_with_message(monkeypatch, capsys):
    """--text指定かつdelete以外のintentがある場合、テキストモード問い自体は
    出さず「テキストモードは削除のみ対応」と表示して従来経路にフォールバック
    する(監督者裁定3)。"""
    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1), _stats("IFCSLAB", 1)],
        elements={"IFCWALL": ["W0"], "IFCSLAB": ["S0"]},
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_scan_full_graph(*a, **kw):
        raise AssertionError("フォールバックしたはずなのにscan_full_graphが呼ばれた")

    monkeypatch.setattr(repl, "scan_full_graph", _boom_scan_full_graph)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "bbox IFCSLAB", "apply", "n", "quit"])
    repl.run("dummy.ifc", text=True)

    out = capsys.readouterr().out
    assert "テキストモードは削除のみ対応" in out
    assert not any("テキストモードで適用しますか" in c for c in calls)


def test_apply_no_text_prompt_when_text_flag_absent_and_warning_not_triggered(
    monkeypatch, capsys
):
    """--text未指定・est_fullopen_bytes警告非該当なら、delete onlyであっても
    テキストモード問い自体を出さない(従来と完全に同一の挙動)。"""
    scan = _basic_fake_scan()
    assert scan.est_fullopen_bytes < repl._FULLOPEN_WARN_BYTES
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_scan_full_graph(*a, **kw):
        raise AssertionError("プロンプト非表示のはずなのにscan_full_graphが呼ばれた")

    monkeypatch.setattr(repl, "scan_full_graph", _boom_scan_full_graph)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "n", "quit"])
    repl.run("dummy.ifc")

    assert not any("テキストモード" in c for c in calls)
    out = capsys.readouterr().out
    assert "テキストモード" not in out


def test_apply_text_prompt_shown_when_warning_triggered_without_text_flag(monkeypatch, capsys):
    """delete onlyかつest_fullopen_bytes警告該当なら、--text未指定でも
    テキストモード問いを出す(発動条件のor側)。nと答えれば従来の確認1に
    落ちることも確認する。"""
    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)],
        elements={"IFCWALL": ["W0"]},
        est_fullopen_bytes=repl._FULLOPEN_WARN_BYTES + 1,
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(
        ifcopenshell, "open", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "n", "n", "quit"])
    repl.run("dummy.ifc")

    assert any("テキストモードで適用しますか" in c for c in calls)
    out = capsys.readouterr().out
    assert "中断しました" in out


def test_apply_text_prompt_declined_falls_through_to_unmodified_fullopen_path(
    monkeypatch, capsys, tmp_path
):
    """テキストモード問いにnと答えると、そのまま既存の確認1に落ちて従来の
    フルオープン経路が完全に(1バイトも変えず)動く(監督者裁定2)。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    captured_calls = []
    fake_report = ExportReport(
        deleted=[wall_gid],
        simplified=[],
        skipped=[],
        warnings=[],
        output_path=str(tmp_path / "src_light.ifc"),
        stage_seconds={},
    )

    def _fake_apply_operations(src, operations, output_path, **kwargs):
        captured_calls.append((src, operations, output_path, kwargs))
        return fake_report

    monkeypatch.setattr(repl, "apply_operations", _fake_apply_operations)

    def _boom_rewrite_without(*a, **kw):
        raise AssertionError("nと答えたのにテキスト経路(rewrite_without)が呼ばれた")

    monkeypatch.setattr(repl, "rewrite_without", _boom_rewrite_without)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "n", "y", "", "y", "quit"])
    repl.run(str(src_path), text=True)

    assert len(captured_calls) == 1
    assert any("テキストモードで適用しますか" in c for c in calls)
    out = capsys.readouterr().out
    assert "フルオープン" in out


def test_apply_text_mode_falls_back_when_seeds_zero(monkeypatch, capsys, tmp_path):
    """指定クラスが実ファイルの参照グラフ上に1件も見つからない
    (plan.stats['seeds']==0)場合、出力を書かずに従来のフルオープン経路へ
    フォールバックする(監督者裁定5: 黙って書くと「削除したつもりで中身が
    元と同じファイル」という最も危険な失敗モードになる)。"""
    f = build_wall_with_window_ifc()  # 実ファイルにIFCSLABは存在しない
    src_path = _write_ifc(f, tmp_path)

    # スキャン結果はIFCSLABが1件あると(偽って)報告する
    # (軽量スキャンと実ファイルのdriftを模した状況)。
    scan = _fake_scan(
        stats=[_stats("IFCSLAB", 1)], path=str(src_path), elements={"IFCSLAB": ["S0"]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_apply_operations(*a, **kw):
        raise AssertionError("フォールバック後、確認1でnと答えたのにapply_operationsが呼ばれた")

    monkeypatch.setattr(repl, "apply_operations", _boom_apply_operations)

    def _boom_rewrite_without(*a, **kw):
        raise AssertionError("seeds==0のはずなのにrewrite_withoutが呼ばれた")

    monkeypatch.setattr(repl, "rewrite_without", _boom_rewrite_without)

    _feed_input(monkeypatch, ["delete IFCSLAB", "apply", "y", "n", "quit"])
    repl.run(str(src_path), text=True)

    out = capsys.readouterr().out
    assert "指定クラスが参照グラフ上で見つかりませんでした" in out
    assert "中断しました" in out


def test_print_rewrite_report_shows_output_file_and_counts(capsys):
    report = RewriteReport(
        records_in=1000, records_dropped=42, rels_patched=7, rels_dropped=3, bytes_out=123456
    )

    repl._print_rewrite_report(report, "out_text.ifc")

    out = capsys.readouterr().out
    assert "out_text.ifc" in out
    assert "42" in out
    assert "7" in out
    assert "3" in out
    assert "123,456" in out


# --- 5. Ctrl+C(KeyboardInterrupt)/EOFで安全終了 ---


def test_keyboard_interrupt_in_main_loop_exits_safely(monkeypatch, capsys):
    scan = _fake_scan([])
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _raise_kbd(prompt: str = "") -> str:
        raise KeyboardInterrupt()

    monkeypatch.setattr("builtins.input", _raise_kbd)

    repl.run("dummy.ifc")  # 例外が伝播しなければOK

    out = capsys.readouterr().out
    assert "中断しました" in out


def test_eof_in_main_loop_exits_safely(monkeypatch, capsys):
    scan = _fake_scan([])
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise_eof)

    repl.run("dummy.ifc")

    out = capsys.readouterr().out
    assert "中断しました" in out


def test_keyboard_interrupt_during_apply_confirmation_exits_safely(monkeypatch, capsys):
    """applyフロー内部のinput()(確認1)でCtrl+Cが起きても、メインループ同様に
    安全終了する(ネストしていても同じtry/exceptで拾われることの確認)。"""
    scan = _basic_fake_scan()
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    lines = iter(["delete IFCWALL", "apply"])

    def _fake_input(prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise KeyboardInterrupt()

    monkeypatch.setattr("builtins.input", _fake_input)

    repl.run("dummy.ifc")  # 例外が伝播しなければOK(確認1のinput()でKeyboardInterrupt)

    out = capsys.readouterr().out
    assert "中断しました" in out


def test_keyboard_interrupt_during_output_filename_prompt_exits_safely(monkeypatch, capsys):
    """1b(出力ファイル名プロンプト)のinput()でCtrl+Cが起きても、確認1と同様に
    メインループの1箇所catchで安全終了する(レビューア指摘Minorの解消)。"""
    scan = _basic_fake_scan()
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    lines = iter(["delete IFCWALL", "apply", "y"])

    def _fake_input(prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise KeyboardInterrupt()

    monkeypatch.setattr("builtins.input", _fake_input)

    def _boom_open(*a, **kw):
        raise AssertionError("1bでKeyboardInterruptしたのにフルオープンされた")

    monkeypatch.setattr(ifcopenshell, "open", _boom_open)

    repl.run("dummy.ifc")  # 例外が伝播しなければOK(1bのinput()でKeyboardInterrupt)

    out = capsys.readouterr().out
    assert "中断しました" in out


# --- 6. ヘルプ(監督者確定要件1: undo番号省略の挙動明記) ---


def test_help_command_mentions_undo_omitted_number_behavior(monkeypatch, capsys):
    scan = _fake_scan([])
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    _feed_input(monkeypatch, ["help", "quit"])

    repl.run("dummy.ifc")

    out = capsys.readouterr().out
    assert "undo" in out
    assert "番号省略" in out
    assert "最終行" in out


def test_help_shorthand_h_also_prints_help(monkeypatch, capsys):
    scan = _fake_scan([])
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    _feed_input(monkeypatch, ["h", "quit"])

    repl.run("dummy.ifc")

    out = capsys.readouterr().out
    assert "コマンド一覧" in out


# --- 7. 進捗表示の間引き(監督者確定要件3) ---


def test_progress_printer_thins_output_to_stride_and_final_only(capsys):
    progress = repl._make_progress_printer()
    total = 1000
    for done in range(1, total + 1):
        progress("delete", done, total)

    out = capsys.readouterr().out
    assert out.count("\n") == 1  # ステージ完了時の1回だけ改行される
    assert "1/1000" in out  # 最初の1件
    assert "500/1000" in out  # 500件ごと
    assert "1000/1000" in out  # 最終件
    assert "37/1000" not in out  # 間引かれる(ストライドに乗らない件数)
    assert "499/1000" not in out


def test_progress_printer_starts_fresh_line_for_next_stage(capsys):
    progress = repl._make_progress_printer()
    progress("delete", 1, 3)
    progress("delete", 2, 3)
    progress("delete", 3, 3)
    progress("simplify", 1, 2)
    progress("simplify", 2, 2)

    out = capsys.readouterr().out
    assert out.count("\n") == 2  # delete完了時+simplify完了時、それぞれ1回


def test_progress_printer_does_not_print_one_line_per_element():
    """1要素1行の出力は禁止(監督者確定要件3)。100件で出力行(確定行)は1行のみ。"""
    import io
    import contextlib

    progress = repl._make_progress_printer()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for done in range(1, 101):
            progress("simplify", done, 100)

    printed = buf.getvalue()
    assert printed.count("\n") == 1


def test_progress_printer_uses_japanese_label_for_rewrite_stage(capsys):
    """CUI Phase3 Task5: `_STAGE_LABELS` に"rewrite"用の日本語ラベルが
    追加されていること(監督者裁定6)。"""
    progress = repl._make_progress_printer()
    progress("rewrite", 1, 1)

    out = capsys.readouterr().out
    assert "書き換え中" in out


# --- 8. 出力先パスの解決 ---


def test_default_output_path_is_next_to_input_regardless_of_cwd(tmp_path, monkeypatch):
    src = tmp_path / "sub" / "model.ifc"
    src.parent.mkdir()
    src.touch()
    monkeypatch.chdir(tmp_path)  # cwdが入力ファイルのディレクトリと異なることを保証

    resolved = repl._resolve_cui_output_path(str(src), None)

    assert resolved == str(src.with_name("model_light.ifc"))


def test_explicit_relative_output_resolves_against_input_directory(tmp_path, monkeypatch):
    src = tmp_path / "sub" / "model.ifc"
    src.parent.mkdir()
    src.touch()
    monkeypatch.chdir(tmp_path)

    resolved = repl._resolve_cui_output_path(str(src), "custom_out.ifc")

    assert resolved == str(src.with_name("custom_out.ifc"))


def test_explicit_absolute_output_is_used_as_is(tmp_path):
    src = tmp_path / "model.ifc"
    src.touch()
    absolute_out = tmp_path / "elsewhere" / "out.ifc"

    resolved = repl._resolve_cui_output_path(str(src), str(absolute_out))

    assert resolved == str(absolute_out)


# --- 9. 統合: small.ifc で `cui --scan-only` がランキングを出力(通常テスト) ---


def test_scan_only_on_real_small_ifc_prints_ranking(small_ifc_path, capsys):
    repl.run(str(small_ifc_path), scan_only=True)

    out = capsys.readouterr().out
    assert "クラス別ランキング" in out
    assert "スキーマ" in out


# --- 10. E2E: delete IFCGRID + bbox 最大クラス → apply(確認yes) → 出力生成 →
#           diagnoseで三角形数減を確認(通常テスト、small.ifc使用) ---


def test_e2e_delete_grid_and_bbox_top_class_then_apply_reduces_triangle_count(
    tmp_path, small_ifc_path, monkeypatch
):
    top_class = scan_file(small_ifc_path).stats[0].ifc_class
    assert top_class != "IFCGRID"

    src_copy = tmp_path / "small_src.ifc"
    shutil.copy(small_ifc_path, src_copy)
    expected_output = src_copy.with_name("small_src_light.ifc")

    _feed_input(
        monkeypatch,
        ["rank", "delete IFCGRID", f"bbox {top_class}", "list", "apply", "y", "", "y", "quit"],
    )

    repl.run(str(src_copy))

    assert expected_output.exists()

    model_before, _ = extract_model(src_copy)
    total_before = sum(s.total_triangles for s in aggregate_by_class(model_before))

    model_after, _ = extract_model(expected_output)
    total_after = sum(s.total_triangles for s in aggregate_by_class(model_after))

    assert total_after < total_before


# --- 11. E2E: テキストモード(--text)でsmall.ifcの少要素数クラスをdelete →
#            出力再オープンで対象クラス0件、ifcopenshell.open未呼び出し
#            (brief Step1(d)、監督者裁定8) ---


def test_e2e_text_mode_delete_ductsilencer_on_small_ifc_removes_class_without_fullopen(
    tmp_path, small_ifc_path, monkeypatch
):
    """small.ifc実物で、少要素数クラス(IfcDuctSilencer、44要素。
    tests/test_cui_phase3_equivalence.pyの等価性試験でも選定済み: cascade=0で
    実行時間が予測可能)をdelete指定+--textでテキストモード適用し、
    (1) ifcopenshell.openが一度も呼ばれないこと(監督者裁定8)
    (2) 出力を再オープンして対象クラスが0件であること
    を確認する(brief Step1(d))。"""
    target_class = "IFCDUCTSILENCER"

    src_copy = tmp_path / "small_src.ifc"
    shutil.copy(small_ifc_path, src_copy)
    expected_output = src_copy.with_name("small_src_light.ifc")

    open_calls: list[tuple] = []
    real_open = ifcopenshell.open

    def _tracking_open(*a, **kw):
        open_calls.append((a, kw))
        return real_open(*a, **kw)

    monkeypatch.setattr(ifcopenshell, "open", _tracking_open)

    _feed_input(monkeypatch, [f"delete {target_class}", "apply", "y", "y", "", "quit"])
    repl.run(str(src_copy), text=True)

    assert open_calls == [], "テキスト経路でifcopenshell.openが呼ばれた(監督者裁定8違反)"
    assert expected_output.exists()

    reopened = ifcopenshell.open(str(expected_output))
    remaining = reopened.by_type("IfcDuctSilencer")
    assert remaining == [], f"対象クラスが{len(remaining)}件残存している"


# --- 12. C1(Critical、フェーズ最終レビュー): UI層(両経路)での出力先=入力先ガード ---
#
# ライブラリ層(textops/rewrite.py)のガードだけでは、フルオープン経路
# (apply_operationsのfileオブジェクト経由の書き込み)を保護できない
# (原本非破壊はこのツールの契約であり、フルオープン経路もtruncateしないだけで
# 原本上書きは同様に契約違反)。repl.py側で、解決後のoutput_pathが入力ファイルと
# 同一実体なら適用を開始せずエラー表示して中断する。プロンプト入力値・--output
# 指定値の両方、テキスト経路・フルオープン経路の両方に効かせる。


def test_apply_fullopen_path_refuses_when_output_prompt_repeats_input_filename(
    monkeypatch, capsys, tmp_path
):
    """フルオープン経路: 出力ファイル名プロンプトに入力ファイルと同じ名前を
    打つと、フルオープンを開始せずエラー表示して中断する。原本コピーは
    サイズ・ハッシュとも無傷であること。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)
    before_size = src_path.stat().st_size
    before_hash = hashlib.sha256(src_path.read_bytes()).hexdigest()

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_open(*a, **kw):
        raise AssertionError("同一パスガードより前にフルオープンされた")

    monkeypatch.setattr(ifcopenshell, "open", _boom_open)

    def _boom_apply_operations(*a, **kw):
        raise AssertionError("同一パスガードより前にapply_operationsが呼ばれた")

    monkeypatch.setattr(repl, "apply_operations", _boom_apply_operations)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", src_path.name, "quit"])
    repl.run(str(src_path))

    out = capsys.readouterr().out
    assert "エラー" in out
    assert src_path.stat().st_size == before_size
    assert hashlib.sha256(src_path.read_bytes()).hexdigest() == before_hash


def test_apply_fullopen_path_refuses_when_cli_output_equals_input(
    monkeypatch, capsys, tmp_path
):
    """フルオープン経路: `--output` に入力ファイルと同じパスを指定した場合も
    (プロンプトを経由しない経路)同じガードで弾く。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)
    before_size = src_path.stat().st_size
    before_hash = hashlib.sha256(src_path.read_bytes()).hexdigest()

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(
        ifcopenshell, "open", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )
    monkeypatch.setattr(
        repl, "apply_operations", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "quit"])
    repl.run(str(src_path), output=str(src_path))

    assert not any("出力ファイル名" in c for c in calls)  # --output指定時はプロンプト無し
    out = capsys.readouterr().out
    assert "エラー" in out
    assert src_path.stat().st_size == before_size
    assert hashlib.sha256(src_path.read_bytes()).hexdigest() == before_hash


def test_apply_text_path_refuses_when_output_prompt_repeats_input_filename(
    monkeypatch, capsys, tmp_path
):
    """テキスト経路: 出力ファイル名プロンプトに入力ファイルと同じ名前を打つと、
    rewrite_withoutを呼ばずエラー表示して中断する。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)
    before_size = src_path.stat().st_size
    before_hash = hashlib.sha256(src_path.read_bytes()).hexdigest()

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_rewrite_without(*a, **kw):
        raise AssertionError("同一パスガードより前にrewrite_withoutが呼ばれた")

    monkeypatch.setattr(repl, "rewrite_without", _boom_rewrite_without)

    _feed_input(
        monkeypatch, ["delete IFCWALL", "apply", "y", "y", src_path.name, "quit"]
    )
    repl.run(str(src_path), text=True)

    out = capsys.readouterr().out
    assert "エラー" in out
    assert src_path.stat().st_size == before_size
    assert hashlib.sha256(src_path.read_bytes()).hexdigest() == before_hash


def test_apply_text_path_refuses_when_cli_output_equals_input(monkeypatch, capsys, tmp_path):
    """テキスト経路: `--output` に入力ファイルと同じパスを指定した場合も
    同じガードで弾く(プロンプトを経由しない経路)。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)
    before_size = src_path.stat().st_size
    before_hash = hashlib.sha256(src_path.read_bytes()).hexdigest()

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_rewrite_without(*a, **kw):
        raise AssertionError("同一パスガードより前にrewrite_withoutが呼ばれた")

    monkeypatch.setattr(repl, "rewrite_without", _boom_rewrite_without)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "y", "quit"])
    repl.run(str(src_path), output=str(src_path), text=True)

    assert not any("出力ファイル名" in c for c in calls)
    out = capsys.readouterr().out
    assert "エラー" in out
    assert src_path.stat().st_size == before_size
    assert hashlib.sha256(src_path.read_bytes()).hexdigest() == before_hash


def test_cli_output_equals_input_is_refused_before_the_reference_graph_scan(
    monkeypatch, capsys, tmp_path
):
    """`--output` 指定時の同一パス衝突は、参照グラフスキャンより**前**に弾くこと(N2)。

    `--output` の値は `_run_apply` 冒頭の `_resolve_cui_output_path` で確定して
    いるので、テキスト経路の終盤(参照グラフスキャン+確認2の後)まで判定を
    遅らせる理由がない。遅いままだと、このツールの本来の対象である多GB
    ファイルで数十分のスキャンを丸ごと無駄にした上、実行不可能な操作に
    ユーザーが確認を答えることになる。
    """
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _boom_scan_full_graph(*a, **kw):
        raise AssertionError("同一パスガードより前に参照グラフスキャンが走った")

    monkeypatch.setattr(repl, "scan_full_graph", _boom_scan_full_graph)

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "quit"])
    repl.run(str(src_path), output=str(src_path), text=True)

    out = capsys.readouterr().out
    assert "エラー" in out
    assert "参照グラフスキャン中" not in out
    # テキストモードの提案も確認1も出さずに即中断している(入力は3行で足りる)。
    assert len(calls) == 3
    assert not any("テキストモードで適用しますか" in c for c in calls)


# --- 13. I2(Important): 確認2の数値表示を正直なラベルにする ---


def test_text_mode_confirm2_labels_are_honest_about_candidates_and_lower_bound(
    monkeypatch, capsys, tmp_path
):
    """テキストモード確認2の表示:
      - 削除数は「確定分〜上限」の範囲で示され、実行時に増える場合があることが
        伝わる(N3: 下限だけの表示では隠れている差の大きさが伝わらないため、
        追加パス無しで計算できる厳密な上限=確定分+rel patch候補全件を併記する)。
      - rel patch件数は「候補」であり、実行時に修正/削除へ分岐すること・
        内訳は実行後の結果表示で確定することが明示される。
      - 旧ラベル(候補である旨の注記なしの「参照リスト修正予定」、および
        「参照リストにより削除される関係レコード(rel drop)」)は出ない。
    """
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "n", "quit"])
    repl.run(str(src_path), text=True)

    out = capsys.readouterr().out
    assert "参照リスト修正予定" not in out
    assert "参照リストにより削除される関係レコード" not in out
    assert "削除レコード数(見積り)" in out
    assert "確定分〜" in out
    assert "候補" in out
    assert "rel patch候補" in out
    assert "実行後の結果表示" in out

    # N3: 範囲の両端が plan の実数から作られていることを、実際の plan と
    # 突き合わせて確認する(このフィクスチャは rel patch候補が0件なので範囲は
    # 縮退する。非縮退ケースは次のテストで固定する)。
    graph = scan_full_graph(str(src_path))
    plan = compute_text_delete_plan(graph, ["IFCWALL"])
    lower = int(plan.drop_ids.size)
    upper = lower + plan.stats["rels_patched"]
    assert f"{lower}〜{upper}件" in out


def test_text_mode_confirm2_upper_bound_adds_rel_patch_candidates(
    monkeypatch, capsys, tmp_path
):
    """N3(非縮退ケース): 上限 = 確定分 + rel patch候補 になっていること。

    合成フィクスチャは rel patch候補が0件で範囲が縮退するため、候補を持つ
    plan を差し込んで範囲の作られ方そのものを固定する(上限を下限と同じに
    したり、候補数を足し忘れたりしていないことの証明)。
    """
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    fake_plan = TextDeletePlan(
        drop_ids=np.array([11, 12, 13], dtype=np.int64),
        patch_rel_ids=np.array([21, 22, 23, 24, 25, 26, 27], dtype=np.int64),
        stats={
            "seeds": 1,
            "cascade": 2,
            "swept": 0,
            "rels_dropped": 0,
            "rels_patched": 7,
        },
    )
    monkeypatch.setattr(repl, "compute_text_delete_plan", lambda graph, classes: fake_plan)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "n", "quit"])
    repl.run(str(src_path), text=True)

    out = capsys.readouterr().out
    assert "3〜10件" in out


# --- 14. M5(i): テキストモード提案の疑問符はASCII `?` に揃える ---


def test_text_mode_prompt_uses_ascii_question_mark_not_fullwidth(monkeypatch, capsys):
    """他の確認プロンプト(確認1・確認2など)はすべてASCII `?` を使っており、
    テキストモード提案の1問だけが全角 `？` になっていた不整合を直す。"""
    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)],
        elements={"IFCWALL": ["W0"]},
        est_fullopen_bytes=repl._FULLOPEN_WARN_BYTES + 1,
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    monkeypatch.setattr(
        ifcopenshell, "open", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    )

    calls = _feed_input(monkeypatch, ["delete IFCWALL", "apply", "n", "n", "quit"])
    repl.run("dummy.ifc")

    prompt = next(c for c in calls if "テキストモードで適用しますか" in c)
    assert "?" in prompt
    assert "？" not in prompt


# --- 15. M5(ii): quitが「ファイルは変更されていません」と言うのは、
#         このセッションで一度も書き込みが起きていない場合だけにする ---
#
# applyが実際に出力ファイルを書いた後にquit/Ctrl+C/EOFすると、無条件の
# 「中断しました。ファイルは変更されていません。」は事実に反する(何らかの
# 出力ファイルが書き込まれている)。適用でファイルを書いたかどうかを追跡し、
# 書いた場合はこの文言を出さない。両経路(テキスト/フルオープン)で効かせる。


def test_quit_after_successful_fullopen_apply_does_not_claim_no_file_changed(
    monkeypatch, capsys, tmp_path
):
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    fake_report = ExportReport(
        deleted=[wall_gid],
        simplified=[],
        skipped=[],
        warnings=[],
        output_path=str(tmp_path / "src_light.ifc"),
        stage_seconds={},
    )
    monkeypatch.setattr(repl, "apply_operations", lambda *a, **kw: fake_report)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "", "y", "quit"])
    repl.run(str(src_path))

    out = capsys.readouterr().out
    assert "=== 完了 ===" in out  # 実際に書き込みが完了している(前提の確認)
    assert "ファイルは変更されていません" not in out


def test_quit_after_successful_text_mode_apply_does_not_claim_no_file_changed(
    monkeypatch, capsys, tmp_path
):
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    def _fake_rewrite_without(src, out, plan, graph, source_name, progress=None):
        return RewriteReport(
            records_in=10, records_dropped=3, rels_patched=1, rels_dropped=0, bytes_out=1234
        )

    monkeypatch.setattr(repl, "rewrite_without", _fake_rewrite_without)

    _feed_input(monkeypatch, ["delete IFCWALL", "apply", "y", "y", "", "quit"])
    repl.run(str(src_path), text=True)

    out = capsys.readouterr().out
    assert "=== 完了(テキストモード) ===" in out
    assert "ファイルは変更されていません" not in out


def test_quit_without_any_apply_still_claims_no_file_changed(monkeypatch, capsys):
    """回帰防止: 一度もapplyが成功していないセッションでは、従来通り
    「ファイルは変更されていません」が出ること(M5(ii)が既定挙動を壊していない
    ことの確認)。"""
    scan = _fake_scan([])
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)
    _feed_input(monkeypatch, ["quit"])

    repl.run("dummy.ifc")

    out = capsys.readouterr().out
    assert "中断しました。ファイルは変更されていません。" in out


def test_declined_apply_after_earlier_success_does_not_claim_no_file_changed_on_quit(
    monkeypatch, capsys, tmp_path
):
    """このセッション内で一度applyが成功した後、2回目のapplyを確認1で
    declineしても(その2回目自体はファイルを書いていない)、その後のquitは
    セッション全体として見て「ファイルは変更されていません」とは言わない
    (最初のapplyで書き込みが起きているため)。"""
    f = build_wall_with_window_ifc()
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    src_path = _write_ifc(f, tmp_path)

    scan = _fake_scan(
        stats=[_stats("IFCWALL", 1)], path=str(src_path), elements={"IFCWALL": [wall_gid]}
    )
    monkeypatch.setattr(repl, "scan_file", lambda path, **kw: scan)

    fake_report = ExportReport(
        deleted=[wall_gid],
        simplified=[],
        skipped=[],
        warnings=[],
        output_path=str(tmp_path / "src_light.ifc"),
        stage_seconds={},
    )
    monkeypatch.setattr(repl, "apply_operations", lambda *a, **kw: fake_report)

    _feed_input(
        monkeypatch,
        ["delete IFCWALL", "apply", "y", "", "y", "apply", "n", "quit"],
    )
    repl.run(str(src_path))

    out = capsys.readouterr().out
    assert "ファイルは変更されていません" not in out


# --- summarize_warnings(警告の要約表示) ---


def test_summarize_warnings_dedups_and_ranks():
    """同文の警告を畳み、件数降順に上位 top 種だけ表示し、残りは合計で示す。"""
    from ifc_occam.cui.repl import summarize_warnings

    lines = summarize_warnings(["a", "b", "a", "a", "b", "c"], top=2)
    assert lines == [
        "警告: 6件(3種)",
        "  - a ×3",
        "  - b ×2",
        "  … 他 1種 1件",
    ]


def test_summarize_warnings_single_and_empty():
    """空リストは空(見出しも出さない)、1件は件数サフィックスなし。"""
    from ifc_occam.cui.repl import summarize_warnings

    assert summarize_warnings([]) == []
    assert summarize_warnings(["x"]) == ["警告: 1件(1種)", "  - x"]


def test_summarize_warnings_ties_keep_first_seen_order():
    """同数の警告は初出順を保つ(表示が実行ごとに揺れない)。"""
    from ifc_occam.cui.repl import summarize_warnings

    lines = summarize_warnings(["b", "a", "b", "a"], top=5)
    assert lines == ["警告: 4件(2種)", "  - b ×2", "  - a ×2"]


def test_print_report_shows_summarized_warning_contents_not_just_the_count(capsys):
    """結線レベルの番人(フェーズ最終レビュー I-2): summarize_warnings の純粋
    関数テストだけでは、_print_report がそれを実際に使っていることを固定
    できない。同文警告×3+別文1(=2種4件)の report を _print_report に
    食わせ、画面出力に summarize_warnings の中身(種類の内訳・×N)が
    実際に出ることを確認する。"""
    report = ExportReport(
        deleted=[],
        simplified=[],
        skipped=[],
        warnings=["同文警告"] * 3 + ["別の警告"],
        output_path="out.ifc",
    )

    repl._print_report(report)

    out = capsys.readouterr().out
    assert "警告: 4件(2種)" in out
    assert "×3" in out


# --- 共有波及の確認2開示(docs/plans/2026-07-31-cui-shared-scope.md) ---


def test_shared_spillover_counts_siblings_outside_all_targets(tmp_path):
    """共有マップの片割れだけを対象にした shared simplify は、もう片方
    (どの操作の対象でもない)への波及としてクラス別に数えられる。"""
    import ifcopenshell

    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _shared_spillover_counts
    from tests.fixtures_ifc import build_two_consumers_mapped_child_styled_brep_ifc

    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    ops = [Operation(op="simplify", targets=[elem1.GlobalId], scope="shared",
                     params={"method": "bbox"})]

    assert _shared_spillover_counts(f, ops) == {"IfcBuildingElementProxy": 1}


def test_shared_spillover_excludes_deleted_and_targeted_siblings(tmp_path):
    """波及先が (a) 他の simplify の対象、(b) 削除対象、のどちらかなら開示に
    数えない(aは指定済み、bはどうせ消えるため情報にならない)。"""
    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _shared_spillover_counts
    from tests.fixtures_ifc import build_two_consumers_mapped_child_styled_brep_ifc

    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")

    both = [
        Operation(op="simplify", targets=[elem1.GlobalId], scope="shared",
                  params={"method": "bbox"}),
        Operation(op="simplify", targets=[elem2.GlobalId], scope="shared",
                  params={"method": "bbox"}),
    ]
    assert _shared_spillover_counts(f, both) == {}

    with_delete = [
        Operation(op="simplify", targets=[elem1.GlobalId], scope="shared",
                  params={"method": "bbox"}),
        Operation(op="delete", targets=[elem2.GlobalId]),
    ]
    assert _shared_spillover_counts(f, with_delete) == {}


def test_element_scope_reports_no_spillover(tmp_path):
    """個別化(scope=element)は波及しないので開示対象にならない。"""
    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _shared_spillover_counts
    from tests.fixtures_ifc import build_two_consumers_mapped_child_styled_brep_ifc

    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, _elem2 = f.by_type("IfcBuildingElementProxy")
    ops = [Operation(op="simplify", targets=[elem1.GlobalId], scope="element",
                     params={"method": "bbox"})]

    assert _shared_spillover_counts(f, ops) == {}


def test_confirm2_prints_spillover_disclosure(tmp_path, capsys, monkeypatch):
    """確認2の画面に波及の開示行が出る(結線レベルの番人)。"""
    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _preview_and_confirm2
    from tests.fixtures_ifc import build_two_consumers_mapped_child_styled_brep_ifc

    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, _elem2 = f.by_type("IfcBuildingElementProxy")
    ops = [Operation(op="simplify", targets=[elem1.GlobalId], scope="shared",
                     params={"method": "bbox"})]
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    assert _preview_and_confirm2(f, ops) is True
    out = capsys.readouterr().out
    assert "共有波及: 操作で指定していない 1要素 の形状も対象になります" in out
    assert "IfcBuildingElementProxy: 1" in out


def test_確認プレビューにOBB軽量化が出る(monkeypatch, capsys):
    """確認2プレビューのラベル(Task2)。_SIMPLIFY_PREVIEW_LABELS がobbを
    OBB軽量化に対応させ、実際の確認2表示にもそのラベルが出ることを固定する。"""
    assert repl._SIMPLIFY_PREVIEW_LABELS["obb"] == "OBB軽量化"

    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _preview_and_confirm2
    from tests.fixtures_ifc import build_two_consumers_mapped_child_styled_brep_ifc

    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, _elem2 = f.by_type("IfcBuildingElementProxy")
    ops = [Operation(op="simplify", targets=[elem1.GlobalId], scope="element",
                     params={"method": "obb"})]
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    assert _preview_and_confirm2(f, ops) is True
    out = capsys.readouterr().out
    assert "OBB軽量化 1件" in out


def test_shared_spillover_counts_extra_excluded_removes_cascade_deleted_sibling(tmp_path):
    """extra_excluded に渡した GlobalId は波及開示から除かれる
    (フェーズ最終レビューM-7、_shared_spillover_counts単体)。"""
    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _shared_spillover_counts
    from tests.fixtures_ifc import build_two_consumers_mapped_child_styled_brep_ifc

    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    ops = [Operation(op="simplify", targets=[elem1.GlobalId], scope="shared",
                     params={"method": "bbox"})]

    # extra_excluded を渡さない(従来どおり)場合はelem2が波及として数えられる。
    assert _shared_spillover_counts(f, ops) == {"IfcBuildingElementProxy": 1}
    # elem2 が(例えば削除連鎖で)どうせ消えるなら、extra_excluded に渡して除ける。
    assert _shared_spillover_counts(f, ops, extra_excluded={elem2.GlobalId}) == {}


def test_spillover_counts_include_directly_shared_sibling():
    """直接共有の兄弟が確認2の波及開示に乗ること(フェーズ最終レビューI-3)。
    従来は IfcMappedItem 経由の共有しか集計されず、「開示行が出ない=波及
    が一切ない」とは限らなかった(testing-guide の旧注意点2)。"""
    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _shared_spillover_counts
    from tests.fixtures_ifc import build_two_elements_sharing_representation_directly_ifc

    f = build_two_elements_sharing_representation_directly_ifc()
    elem1, _elem2 = f.by_type("IfcBuildingElementProxy")
    ops = [
        Operation(
            op="simplify",
            targets=[elem1.GlobalId],
            scope="shared",
            params={"method": "bbox"},
        )
    ]
    spillover = _shared_spillover_counts(f, ops)
    assert spillover == {"IfcBuildingElementProxy": 1}


def test_confirm2_excludes_cascade_deleted_sibling_from_spillover_disclosure(
    tmp_path, capsys, monkeypatch
):
    """兄弟要素が simplify/delete どちらの直接対象でもなくても、delete連鎖
    (集約の子部材)でどうせ削除される場合は確認2の開示に出ない
    (フェーズ最終レビューM-7、_preview_and_confirm2への結線)。

    修正前は _shared_spillover_counts に delete closure が渡っていないため、
    連鎖で消えるだけの兄弟(elem2)も「操作で指定していない要素」として
    誤って開示されてしまう(過剰開示)。
    """
    import ifcopenshell.api

    from ifc_occam.core.ops import Operation
    from ifc_occam.cui.repl import _preview_and_confirm2
    from tests.fixtures_ifc import build_two_consumers_mapped_child_styled_brep_ifc

    f = build_two_consumers_mapped_child_styled_brep_ifc()
    elem1, elem2 = f.by_type("IfcBuildingElementProxy")
    assembly = ifcopenshell.api.run(
        "root.create_entity", f, ifc_class="IfcElementAssembly", name="Assembly1"
    )
    ifcopenshell.api.run(
        "aggregate.assign_object", f, products=[elem2], relating_object=assembly
    )

    ops = [
        Operation(op="delete", targets=[assembly.GlobalId]),
        Operation(op="simplify", targets=[elem1.GlobalId], scope="shared",
                  params={"method": "bbox"}),
    ]
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    assert _preview_and_confirm2(f, ops) is True
    out = capsys.readouterr().out
    assert "共有波及" not in out


def test_help_text_documents_element_shared_scope_and_shared_default():
    """helpテキストの [element|shared] とscope既定の注記に番人テストを付ける
    (フェーズ最終レビューM-2)。既定が破壊的方向(共有波及)に変わった以上、
    element オプトアウトの存在を知る唯一の手段であるこの文言を固定する。"""
    assert "[element|shared]" in repl._HELP_TEXT
    assert "※ 簡略化は既定で共有波及" in repl._HELP_TEXT


def test_help_text_documents_obb_command():
    """helpテキストのbbox行の直後にobbコマンドの日本語ラベル併記行が
    追加されている(Task2、CF-A最終レビューM-1と同じ表記形式)。"""
    assert "obb <クラス名> [element|shared]" in repl._HELP_TEXT
    assert "OBB軽量化(obb)" in repl._HELP_TEXT


def test_format_spillover_line_truncates_to_top_five_with_rest_count():
    """6クラス以上の波及は上位5クラスまで表示し「...他Nクラス」を付ける
    (フェーズ最終レビューM-3、純粋関数の番人テスト)。"""
    from ifc_occam.cui.repl import _format_spillover_line

    spillover = {
        "IfcWall": 10,
        "IfcSlab": 9,
        "IfcBeam": 8,
        "IfcColumn": 7,
        "IfcDoor": 6,
        "IfcWindow": 5,
    }
    line = _format_spillover_line(spillover)
    assert line == (
        "共有波及: 操作で指定していない 45要素 の形状も対象になります"
        "(IfcWall: 10, IfcSlab: 9, IfcBeam: 8, IfcColumn: 7, IfcDoor: 6, ...他1クラス)。"
    )
