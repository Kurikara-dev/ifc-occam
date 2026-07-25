"""cui/repl.py(対話ループ・薄いI/O殻)のTDD (cui-design.md §6、task-6-brief.md)。

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

import shutil
from pathlib import Path

import ifcopenshell
import pytest

from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.export import ExportReport
from ifc_occam.core.extract import extract_model
from ifc_occam.cui import repl
from ifc_occam.scan.aggregate import ClassScanStats, ScanResult, scan_file
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
