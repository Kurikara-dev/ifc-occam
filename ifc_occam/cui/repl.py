"""cui/repl.py — 対話ループ(薄いI/O殻)(cui-design.md §6)。

コマンド解釈ロジックは一切持たない(それは `CuiSession` の責務)。ここでは:
  - Phase A: `scan_file` で軽量スキャンし、ランキングを表示する
    (`--scan-only` はここで終了する)。
  - Phase B: `input()` ループ。`quit`/`h`・`help`/`apply` だけ repl 自身が
    横取りし、それ以外は `CuiSession.command()` の戻り値をそのまま表示する。
  - Phase C(`apply`): repl が確認フローを組み立てる(確認2回+出力ファイル名プロンプト)。
    1. 操作サマリ(`render_intents`)+ `est_fullopen_bytes` 警告判定 → 確認1。
    1b. 出力ファイル名プロンプト(確認1が通った後・フルオープンの前。空Enter・
        入力ありのどちらも `_resolve_cui_output_path` で解決する(基準統一。
        レビューア指摘2026-07-25で改訂)。CLI --output 指定時は表示しない。
        要件§5モック準拠・監督者裁定2026-07-25追記、design.md §6 1b)。
    2. フルオープン(経過秒を表示)→ `extract_elements_light` で実ファイルの
       GlobalId集合と突合(スキャン時との差異があれば件数を表示)→ delete群は
       `compute_delete_closure` で連鎖を展開し「直接N件+連鎖M件」等を表示 → 確認2。
    3. `apply_operations` に確認2で開いた `ifcopenshell.file` オブジェクトを
       直接渡す(design.md §5-1: 再オープンしない。CUIはフルオープンを1回に
       抑えるため。渡した file オブジェクトは以後使い捨てにする)。進捗は
       間引いて表示する(下記)。あわせて `source_name=<入力ファイル名>` を渡す
       (CUI Phase2 Task1: 出力ヘッダの由来刻印に使われる。file オブジェクトを
       渡すため apply_operations 側の既定導出 `"(in-memory)"` に頼ると元ファイル名
       が失われる)。

Ctrl+C(KeyboardInterrupt)・EOF(標準入力終端)は、メインループの `input()` でも
apply確認中の `input()` でも同じ1箇所の except で捕まえ、安全に終了する
(呼び出し階層に関わらずPython例外は最も近い外側のtry/exceptまで伝播するため、
apply確認をメインループと同じtryブロックの中で呼ぶだけで両方カバーできる)。

進捗表示の間引き(監督者確定要件3): `apply_operations` の `progress` コールバックは
要素ごとに発火する契約(design.md §5-2)のため、素朴に1件1行で表示すると数十万行に
膨れる。`_make_progress_printer` はステージの最初の1件・最後の1件・
`_PROGRESS_STRIDE` 件ごとだけを `\\r` で同じ行に上書き表示し、ステージ完了時
(最終件)だけ改行して確定行を残す。新規依存(tqdm等)は使わない。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

import ifcopenshell

from ifc_occam.core.cascade import compute_delete_closure
from ifc_occam.core.export import ExportReport, apply_operations
from ifc_occam.core.extract import extract_elements_light
from ifc_occam.cui.session import CuiSession
from ifc_occam.scan.aggregate import FULLOPEN_BYTES_MULTIPLIER, ScanResult, scan_file

__all__ = ["run"]

#: cui-design.md §0の実測目安「32GB RAM機で~2GBファイル」を安全側の閾値として
#: 使う(推定フルオープンメモリ = ファイルサイズ × FULLOPEN_BYTES_MULTIPLIER)。
#: 実際のRAM検出には新規依存(psutil等)が必要になるため採用しない(監督者要件:
#: 新規依存禁止)。
#:
#: Task 8実測を受けて本値(ファイルサイズ側の閾値)は**現状維持(2GiB)**と
#: 判断した(docs/cui-measurements.md「Task 8」章に実測根拠)。理由:
#: (1) 本値と FULLOPEN_BYTES_MULTIPLIER は下の `_FULLOPEN_WARN_BYTES =
#: _FULLOPEN_WARN_FILE_BYTES * FULLOPEN_BYTES_MULTIPLIER` の比較式で両辺から
#: 約分されるため、警告が発火する「ファイルサイズ」の閾値そのものは
#: FULLOPEN_BYTES_MULTIPLIER をいくつに校正しても変わらない(表示される
#: 推定バイト数の精度だけが変わる)。(2) 実測データは2GiB未満(mini
#: 292MB/small 1.2GB、いずれも警告非発火で妥当)と6.5GB(警告発火、妥当)の
#: 2点のみで、2GiB境界そのものを跨ぐ実測点がない。small(1.2GB)のフル
#: オープン単体ピークは実測14.75GB(32GB搭載機の約46%)で「危険域」とまでは
#: 言えず、現行の2GiB閾値を棄却する根拠はない。実測1.2GB〜6.5GBの間隙
#: (境界付近の実測)は未取得のため、この結論は暫定であり追加実測が望ましい。
_FULLOPEN_WARN_FILE_BYTES = 2 * 1024**3
_FULLOPEN_WARN_BYTES = _FULLOPEN_WARN_FILE_BYTES * FULLOPEN_BYTES_MULTIPLIER

#: 進捗表示の間引き間隔(監督者確定要件3)。
_PROGRESS_STRIDE = 500

_STAGE_LABELS = {"delete": "削除中", "simplify": "簡略化中"}

#: ExportReport.stage_seconds のキー(export.py 内部の英語識別子)→ 結果表示用の
#: 日本語ラベル。keyはexport.apply_operationsが実際に設定する6種で固定
#: (open/deletes/simplify/reextract_duplicates/consolidate/write)。
_STAGE_SECONDS_LABELS = {
    "open": "開く",
    "deletes": "削除",
    "simplify": "簡略化",
    "reextract_duplicates": "重複再抽出",
    "consolidate": "重複統合",
    "write": "書き込み",
}

#: Operation(op="simplify").params["method"] → 確認2プレビュー表示用ラベル。
#: session.py の _SET_OP_LABELS/_op_label と同じ語彙(bbox軽量化/凸包化/間引き)に揃える。
_SIMPLIFY_PREVIEW_LABELS = {"bbox": "bbox軽量化", "convex_hull": "凸包化", "decimate": "間引き"}

_INTRO_HINT = "操作を入力してください (h でヘルプ):"

_HELP_TEXT = """\
=== コマンド一覧 ===
  delete <クラス名>             クラス全要素を削除対象に追加する
  bbox <クラス名>               クラス全要素をbbox軽量化対象に追加する
  hull <クラス名>               クラス全要素を凸包化対象に追加する
  decimate <クラス名> <ratio>   クラス全要素を間引き対象に追加する(ratio: 0.05-0.95)
  keep <クラス名>               操作指定を解除し、保持対象として明示する
  undo [番号]                  操作リストから1件取り消す(番号省略時は list の最終行を取り消し)
  list                         現在の操作リストを表示する
  rank                         診断ランキングを再表示する
  apply                        操作リストをIFCファイルに適用して出力する(確認2回)
  quit                         対話を中断して終了する(ファイルは変更されない)
  h / help                     このヘルプを表示する"""


def run(path: str, *, output: str | None = None, scan_only: bool = False) -> None:
    """CUI対話ループのエントリポイント(cui-design.md §6)。

    Phase A(軽量スキャン)→ Phase B(対話。`scan_only=True` ならここで終了)→
    Phase C(`apply` コマンドで開始)の3段を束ねる。
    """
    sys.stdout.reconfigure(encoding="utf-8")

    print("IFC Occam CUI — 軽量スキャン中...")
    scan = scan_file(path)
    session = CuiSession(scan)
    print(session.render_ranking())

    if scan_only:
        return

    print()
    print(_INTRO_HINT)

    while True:
        try:
            line = input("> ").strip()
            if not line:
                continue
            verb = line.split()[0].lower()

            if verb == "quit":
                print("中断しました。ファイルは変更されていません。")
                return
            if verb in ("h", "help"):
                print(_HELP_TEXT)
                continue
            if verb == "apply":
                _run_apply(scan, session, path, output)
                continue

            print(session.command(line))
        except (EOFError, KeyboardInterrupt):
            print()
            print("中断しました。ファイルは変更されていません。")
            return


def _confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def _resolve_cui_output_path(path: str, output: str | None) -> str:
    """出力先を解決する。相対パスは常に入力ファイルと同じディレクトリを基準にする
    (export.resolve_output_path と同じ規約)。

    CUIは確認2で開いた ifcopenshell.file オブジェクトを apply_operations に渡す
    (design.md §5-1)ため、そちら側の「file オブジェクトのときは相対パスをcwd基準で
    解決する」フォールバックには乗れない。repl側であらかじめ絶対パスまで解決しておく。
    """
    src = Path(path).resolve()
    candidate = Path(output) if output is not None else Path(f"{src.stem}_light{src.suffix}")
    if candidate.is_absolute():
        return str(candidate)
    return str(src.parent / candidate)


def _run_apply(scan: ScanResult, session: CuiSession, path: str, output: str | None) -> None:
    """`apply` コマンドの一連処理(cui-design.md §6 手順1-3、1b)。"""
    operations = session.to_operations()
    if not operations:
        print("操作が指定されていません(list で確認できます)。")
        return

    output_path = _resolve_cui_output_path(path, output)

    # 手順1: 操作サマリ + est_fullopen_bytes 警告判定 → 確認1。
    print(session.render_intents())
    print(f"出力先: {output_path}")
    if scan.est_fullopen_bytes > _FULLOPEN_WARN_BYTES:
        print(
            f"警告: 推定フルオープンメモリが大きく({scan.est_fullopen_bytes:,} bytes)、"
            "適用に時間がかかるか失敗する可能性があります。"
        )
    if not _confirm("この内容で適用を開始しますか? (y/N): "):
        print("中断しました。ファイルは変更されていません。")
        return

    # 手順1b: 出力ファイル名プロンプト(監督者裁定2026-07-25、design.md §6 1b、
    # 要件§5モック準拠)。CLI --output 指定時は出力先が確定済みのため出さない
    # (非対話経路は無変更)。空Enter・入力ありのどちらも _resolve_cui_output_path
    # を通し、既定値/--output と同じ基準(入力ファイルと同じディレクトリ)に解決する
    # (レビューア指摘2026-07-25で改訂: 入力値だけがcwd基準になっていた不整合を解消。
    # 既存ヘルパーを再利用し、重複実装はしない)。
    if output is None:
        typed = input(f"出力ファイル名 [{Path(output_path).name}]: ").strip()
        if typed:
            output_path = _resolve_cui_output_path(path, typed)

    # 手順2: フルオープン → 実ファイルとの突合 → delete閉包の展開 → 確認2。
    print("フルオープン中...", flush=True)
    t0 = time.monotonic()
    ifc_file = ifcopenshell.open(str(path))
    print(f"フルオープン完了: {time.monotonic() - t0:.1f}秒")

    if not _preview_and_confirm2(ifc_file, operations):
        print("中断しました。ファイルは変更されていません。")
        return

    # 手順3: 適用(進捗は間引いて表示)→ 結果表示。source_nameは入力ファイル名を
    # 明示的に渡す(CUI Phase2 Task1: apply_operationsにはfileオブジェクトを渡す
    # ため、既定導出"(in-memory)"では出力ヘッダの由来刻印から元ファイル名が
    # 失われてしまう)。
    report = apply_operations(
        ifc_file,
        operations,
        output_path,
        progress=_make_progress_printer(),
        source_name=Path(path).name,
    )
    _print_report(report)


def _preview_and_confirm2(ifc_file, operations) -> bool:
    """フルオープン後の実ファイル突合・削除連鎖プレビュー・確認2(手順2)。
    確認2の答えが y/yes なら True。"""
    real_gids = {gid for gid, _cls, _name in extract_elements_light(ifc_file)}

    delete_targets: set[str] = set()
    simplify_counts: dict[str, int] = {}
    missing = 0
    for op in operations:
        # `valid`はop.targetsからdrift分(スキャン時には存在したが実ファイルには
        # 無いGlobalId)を除いた集合だが、**このメソッド内のプレビュー表示専用**
        # (直後のdelete_targets/simplify_countsという集計・件数表示にしか使わない。
        # op.targets自体は書き換えない)。手順3で実際にapply_operationsへ渡るのは
        # _run_apply側が保持しているフィルタ前の生operations(このメソッドは
        # 確認2のy/n判定boolしか返さない)であり、`valid`がそちらへ伝播することは
        # ない。drift分の実除外はexport.py側の独立したスキップ処理
        # (_apply_deletesのby_guid RuntimeError捕捉・apply_operationsのsimplify
        # ループの同様の捕捉)で別途行われ、結果としてここでの除外表示と一致する
        # (このプレビュー計算が実適用に使われているからではない)。将来の読み手が
        # 「valid = 実際にapply_operationsへ渡る適用対象集合」と誤解しないこと
        # (Phase1 final review Finding2、コメントのみ・挙動変更なし)。
        valid = [gid for gid in op.targets if gid in real_gids]
        missing += len(op.targets) - len(valid)
        if op.op == "delete":
            delete_targets.update(valid)
        elif op.op == "simplify":
            method = op.params.get("method")
            label = _SIMPLIFY_PREVIEW_LABELS.get(method, str(method))
            simplify_counts[label] = simplify_counts.get(label, 0) + len(valid)
        # keep は変化を起こさないためプレビューに表示しない。

    if missing:
        print(f"スキャン時と異なる要素を{missing}件検出しました(対象から除外します)。")

    summary: list[str] = []
    if delete_targets:
        closure = compute_delete_closure(ifc_file, sorted(delete_targets))
        summary.append(f"削除 直接{len(closure.direct)}件+連鎖{len(closure.cascaded)}件")
    for label, count in simplify_counts.items():
        summary.append(f"{label} {count}件")

    print("=== 適用内容(実ファイルで確認済み) ===")
    print(" / ".join(summary) if summary else "(適用対象なし)")

    return _confirm("実行しますか? (y/N): ")


def _print_report(report: ExportReport) -> None:
    print("=== 完了 ===")
    print(f"出力ファイル: {report.output_path}")
    print(f"削除: {len(report.deleted)}要素")
    print(f"簡略化: {len(report.simplified)}要素")
    if report.skipped:
        print(f"スキップ: {len(report.skipped)}件")
    if report.warnings:
        print(f"警告: {len(report.warnings)}件")
    for stage_name, seconds in report.stage_seconds.items():
        if seconds <= 0:
            # consolidate=False(CUIは常にこれ)のとき、export.pyは
            # reextract_duplicates/consolidateを実行せず明示的に0.0を入れる
            # (実行して0.0秒だったわけではない)。無意味な行なので省く。
            continue
        label = _STAGE_SECONDS_LABELS.get(stage_name, stage_name)
        print(f"  {label}: {seconds:.1f}秒")
    try:
        size = Path(report.output_path).stat().st_size
    except OSError:
        size = None
    if size is not None:
        print(f"出力サイズ: {size:,} bytes")


def _make_progress_printer() -> Callable[[str, int, int], None]:
    """`apply_operations` の `progress` コールバックを作る(監督者確定要件3)。

    ステージの最初の1件・最後の1件・`_PROGRESS_STRIDE` 件ごとだけ `\\r` で同じ行を
    上書きし、ステージ完了(最終件)時だけ改行して確定行を残す
    (次のステージは新しい行から始まる)。tqdm等の新規依存は使わない。
    """

    def _progress(stage: str, done: int, total: int) -> None:
        if not (done == 1 or done == total or done % _PROGRESS_STRIDE == 0):
            return
        label = _STAGE_LABELS.get(stage, stage)
        end = "\n" if done == total else ""
        print(f"\r{label}: {done}/{total}件", end=end, flush=True)

    return _progress
