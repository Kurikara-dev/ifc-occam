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

    CUI Phase3 Task5(docs/plans/2026-07-25-cui-phase3.md Task5): 手順1(操作サマリ+警告判定)の後・
    確認1の直前に、「intents が delete のみ」(全件 op=="delete"。keep/bbox/
    hull/decimateが1件でも混在すれば対象外——keepはフルオープン経路の閉包計算に
    効く明示指定であり、テキスト経路には相当機能が無いため黙って無視しては
    ならない)かつ(`--text` フラグ指定 or `est_fullopen_bytes` 警告該当)の場合に
    限り、テキストモード(フルオープン不要)を提案する1問を割り込ませる
    (`_run_apply` 内)。y ならテキスト経路(`_run_text_apply`: `scan_full_graph` →
    `compute_text_delete_plan` → 確認2相当(stats表示) → 出力ファイル名プロンプト
    (1bと同一規約) → `rewrite_without`)に入り、確認1は出さない(テキスト経路が
    自前の確認を持つため、確認2回という設計不変条件は保たれる)。n、または
    この条件に当たらない場合は手順1b以降(フルオープン経路)へそのまま進む
    (挙動を1バイトも変えない)。`--text` 指定かつ delete 以外の intent があれば、
    この1問自体を出さずに「テキストモードは削除のみ対応」と表示してフル
    オープン経路へフォールバックする。テキスト経路は `ifcopenshell.open` を
    一度も呼ばない(このフェーズの存在意義)。指定クラスが参照グラフ上に
    1件も見つからない(`seeds==0`)場合は出力を書かずにフルオープン経路へ
    フォールバックする(黙って書くと「削除したつもりで中身が元と同じ
    ファイル」という最も危険な失敗モードになるため)。

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
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom

from ifc_occam.core.advisor import advise_simplify, metrics_from_shapes
from ifc_occam.core.cascade import compute_delete_closure
# 共有マップの識別はexport.pyのsimplifyループと同じ関数で行う(定義の二重化でズレるより私的importの方がまし)。
from ifc_occam.core.export import ExportReport, _shared_map_key, apply_operations
# _analyze_representation/_shape_from_geometry は extract.py 内部ヘルパだが、
# 確認2のサンプル実測がその変換ロジックを二重化しないよう再利用する(要件2)。
from ifc_occam.core.extract import (
    _analyze_representation,
    _shape_from_geometry,
    extract_elements_light,
)
from ifc_occam.core.paths import refers_to_same_file
from ifc_occam.core.simplify import get_shared_element_gids
from ifc_occam.cui.session import CuiSession, Intent
from ifc_occam.scan.aggregate import FULLOPEN_BYTES_MULTIPLIER, ScanResult, scan_file
from ifc_occam.scan.fullgraph import scan_full_graph
from ifc_occam.textops.plan import compute_text_delete_plan
from ifc_occam.textops.rewrite import RewriteReport, rewrite_without

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

_STAGE_LABELS = {"delete": "削除中", "simplify": "簡略化中", "rewrite": "書き換え中"}

#: ExportReport.stage_seconds のキー(export.py 内部の英語識別子)→ 結果表示用の
#: 日本語ラベル。keyはexport.apply_operationsが実際に設定する7種で固定
#: (open/deletes/simplify/reextract_duplicates/consolidate/write/gc。
#: フェーズ最終レビューI3: "gc" ステージ追加時にここへの追記が漏れ、
#: 日本語表示の中に英語キーがそのまま混ざっていた)。tests/test_repl.py の
#: 番人テストが、この辞書が apply_operations の実際のキー集合を網羅している
#: ことを固定する。
_STAGE_SECONDS_LABELS = {
    "open": "開く",
    "deletes": "削除",
    "simplify": "簡略化",
    "reextract_duplicates": "重複再抽出",
    "consolidate": "重複統合",
    "write": "書き込み",
    "gc": "ゴミ回収",
}

#: Operation(op="simplify").params["method"] → 確認2プレビュー表示用ラベル。
#: session.py の _SET_OP_LABELS/_op_label と同じ語彙(bbox軽量化/凸包化/間引き)に揃える。
_SIMPLIFY_PREVIEW_LABELS = {
    "bbox": "bbox軽量化", "convex_hull": "凸包化", "decimate": "間引き", "obb": "OBB軽量化",
}

#: 確認2の適正判定サンプル実測(_confirm2_advisories)1グループあたりの実測件数上限。
#: 実測79ms/形状(create_shape)なので20件×グループ数でも数秒に収まる
#: (Task4-CUI要件2、閾値ガード不要と裁定済み)。
_ADVISOR_SAMPLE_SIZE = 20

_INTRO_HINT = "操作を入力してください (h でヘルプ):"

# bbox/hull/decimate の各行は「日本語ラベル(内部method名)」の形で併記する
# (carry-forward Phase M「操作表記の統一」、CF-A最終レビューM-1)。
# _method_desc(export.py)が先勝ち警告に出す表記(例:「凸包化(convex_hull)」)
# と同じ形にすることで、コマンド名(bbox/hull/decimate)↔警告文中の英語表記
# (bbox/convex_hull/decimate)↔日本語ラベル(bbox軽量化/凸包化/間引き)の
# 対応をhelpだけで読み取れるようにする(旧文面はコマンド名とhull以外の
# method名が一致するため気付きにくいが、hullだけmethod名がconvex_hullに
# 変わり対応が読み取れなかった)。
_HELP_TEXT = """\
=== コマンド一覧 ===
  delete <クラス名>             クラス全要素を削除対象に追加する
  bbox <クラス名> [element|shared]     クラス全要素をbbox軽量化(bbox)対象に追加する(既定: 共有波及)
  obb <クラス名> [element|shared]      クラス全要素をOBB軽量化(obb)対象に追加する(既定: 共有波及。部材の向きに沿って回転した直方体に置き換える(bboxの向き付き版))
  hull <クラス名> [element|shared]     クラス全要素を凸包化(convex_hull)対象に追加する(既定: 共有波及)
  decimate <クラス名> <ratio> [element|shared]   クラス全要素を間引き(decimate)対象に追加する(ratio: 0.05-0.95、既定: 共有波及)
  keep <クラス名>               操作指定を解除し、保持対象として明示する
  undo [番号]                  操作リストから1件取り消す(番号省略時は list の最終行を取り消し)
  list                         現在の操作リストを表示する
  rank                         診断ランキングを再表示する
  apply                        操作リストをIFCファイルに適用して出力する(確認2回)
  quit                         対話を中断して終了する(ファイルは変更されない)
  ※ 簡略化は既定で共有波及(同じ共有形状を使う要素にまとめて効く)。末尾に element で従来の個別化。
  h / help                     このヘルプを表示する"""


def run(
    path: str, *, output: str | None = None, scan_only: bool = False, text: bool = False,
    inline_cleanup: bool = False,
) -> None:
    """CUI対話ループのエントリポイント(cui-design.md §6)。

    Phase A(軽量スキャン)→ Phase B(対話。`scan_only=True` ならここで終了)→
    Phase C(`apply` コマンドで開始)の3段を束ねる。

    `text`(CUI Phase3 Task5、CLI `--text`)は apply 確認フローに割り込む
    テキストモード提案の発動条件の一部として `_run_apply` にそのまま渡す
    (モジュールdocstring参照)。

    `inline_cleanup`(carry-forward Phase E、CLI `--inline-cleanup`)はフル
    オープン経路の書き出し方式(`apply_operations` の `geometry_cleanup`)を
    "inline"(逐次ゴミ回収、省メモリ)に切り替えるフラグで、`_run_apply` に
    そのまま渡す。テキストモード経路は `apply_operations` を通らないため
    効果を持たない。
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

    # M5(ii)(フェーズ最終レビュー): このセッション中に apply が実際に出力
    # ファイルへの書き込みを完了したかを追跡する。quit/Ctrl+C/EOF の
    # 「ファイルは変更されていません」は書き込みが一度も起きていない場合
    # のみ正しい主張であり、無条件に出すと事実に反する(C1のシナリオでは
    # 積極的な誤誘導になる)。
    file_written = False

    while True:
        try:
            line = input("> ").strip()
            if not line:
                continue
            verb = line.split()[0].lower()

            if verb == "quit":
                print(_exit_message(file_written))
                return
            if verb in ("h", "help"):
                print(_HELP_TEXT)
                continue
            if verb == "apply":
                if _run_apply(
                    scan, session, path, output, text=text,
                    inline_cleanup=inline_cleanup, already_written=file_written,
                ):
                    file_written = True
                continue

            print(session.command(line))
        except (EOFError, KeyboardInterrupt):
            print()
            print(_exit_message(file_written))
            return


def _confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def _exit_message(file_written: bool) -> str:
    """quit/Ctrl+C/EOF で表示する中断メッセージ(M5(ii)、フェーズ最終レビュー)。

    file_written は「このセッション中に apply が実際に出力ファイルへの書き込み
    を完了したか」(run() が追跡する)。書き込みが一度も起きていなければ従来
    通り「ファイルは変更されていません」(正しい主張)。一度でも書き込みが
    起きていればこの文言は事実に反するため出さず、代わりに出力ファイルへの
    書き込みが完了している旨と、原本(入力ファイル)自体は変更されていない
    旨を伝える(この保証は C1 のガード導入後も変わらず成立する——apply は
    常に出力先が入力と異なることを確認した上でのみ書き込む)。
    """
    if file_written:
        return (
            "中断しました。(直前の適用で出力ファイルへの書き込みが完了しています。"
            "原本は変更されていません。)"
        )
    return "中断しました。ファイルは変更されていません。"


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


def _output_path_collides_with_source(path: str, output_path: str) -> bool:
    """output_path(解決済み。プロンプト入力値・`--output` のどちらも通過後)が
    入力ファイル(path)と同一実体を指しているかを判定する(C1、フェーズ最終
    レビュー: 出力先=入力先だと `rewrite_without`/フルオープン経路のいずれも
    原本を破壊しうる——このツールの契約は原本非破壊であり、フルオープン
    経路は truncate しないだけで原本上書きは同様に契約違反)。

    判定の実体は `core/paths.refers_to_same_file`(唯一の判定器。ライブラリ層の
    `core/export.apply_operations` と `textops/rewrite.rewrite_without` も同じ
    関数を使う)。ライブラリ層のガードは書き込みを止めるが例外の形でしか伝え
    られないため、UI層でも先に判定して分かる日本語メッセージで中断する。

    呼び出し元は `_maybe_prompt_output_filename` で出力ファイル名を確定させた
    直後、実際の書き込み(フルオープン/`rewrite_without`)を始める前に呼ぶこと
    (プロンプト入力値・`--output` 指定値のどちらでも output_path は同じ変数を
    経由するため、呼び出し箇所を1つに絞れば両方を自動的に塞げる)。
    """
    return refers_to_same_file(path, output_path)


def _print_output_collision_error(output_path: str) -> None:
    """C1 の同一パス衝突を検出したときの表示(3箇所の呼び出しで文言を共有する)。"""
    print(
        f"エラー: 出力先({output_path})が入力ファイルと同一です。"
        "原本を保護するため中断しました。別のファイル名を指定してください。"
    )


def _maybe_prompt_output_filename(path: str, output: str | None, output_path: str) -> str:
    """出力ファイル名プロンプト(手順1b・テキスト経路手順(iv)の両方で使う共通処理。
    CUI Phase3 Task5 監督者裁定4: 「1bと完全に同一規約」を1箇所にまとめ、
    重複実装を避ける)。

    `output`(CLIの--output)が None のときだけ表示する(CLI --output 指定時は
    出力先が確定済みのため出さない。非対話経路は無変更)。空Enterは
    `output_path` をそのまま使い、入力があれば `_resolve_cui_output_path` で
    解決する(既定値/--outputと同じ基準=入力ファイルと同じディレクトリ)。
    """
    if output is not None:
        return output_path
    typed = input(f"出力ファイル名 [{Path(output_path).name}]: ").strip()
    if typed:
        return _resolve_cui_output_path(path, typed)
    return output_path


def _run_apply(
    scan: ScanResult,
    session: CuiSession,
    path: str,
    output: str | None,
    *,
    text: bool = False,
    inline_cleanup: bool = False,
    already_written: bool = False,
) -> bool:
    """`apply` コマンドの一連処理(cui-design.md §6 手順1-3、1b、
    CUI Phase3 Task5のテキストモード分岐込み)。

    already_written: このセッション中に(過去の `apply` 呼び出しで)既に
    出力ファイルへの書き込みが完了しているか(M5(ii)、フェーズ最終レビュー)。
    この呼び出し自身が確認1/確認2で中断された場合の「中断しました。
    ファイルは変更されていません。」がセッション全体として見て事実に反する
    ことがないよう、`_exit_message` と同じ判定に使う(このapply呼び出し単体
    としては書き込みが起きていなくても、以前のapplyで既に書き込みが起きて
    いれば「変更されていません」とは言わない)。

    戻り値: このapply呼び出しで実際に出力ファイルへの書き込みが完了したか
    (呼び出し元の `run()` がこれを集計して次の呼び出しに `already_written`
    として渡し、quit/Ctrl+C/EOF時のメッセージがファイル書き込みの有無を
    正しく反映するようにする)。操作リストが空・各確認でno・C1の同一パス
    ガードで中断、のいずれも**この呼び出し自身は**書き込みが起きていない
    ため False(ただし `already_written=True` で呼ばれていた場合、そのTrueは
    呼び出し元がそのまま保持し続ける——このFalseは「今回は書いていない」の
    意味でしかない)。テキスト経路(`_run_text_apply`)・フルオープン経路の
    どちらで書き込みが完了しても True。
    """
    operations = session.to_operations()
    if not operations:
        print("操作が指定されていません(list で確認できます)。")
        return False

    output_path = _resolve_cui_output_path(path, output)

    # N2(フェーズ最終レビューの再審): `--output` 指定時は出力先がここで確定
    # しているので、衝突は**この時点で**弾く。テキスト経路の遅い判定
    # (参照グラフスキャン+確認2の後)まで待つと、このツールの本来の対象である
    # 多GBファイルでは数十分のスキャンを無駄にした上、実行不可能な操作に
    # ユーザーが確認を答えることになる。プロンプト入力値は確認の後でしか
    # 確定しないため、後段の判定も残す二段構えにする。
    if output is not None and _output_path_collides_with_source(path, output_path):
        _print_output_collision_error(output_path)
        return False

    # 手順1: 操作サマリ + est_fullopen_bytes 警告判定 → 確認1。
    print(session.render_intents())
    print(f"出力先: {output_path}")
    if scan.est_fullopen_bytes > _FULLOPEN_WARN_BYTES:
        print(
            f"警告: 推定フルオープンメモリが大きく({scan.est_fullopen_bytes:,} bytes)、"
            "適用に時間がかかるか失敗する可能性があります。"
        )

    # CUI Phase3 Task5: 確認1の直前にテキストモード分岐を割り込ませる
    # (docs/plans/2026-07-25-cui-phase3.md Task5、監督者裁定1-5、モジュール
    # docstring参照)。「delete のみ」= intents全件がop=="delete"
    # (keep/bbox/hull/decimateが1件でもあれば対象外)。
    intents = session.intents()
    delete_only = bool(intents) and all(i.op == "delete" for i in intents)

    if text and not delete_only:
        print(
            "テキストモードは削除のみ対応です(bbox/hull/decimate/keepを含む操作には"
            "使えません)。従来のフルオープン経路にフォールバックします。"
        )
    elif delete_only and (text or scan.est_fullopen_bytes > _FULLOPEN_WARN_BYTES):
        if _confirm("テキストモードで適用しますか?(フルオープン不要・削除のみ) [y/N]: "):
            handled, wrote = _run_text_apply(
                path, output, output_path, intents, already_written=already_written
            )
            if handled:
                return wrote
            # 監督者裁定5(seeds==0)によるフォールバックのみここに戻る。
            # そのまま従来の確認1へ進む。

    if not _confirm("この内容で適用を開始しますか? (y/N): "):
        print(_exit_message(already_written))
        return False

    # 手順1b: 出力ファイル名プロンプト(監督者裁定2026-07-25、design.md §6 1b、
    # 要件§5モック準拠)。CLI --output 指定時は出力先が確定済みのため出さない
    # (非対話経路は無変更)。空Enter・入力ありのどちらも _resolve_cui_output_path
    # を通し、既定値/--output と同じ基準(入力ファイルと同じディレクトリ)に解決する
    # (レビューア指摘2026-07-25で改訂: 入力値だけがcwd基準になっていた不整合を解消。
    # 既存ヘルパーを再利用し、重複実装はしない)。
    output_path = _maybe_prompt_output_filename(path, output, output_path)

    # C1(Critical、フェーズ最終レビュー): 出力先=入力先だと、フルオープン
    # 経路でも原本を上書きしてしまう(truncateしないだけで契約違反は同じ)。
    # フルオープンを始める前(=適用を開始する前)にここで弾く。
    if _output_path_collides_with_source(path, output_path):
        _print_output_collision_error(output_path)
        return False

    # 手順2: フルオープン → 実ファイルとの突合 → delete閉包の展開 → 確認2。
    print("フルオープン中...", flush=True)
    t0 = time.monotonic()
    ifc_file = ifcopenshell.open(str(path))
    print(f"フルオープン完了: {time.monotonic() - t0:.1f}秒")

    if not _preview_and_confirm2(ifc_file, operations):
        print(_exit_message(already_written))
        return False

    # 手順3: 適用(進捗は間引いて表示)→ 結果表示。source_nameは入力ファイル名を
    # 明示的に渡す(CUI Phase2 Task1: apply_operationsにはfileオブジェクトを渡す
    # ため、既定導出"(in-memory)"では出力ヘッダの由来刻印から元ファイル名が
    # 失われてしまう)。
    #
    # フェーズ最終レビューI4: apply_operationsに渡すfileオブジェクトは契約上
    # 使い捨て(以後この呼び出し側で再利用しない)。呼び出し前に自分の参照
    # (ifc_file)をNoneにし、別名(ifc_file_to_apply)だけで渡すことで、
    # apply_operations内部の書き出し時GC(mark-and-sweep、fatサイズの約4.8倍
    # のメモリを使う)が走る間もこの関数のローカル変数がモデルを掴み続ける
    # ことを避ける。呼び出し後は別名も明示的にdelする。
    ifc_file_to_apply = ifc_file
    ifc_file = None
    report = apply_operations(
        ifc_file_to_apply,
        operations,
        output_path,
        progress=_make_progress_printer(),
        source_name=Path(path).name,
        # carry-forward Phase E: GUIの_run_exportと同じく常に明示的に渡す
        # (既定値の暗黙依存を作らない)。"inline"は簡略化のたびに旧形状を
        # 逐次回収する省メモリ方式(GUIチェックボックスと同じもの)。
        geometry_cleanup="inline" if inline_cleanup else "gc",
    )
    del ifc_file_to_apply
    _print_report(report)
    return True


def _run_text_apply(
    path: str,
    output: str | None,
    output_path: str,
    intents: list[Intent],
    *,
    already_written: bool = False,
) -> tuple[bool, bool]:
    """テキストモード経路(cui-design.md §8、CUI Phase3 Task5 監督者裁定4)。

    already_written: `_run_apply` の同名引数の意味と同じ(M5(ii)、フェーズ
    最終レビュー)。このルート単体の確認2で no と答えた際の中断メッセージが、
    セッション全体として見て事実に反することがないよう `_exit_message` に渡す。

    手順: (i) 参照グラフスキャン(所要秒数を表示。手順2の「フルオープン中...」
    表示と同じ流儀) → (ii) `compute_text_delete_plan` → (iii) 確認2
    (statsを人間可読に表示してから確認) → (iv) 出力ファイル名プロンプト
    (手順1bと完全に同一規約、`_maybe_prompt_output_filename` を再利用) →
    (v) `rewrite_without`(進捗は既存プリンタを再利用) → (vi) 結果表示。

    `ifcopenshell.open` は一度も呼ばない(このフェーズの存在意義、監督者裁定8)。

    戻り値: `(handled, wrote)` の2値タプル(M5(ii)、フェーズ最終レビューで
    `wrote` を追加——`_run_apply`/`run()` がファイル書き込みの有無を正確に
    追跡できるようにするため。呼び出し元の解釈は監督者裁定4/5から不変)。
    - `handled`: True ならこのルートで完結した(呼び出し元はそのまま
      `return wrote` する。出力を書いて完了した場合、確認2で no と答えて
      中断した場合、C1の同一パスガードで中断した場合のいずれもTrue)。
      False は監督者裁定5(seeds==0: 指定クラスが参照グラフ上で1件も見つから
      ない)によるフルオープン経路へのフォールバックを意味し、呼び出し元は
      確認1以降へそのまま処理を続ける(黙って出力を書くと「削除したつもりで
      中身が元と同じファイル」という最も危険な失敗モードになるため、この
      ケースだけは出力を一切書かずに戻る。`wrote` は常にFalse)。
    - `wrote`: `handled`がTrueのときのみ意味を持つ。実際に `rewrite_without`
      が出力ファイルへの書き込みを完了したときだけTrue(確認2のno・C1の
      同一パスガードでの中断はいずれも書き込み前の中断なのでFalse)。
    """
    print("参照グラフスキャン中...", flush=True)
    t0 = time.monotonic()
    graph = scan_full_graph(path)
    print(f"参照グラフスキャン完了: {time.monotonic() - t0:.1f}秒")

    delete_classes = [i.ifc_class for i in intents if i.op == "delete"]
    plan = compute_text_delete_plan(graph, delete_classes)

    if plan.stats["seeds"] == 0:
        print(
            "指定クラスが参照グラフ上で見つかりませんでした。"
            "従来のフルオープン経路にフォールバックします。"
        )
        return False, False

    # 手順(iii): 確認2(statsを人間可読に表示。監督者裁定4)。
    #
    # I2(Important、フェーズ最終レビュー): ここで分かる数値はいずれも
    # plan(rewrite_without実行前の見積り)由来であり、実行結果
    # (RewriteReport、_print_rewrite_report)とは意味が異なる。ラベルは
    # plan.py の docstring が定義する正確な意味に合わせて正直化する:
    # - drop_ids.size は「この時点で確定している」削除レコード数の下限
    #   であり、rels_patched の候補のうち実行時にレコードごと削除へ確定した
    #   分だけ、実際の削除レコード総数はこれより増え得る(patch.pyの規則
    #   3/4はレコードのテキスト解析が要るため、Task2(plan.py)の時点では
    #   まだ確定しない)。
    # - rels_patched は「参照リスト修正 or レコードごと削除」の**候補**数
    #   であり、その内訳(何件が実際にpatchされ、何件がdropされたか)は
    #   rewrite_without実行後にしか確定しない。
    # - rels_dropped(plan.py側の意味: カスケード/sweepで死んだ関係レコード
    #   数)は上のdrop_ids.sizeに**既に含まれている**内訳であり、実行時に
    #   追加で削除される件数(patch候補の一部がdropされる分)とは別物
    #   なので、その旨を明記して内訳行に統合する(誤読を招く独立行としては
    #   出さない)。
    # N3(フェーズ最終レビューの再審): 下限だけの表示では隠れている差の
    # 大きさが伝わらない(small.ifc/IfcDuctSilencer の実測では下限220件に対し
    # 実際は572件=+160%)。厳密な上限は「確定分 + patch候補の全件がレコード
    # ごと削除に倒れた場合」なので、追加パス無しで計算できる。下限と上限を
    # 併記してユーザーが結果をブラケットできるようにする。
    print("=== テキストモード適用内容(参照グラフスキャン結果) ===")
    print(
        f"削除レコード数(見積り): {int(plan.drop_ids.size)}〜"
        f"{int(plan.drop_ids.size) + plan.stats['rels_patched']}件"
        "(確定分〜参照リスト修正候補が全件レコードごと削除になった場合)"
    )
    print(
        f"  内訳: 直接指定{plan.stats['seeds']}件 + 連鎖{plan.stats['cascade']}件"
        f" + 専有回収{plan.stats['swept']}件"
        f"(うち巻き添えで消える関係レコード{plan.stats['rels_dropped']}件を含む)"
    )
    print(
        "参照リスト修正候補(rel patch候補、実行時に「参照リスト修正」または"
        f"「レコードごと削除」のいずれかに確定します): {plan.stats['rels_patched']}件"
    )
    print("  内訳(何件が修正され何件が削除されたか)は実行後の結果表示で確定します。")
    if not _confirm("実行しますか? (y/N): "):
        print(_exit_message(already_written))
        return True, False

    # 手順(iv): 出力ファイル名プロンプト(1bと完全に同一規約)。
    output_path = _maybe_prompt_output_filename(path, output, output_path)

    # C1(Critical、フェーズ最終レビュー): 出力先=入力先だと rewrite_without が
    # 出力ファイルを開いた瞬間に入力をtruncateしてしまう(ライブラリ層にも
    # 同じガードがあるが、UI層でも早期に検出してフルオープン経路と同じ
    # エラー表示に揃える)。
    if _output_path_collides_with_source(path, output_path):
        _print_output_collision_error(output_path)
        return True, False

    # 手順(v): ストリーム書き換え(ifcopenshellを一度も開かない)。
    #
    # 監督者裁定6: records_in は壊れたレコードを含むため graph.record_count を
    # 超え得る(= 最終発火が done==total にならず、進捗プリンタが最終行を改行で
    # 確定しないことがある)。最後に転送した (done, total) を覚えておき、
    # 未確定のときだけ改行する(無条件に print() すると通常ケースで空行が
    # 1行余る。プリンタ側の間引き条件(_PROGRESS_STRIDE)をここで再実装しない
    # ため、改行の有無は「最終発火が done==total だったか」だけで判定する
    # ——プリンタが改行するのはその条件のときだけ)。
    printer = _make_progress_printer()
    last_fired = {"done": 0, "total": 0}

    def _tracked_progress(stage: str, done: int, total: int) -> None:
        last_fired["done"] = done
        last_fired["total"] = total
        printer(stage, done, total)

    report = rewrite_without(
        path,
        output_path,
        plan,
        graph,
        source_name=Path(path).name,
        progress=_tracked_progress,
    )
    if last_fired["done"] != last_fired["total"]:
        print()

    # 手順(vi): 結果表示(_print_report は ExportReport 専用のため流用しない。
    # 監督者裁定7)。
    _print_rewrite_report(report, output_path)
    return True, True


def _shared_spillover_counts(
    ifc_file, operations, extra_excluded: set[str] | None = None
) -> dict[str, int]:
    """共有波及(scope="shared")の simplify が、どの操作の対象にも入っていない
    要素へ波及する件数をクラス別に数える(確認2の開示用)。

    共有マップは同一形状を複数要素で使い回すため、対象クラスだけを指定しても
    同じマップを使う別クラスの要素の形状が一緒に変わる。GUIは共有波及プレビュー
    で開示しており、CUIも黙って波及させない(docs/plans/2026-07-31-cui-shared-scope.md
    設計判断2)。simplify対象と削除対象は開示から除く(前者は指定済み、後者は
    どうせ消える)。走査はマップ単位で1回(同じマップを使う対象が何千要素でも
    inverse走査は1回で済む)。

    extra_excluded: 呼び出し側(_preview_and_confirm2)が別途計算済みの削除連鎖
    (direct+cascaded)の GlobalId 集合を渡せる(フェーズ最終レビューM-7)。
    delete op.targets(直接指定)は上の走査で既に除外しているが、集約の子部材や
    開口の充填要素のように連鎖でしか消える要素は op.targets には現れないため、
    このパラメータで合流させないと「どうせ消える要素」が波及開示に紛れ込む
    (安全側だが過剰開示)。省略時(None)は従来どおり直接指定のみを除外する。
    """
    excluded: set[str] = set()
    for op in operations:
        if op.op in ("simplify", "delete"):
            excluded.update(op.targets)
    if extra_excluded:
        excluded.update(extra_excluded)

    spillover: dict[str, int] = {}
    seen_maps: set[int] = set()
    counted: set[str] = set()
    for op in operations:
        if op.op != "simplify" or op.scope != "shared":
            continue
        for gid in op.targets:
            try:
                element = ifc_file.by_guid(gid)
            except RuntimeError:
                continue
            map_key = _shared_map_key(ifc_file, element)
            if map_key is None or map_key in seen_maps:
                continue
            seen_maps.add(map_key)
            for sibling_gid in get_shared_element_gids(ifc_file, gid):
                if sibling_gid in excluded or sibling_gid in counted:
                    continue
                counted.add(sibling_gid)
                try:
                    sibling = ifc_file.by_guid(sibling_gid)
                except RuntimeError:
                    continue
                spillover[sibling.is_a()] = spillover.get(sibling.is_a(), 0) + 1
    return spillover


def _confirm2_advisories(ifc_file, operations) -> list[str]:
    """確認2でのGUI同等の適正判定を、実ファイルのサンプル実測で組み立てる。

    背景(要件2、GUI同等化): GUIはロード時に実ファイルをフルオープンしており
    サンプル実測(hull_triangle_ratio/obb_volume_ratio等)を持てるが、CUIは
    コマンド応答時点では軽量スキャン(メッシュを持たない)しか使えず
    advise_simplify の一部の規則(ほぼ凸警告・OBB推奨)が常にNoneで沈黙する。
    確認2はフルオープン済み(_preview_and_confirm2)なので、ここで初めて
    実メッシュのサンプル実測が可能になる。

    グループ化: simplify操作の対象GlobalId(実ファイルに存在するものだけ)を
    (element.is_a().upper(), method) でまとめる。同一グループが複数の
    Operation(例: scope違い)に分かれていても対象は合流する。

    決定性: 各グループのサンプルはgid昇順で先頭 `_ADVISOR_SAMPLE_SIZE` 件に
    固定する(挿入順・辞書順のブレを排除)。グループの処理順もキー(クラス名,
    method)の昇順に固定し、出力行の順序を安定させる。

    メッシュ化に失敗した要素はそのサンプルからスキップする(advisor.py側の
    退化形状スキップと同じ方針)。triangle_source は
    extract.py._analyze_representation を再利用して representation_types の
    和集合から導く(サーバ側 app.py._triangle_source_by_class と同じ意味論)。

    戻り値は「注意(クラス名 / ラベル): 文」の行のリスト。同一行は初出のみ残す。
    """
    real_gids = {gid for gid, _cls, _name in extract_elements_light(ifc_file)}

    targets_by_group: dict[tuple[str, str], set[str]] = {}
    for op in operations:
        if op.op != "simplify":
            continue
        method = op.params.get("method")
        for gid in op.targets:
            if gid not in real_gids:
                continue
            try:
                element = ifc_file.by_guid(gid)
            except RuntimeError:
                continue
            key = (element.is_a().upper(), method)
            targets_by_group.setdefault(key, set()).add(gid)

    if not targets_by_group:
        return []

    settings = ifcopenshell.geom.settings()
    settings.set("weld-vertices", True)
    settings.set("apply-default-materials", False)

    lines: list[str] = []
    seen: set[str] = set()
    for ifc_class, method in sorted(targets_by_group):
        sample_gids = sorted(targets_by_group[(ifc_class, method)])[:_ADVISOR_SAMPLE_SIZE]

        shapes = []
        rep_types: set[str] = set()
        for gid in sample_gids:
            element = ifc_file.by_guid(gid)
            types, _is_mapped, _layer = _analyze_representation(
                getattr(element, "Representation", None)
            )
            rep_types.update(types)
            try:
                shape = ifcopenshell.geom.create_shape(settings, element)
            except Exception:  # noqa: BLE001 - 幾何化できない要素はサンプルからスキップする
                continue
            shapes.append(_shape_from_geometry(shape.geometry))

        avg_triangles_per_shape = (
            None if not shapes else sum(s.triangle_count for s in shapes) / len(shapes)
        )
        if not rep_types:
            triangle_source = None
        elif "Tessellation" in rep_types:
            triangle_source = "tessellation"
        else:
            triangle_source = "other"

        metrics = metrics_from_shapes(shapes)
        label = _SIMPLIFY_PREVIEW_LABELS.get(method, str(method))
        for msg in advise_simplify(
            method,
            avg_triangles_per_shape=avg_triangles_per_shape,
            triangle_source=triangle_source,
            hull_triangle_ratio=metrics.get("hull_triangle_ratio"),
            obb_volume_ratio=metrics.get("obb_volume_ratio"),
        ):
            line = f"注意({ifc_class} / {label}): {msg}"
            if line not in seen:
                seen.add(line)
                lines.append(line)

    return lines


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
    delete_closure_gids: set[str] = set()
    if delete_targets:
        closure = compute_delete_closure(ifc_file, sorted(delete_targets))
        summary.append(f"削除 直接{len(closure.direct)}件+連鎖{len(closure.cascaded)}件")
        delete_closure_gids = closure.all_gids
    for label, count in simplify_counts.items():
        summary.append(f"{label} {count}件")

    print("=== 適用内容(実ファイルで確認済み) ===")
    print(" / ".join(summary) if summary else "(適用対象なし)")

    # extra_excluded=delete_closure_gids: 削除連鎖(集約の子部材・開口の充填要素等)
    # でどうせ消える兄弟は、simplify/delete の直接対象でなくても開示から除く
    # (フェーズ最終レビューM-7、設計判断2の意図と揃える)。
    spillover = _shared_spillover_counts(
        ifc_file, operations, extra_excluded=delete_closure_gids
    )
    if spillover:
        print(_format_spillover_line(spillover))

    # GUI同等の適正判定(要件2): フルオープン済みのこの時点で初めて実メッシュの
    # サンプル実測が可能になるため、確認2でだけ4規則フルの注意を出す。
    for line in _confirm2_advisories(ifc_file, operations):
        print(line)

    return _confirm("実行しますか? (y/N): ")


def _format_spillover_line(spillover: dict[str, int]) -> str:
    """共有波及の開示行を整形する(確認2用の純粋関数、フェーズ最終レビューM-3)。

    上位5クラスまでの内訳を件数降順で表示し、6クラス以上あれば
    「...他Nクラス」を付ける。spillover が空の場合は呼び出し側
    (_preview_and_confirm2)が呼ばない前提(このケースは想定していない)。

    文言はフェーズ最終レビューM-1+I-3の裁定により「一緒に変わります」の断定を
    避け「対象になります」に緩めている(MappingTargetが逆変換不能な場合は
    scope="element"へフォールバックし実際には変わらないことがあるため。
    直接共有(IfcMappedItem非経由)も2026-08-01から集計対象になったが、
    フォールバックの可能性があるため文言は据え置き)。
    """
    total = sum(spillover.values())
    ordered = sorted(spillover.items(), key=lambda kv: -kv[1])
    detail = ", ".join(f"{cls}: {n}" for cls, n in ordered[:5])
    rest = len(ordered) - 5
    if rest > 0:
        detail += f", ...他{rest}クラス"
    return (
        f"共有波及: 操作で指定していない {total}要素 の形状も対象になります"
        f"({detail})。"
    )


def summarize_warnings(warnings: list[str], top: int = 5) -> list[str]:
    """警告リストを「合計件数(種類数)」の見出し+件数降順の上位 top 種に
    要約した表示行のリストを返す(warnings が空なら空リスト)。

    実データでは同じ文言の警告が要素数ぶん重複する(test-donuts_mini.ifc の
    decimate で同文456件)。件数だけの表示では中身が判断できず、全件表示は
    画面を流し切ってしまうため、同文を畳んで件数を付け、上位だけ出す。
    順序は件数降順、同数なら初出順(Counter は挿入順を保ち、sorted は安定)。
    """
    if not warnings:
        return []
    counts = Counter(warnings)
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    lines = [f"警告: {len(warnings)}件({len(counts)}種)"]
    for message, n in ordered[:top]:
        suffix = f" ×{n}" if n > 1 else ""
        lines.append(f"  - {message}{suffix}")
    rest = ordered[top:]
    if rest:
        rest_total = sum(n for _, n in rest)
        lines.append(f"  … 他 {len(rest)}種 {rest_total}件")
    return lines


def _print_report(report: ExportReport) -> None:
    print("=== 完了 ===")
    print(f"出力ファイル: {report.output_path}")
    print(f"削除: {len(report.deleted)}要素")
    print(f"簡略化: {len(report.simplified)}要素")
    if report.skipped:
        print(f"スキップ: {len(report.skipped)}件")
    for line in summarize_warnings(report.warnings):
        print(line)
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


def _print_rewrite_report(report: RewriteReport, output_path: str) -> None:
    """テキストモード経路の結果表示(CUI Phase3 Task5 監督者裁定7: `_print_report`
    は `ExportReport` 専用のため流用しない)。"""
    print("=== 完了(テキストモード) ===")
    print(f"出力ファイル: {output_path}")
    print(f"削除レコード数: {report.records_dropped}")
    print(f"パッチ済みrel件数: {report.rels_patched}")
    print(f"dropされたrel件数: {report.rels_dropped}")
    print(f"出力サイズ: {report.bytes_out:,} bytes")


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
