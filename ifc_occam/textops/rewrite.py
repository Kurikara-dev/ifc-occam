"""ストリーム書き換え + 由来刻印 (cui-design.md §8、
docs/plans/2026-07-25-cui-phase3.md Task 4)。

`rewrite_without(src_path, out_path, plan, graph, source_name, progress=None)
-> RewriteReport` は、Task 1(`ifc_occam.scan.fullgraph.FullGraph`)・
Task 2(`ifc_occam.textops.plan.TextDeletePlan`)・
Task 3(`ifc_occam.textops.patch.patch_rel_record`)を繋いで、元のIFCファイルを
一度も ifcopenshell でフルオープンせずに削除のみの操作をバイト列ストリームの
書き換えとして適用する、CUI Phase3 の最終段。**本モジュールは ifcopenshell を
import しない**(監督者裁定11。テスト側では import してよい)——これが
「フルオープン不能級の巨大IFCを扱う」という本フェーズの存在意義そのものである。

## 手順

1. `src_path` の先頭〜最初の `DATA;` までを「ヘッダ」として読み切り(quote/
   comment-blind な単純バイト列検索。reader.py の `_RecordScanner.skip_past`
   と同じ制約をヘッダ複写でも踏襲する——理由は `_read_header` docstring 参照)、
   そのヘッダの `FILE_DESCRIPTION` 第1引数(description のタプル)の**末尾に**
   由来刻印3エントリをテキストレベルで追記する(`_stamp_header` 参照)。
   既存エントリ(ViewDefinition 等の MVD 宣言)はバイト単位で温存する。
2. `iter_records(src_path)` で全レコード(reader.py の契約により、元ファイルが
   複数の DATA セクションを持っていても横断して1本のストリームになる)を
   順に消費し、各レコードについて:
   - `_match_header` が None(壊れたレコード)→ id が取れないので drop 判定
     できない。verbatim で出力し `records_in` にも数える(裁定6)。
   - id ∈ `plan.drop_ids` → skip(出力しない。`records_dropped` を加算)。
   - id ∈ `plan.patch_rel_ids` → `patch_rel_record(record, plan.drop_ids)` を
     呼ぶ。None なら drop(`rels_dropped` を加算)。入力と異なるバイト列が
     返れば `rels_patched` を加算した上で、その結果を出力する(入力と同じ
     バイト列が返った場合はどちらのカウンタも増やさず、そのまま出力する
     ——patch_rel_record 自身がその候補には実際には除去すべき参照が無かったと
     判定したケース。patch.py docstring「body内部の括弧が対応していない」
     分岐等、通常到達しうる)。
   - それ以外 → verbatim + 改行区切りで出力する。
3. 全レコードを消費したら `ENDSEC;` + `END-ISO-10303-21;` を書いて締める。

複数 DATA セクション入力は、この手順の結果として自動的に単一 DATA セクション
へ統合される(`iter_records` が全 DATA セクションを横断した1本のストリームを
返し、本モジュールが自前で1回だけ `ENDSEC;`/`END-ISO-10303-21;` を書くため。
Global Constraints)。

## 出力の刻印カウント(監督者裁定2)

`deleted_count = plan.stats["seeds"] + plan.stats["cascade"]`、
`simplified_count = 0` を `build_provenance_lines` に渡す。`stats["swept"]`
(専有サブグラフ回収で連鎖的に消えた**幾何・補助レコード**)や
`stats["rels_dropped"]`(除去された**関係レコード**)は IFC の「要素
(element)」ではないため、これらを混ぜると刻印が「削除要素数」を偽ることに
なる——`seeds`(明示指定クラスの実体)+`cascade`(3関係クラスによる意味論的
連鎖削除で実際に消えた**製品/開口/部材**相当のレコード)だけが「削除要素数」
の実体に対応する。`simplified_count` は常に0(テキスト経路は削除のみの
フェーズであり、bbox/hull/decimate 等の幾何簡略化はこの経路の対象外)。

## FILE_NAME.originating_system は書き換えない(監督者裁定3)

`core/export.py:_stamp_provenance`(フルオープン経路)は
`FILE_NAME.originating_system` も上書きするが、本モジュールはそれを**行わない
——意図的な判断**。brief の Interfaces は「FILE_DESCRIPTION の description
リスト末尾に...刻印3エントリをテキストレベルで追記」としか要求しておらず、
`FILE_NAME` 行のテキストレベル書き換えは対象外。`FILE_NAME` は
`FILE_DESCRIPTION` より属性数が多くフィールド境界の特定コストが高い一方、
「非正本マーク」としての実効性は description 側の3行で既に十分に果たされて
いるため、スコープを広げない(YAGNI)。

## ヘッダ複写の quote/comment-blind な制約(監督者裁定3・ruling適用)

`_read_header` は文字列リテラル・コメントを認識しない単純なバイト列走査
(`DATA;` の単純検索)である。reader.py の `_RecordScanner.skip_past` が同じ
制約を明記している(HEADER 内の文字列が偶然 `DATA;` という並びを含むと
誤認する)ため、本モジュールも `_read_header` のヘッダ複写で同じ制約を
踏襲する(`_read_header` docstring 参照。**この関数自体は変更しない**——
`iter_records` と同じ規約により、両者が判定する DATA 開始位置は常に一致する)。

`_stamp_header` は元々(監督者裁定3時点)括弧の深さだけを追跡し引用符を見ない
実装だったが、修正3(Important-4、2026-07-25レビュー)で quote-aware になった
——文字列内に釣り合わない丸括弧があると既存 description エントリを無音で
破壊する事故が実測で確認されたため、深さ走査の前に `parser.py` の
`_blank_strings` でヘッダをブランク化し、文字列内の丸括弧を隠してから
構造を見る(`_stamp_header` docstring参照)。

## 非ASCIIエスケープの分岐(修正1: 監督者裁定3の訂正、2026-07-25レビュー)

裁定3の原文は「非ASCII文字は連続ランごとに `\\X2\\...\\X0\\` へエスケープする」
だったが、これは誤りだった。エンコード側が生成するバイト列自体は数学的に正しい
UTF-16BE(例: U+1F600 → `D83DDE00`)であり、本プロジェクト自身の
`scan/parser.py:_decode_x2_runs` は正しく復号できる。しかし **ifcopenshell
0.8.5 は `\\X2\\` 内のサロゲートペアを合成復号できず、該当文字を例外を出さず
無音で消失させる**(実測で確認済み: `source_name="\U0001F600"` を `\\X2\\`
エスケープして出力し `ifcopenshell.open` で読むと、対応する description
エントリが空文字列になる。絵文字だけでなく実在の補助面漢字 U+29E3D でも同様に
消失する)。ifcopenshell 自身の header ライタは非BMP文字に
`\\X4\\<32bitのUCS-4を8桁hexで>\\X0\\` を使い、これは正しく round-trip する。

そこで、エスケープはコードポイント単位で分岐する(`_encode_step_string_body`):
- ASCII(U+0000〜U+007F): エスケープ不要。
- BMP の非ASCII(U+0080〜U+FFFF): `_encode_x2_run`(`\\X2\\` + 各文字4桁hex)。
- 非BMP(U+10000以上): `_encode_x4_run`(修正1。`\\X4\\` + 各文字8桁hex)。

連続ランは同種(BMP/非BMP)ごとに1組の `\\X2\\...\\X0\\`/`\\X4\\...\\X0\\`
にまとめる。異種が隣接する場合はランを切り替え、`\\X2\\...\\X0\\\\X4\\...\\X0\\`
のように連続してよい(ISO 10303-21 の規則どおり、各エスケープランは
`\\X0\\` で閉じる)。round-trip の証拠は
`tests/test_textops_rewrite.py` の非BMP関連テスト群(絵文字単独・補助面漢字
単独・ASCII+BMP+非BMP混在・非BMP2文字連続)を参照。

## バックスラッシュの二重化(修正2)

`_encode_step_string_body` は STEP のエスケープ導入文字である `\\`
(バックスラッシュ)自体を、以前は無変換で出力していた。これは
「表示崩れ」ではなく**実際に ifcopenshell をクラッシュさせる**欠陥だった
(実測: `source_name=r"C:\\path\\to\\file.ifc"` を無変換で出力したファイルを
`ifcopenshell.open` すると、最小構成のヘッダではセグメンテーションフォルト
(exit code 139)でプロセスごと落ちる。より複雑な実際のヘッダ構成では
代わりに `ifcopenshell.Error: Unable to parse IFC SPF header` という
catchable な例外になる場合もある——どちらの症状も同じ根本原因〈パーサが
非対応のエスケープ列に遭遇する〉のメモリ安全性バグの現れであり、症状の
違いはヒープ配置等の周辺条件に依存する)。

修正: リテラルの `\\` を `\\\\` に二重化する(ifcopenshell 自身の writer と
同じ標準STEPエスケープ)。二重化は ASCII 文字同士の置換であり、後段で生成する
`\\X2\\`/`\\X4\\`/`\\X0\\` というエスケープ導入子(この時点ではまだ生成
されていない)を誤って二重化する余地はない——`_encode_step_string_body` は
`\\`→`\\\\`・`'`→`''` の2つのASCII側変換を**先に**まとめて済ませてから、
コードポイント単位のラン分類・非ASCIIエスケープ生成を**後段で**行う構造に
なっている。round-trip の証拠は `tests/test_textops_rewrite.py` の
`test_header_stamp_round_trips_backslash_in_source_name_without_crashing`
(クラッシュも例外も起きずに復元できることを確認)と
`test_header_stamp_round_trips_quote_backslash_and_non_ascii_all_mixed`
(`'`・`\\`・非ASCIIの同時混在)を参照。

## ストリーム性(監督者裁定8)

出力全体をメモリに溜めない。出力ファイルは `open(..., "wb")` で開いて
レコードごとに逐次 `write` する。入力も `iter_records` の逐次消費のみ
(HEADER 部分だけは実サイズが小さいため丸ごと読む——`_read_header` docstring
参照。DATA セクション本体をバッファに溜めることは一切しない)。

## メンバシップ判定(監督者裁定5)

`plan.drop_ids`/`plan.patch_rel_ids` を Python の set/dict へ変換しない
(レコード規模になり得るため)。`_is_member` が `np.searchsorted` +
クランプ+等値ガードで1件ずつ判定する(patch.py `_is_dead_mask` / plan.py
`_class_code` 等と同型のミスガードパターン)。1レコードあたりのコストは
O(log k)(k=`len(drop_ids)`/`len(patch_rel_ids)`)であり、`len(drop_ids)` に
比例するコストは発生しない。

## ソート前提の1回検証(監督者裁定4)

ストリーム開始前に `plan.drop_ids` と `plan.patch_rel_ids` の両方が昇順
ソート済み(狭義単調増加)であることを1回だけ検証し(O(k))、破れていたら
`ValueError`(出力ファイルはまだ開いていないため、検証失敗時に部分的な
出力ファイルは残らない)。理由: `patch_rel_record` は1回の呼び出しコストを
「そのレコード内の参照数」に比例させる設計のため per-call のソート検証を
行わない——前提が崩れると `np.searchsorted` が例外を出さず無音で dead を
alive と誤判定し、出力に dangling 参照を残す。検証はレコード規模のループの
外にあるここが唯一の適切な場所。`patch_rel_ids` も同じ理由で検証する
(brief は drop_ids のみ必須と定めるが、`_is_member` が同じ searchsorted
ミスガードパターンに依存するため、対称に検証してよいという裁定に従う)。

## 出力先=入力先の禁止(C1、Critical、フェーズ最終レビュー)

`out_path == src_path`(出力先=入力先)だと、`open(out_path, "wb")` が
`iter_records(src_path)` より**先**に実行されるため、入力ファイルが1バイトも
読まれる前に truncate されてしまう(実測: 21,529,266 bytes の入力が、例外も
警告も出さずに453バイト(header + 空DATA)へ破壊された上で「完了」と表示
された)。`_refers_to_same_file` が `_ensure_sorted_ascending` と同じ位置
(=出力ファイルを開く前。部分出力も残さない)でこれを検出し `ValueError`。
判定順序(取り違えを避けるため): 両方が存在するなら `os.path.samefile`
(inode比較。Windowsの大文字小文字違い・8.3短縮名・シンボリックリンクに強い)、
そうでなければ `Path.resolve()` の比較(詳細は関数docstring参照)。

UI層(`cui/repl.py`)にも独立したガードがある——本モジュールのガードは
テキスト経路にしか効かないが、このツールの契約(原本非破壊)はフルオープン
経路にも及ぶため(フルオープン経路は truncate しないだけで原本上書きは
同様に契約違反)。

事後条件(保険): `plan.drop_ids` が非空なのに `records_in == 0`(入力から
1レコードも読めなかった)場合も `ValueError`(監督者裁定5「seeds==0なら
書かない」と対になる保険——削除するつもりのレコードがあるのに入力が空、
という矛盾を fail-loud にする)。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ifc_occam.core.paths import refers_to_same_file
from ifc_occam.core.provenance import build_provenance_lines
from ifc_occam.scan.fullgraph import FullGraph
from ifc_occam.scan.parser import _blank_strings, _match_header
from ifc_occam.scan.reader import iter_records
from ifc_occam.textops.patch import patch_rel_record
from ifc_occam.textops.plan import TextDeletePlan

__all__ = ["RewriteReport", "rewrite_without"]

_DATA_MARKER = b"DATA;"
_HEADER_READ_CHUNK = 64 * 1024
_FILE_DESCRIPTION_RE = re.compile(rb"FILE_DESCRIPTION\s*\(")


@dataclass(slots=True)
class RewriteReport:
    """`rewrite_without` の結果報告(cui-phase3 Task 4 契約。フィールド名/型は
    docs/plans/2026-07-25-cui-phase3.md Task 4 から verbatim。各フィールドの意味は
    監督者裁定7で確定した定義を採用する)。

    - records_in: `iter_records` が返したレコード総数(壊れたものも含む)。
    - records_dropped: `drop_ids` に含まれて出力しなかった数。
    - rels_patched: `patch_rel_record` が入力と異なるバイト列を返した数。
    - rels_dropped: `patch_rel_record` が None を返して落とした数。
    - bytes_out: 書き出した総バイト数(出力ファイルの実サイズ)。
    """

    records_in: int
    records_dropped: int
    rels_patched: int
    rels_dropped: int
    bytes_out: int


def _refers_to_same_file(a: str | Path, b: str | Path) -> bool:
    """`core/paths.refers_to_same_file` の別名(C1)。

    判定の実体は `ifc_occam/core/paths.py` に1つだけ置く。フルオープン経路
    (`core/export.py`)と UI 層(`cui/repl.py`)も同じ関数を使う——同じ判定を
    各所で独立に書くと、片方だけ修正されて食い違うため。
    """
    return refers_to_same_file(a, b)


def _ensure_sorted_ascending(ids: np.ndarray, name: str) -> None:
    """ids(TextDeletePlan の契約により昇順ソート済みのはずの配列)がその前提を
    満たしているかをストリーム開始前に1回だけ検証する(O(k)。監督者裁定4)。
    破れていれば ValueError。

    `np.diff` は size 0/1 の配列に対して空配列を返し、`np.all` は空配列に
    対して True を返す(vacuously true)ため、要素数0/1の場合は特別扱い
    不要でそのまま「ソート済み」と判定される。

    M3(Minor、フェーズ最終レビュー): 本関数は狭義単調増加(重複なし)を要求
    する。入力IFCに重複 record id があると、`compute_text_delete_plan` の
    `np.sort(graph.ids[dead])` はその重複を正当に含んだまま返すため、本関数
    はそれを契約違反として弾く(fail-loud 自体の挙動は正しく、変更しない)。
    ここで弾かれた場合の実務上の手がかりとして、エラーメッセージに「入力
    ファイルに重複した record id がある可能性」を明示する。
    """
    if not np.all(np.diff(ids) > 0):
        raise ValueError(
            f"plan.{name} must be strictly ascending sorted "
            "(TextDeletePlan contract violated); patch_rel_record's per-call "
            "searchsorted membership check would silently misjudge dead as "
            "alive and leave dangling references in the output "
            "(a non-strictly-increasing array most often means the input IFC "
            "file has duplicate record ids — check src_path for that)"
        )


def _is_member(value: int, sorted_ids: np.ndarray) -> bool:
    """value が sorted_ids(昇順ソート済み、重複なし)に含まれるかを
    searchsorted + 等値ガードで判定する(監督者裁定5: sorted_ids を
    Python の set/dict へ変換しない。1回の呼び出しコストは O(log k))。
    """
    if sorted_ids.size == 0:
        return False
    pos = np.searchsorted(sorted_ids, value)
    if pos >= sorted_ids.size:
        return False
    return int(sorted_ids[pos]) == value


def _read_header(path: str | Path) -> bytes:
    """path の先頭から最初の `DATA;` マーカーの直後までを読み切り、そのバイト列
    (マーカー自身を含む)を返す。

    reader.py の `_RecordScanner.skip_past` と同じ contract(quote/comment-
    blind な単純バイト列検索)をヘッダ複写でも踏襲する(モジュールdocstring
    「ヘッダ複写のquote/comment-blindな制約」参照)。HEADER セクションは
    実サイズが小さい(実データで確認済み、高々数百バイト~数KB)ため、
    見つかるまでチャンク読みして1箇所にまとめて保持しても
    「出力全体をメモリに溜めない」というストリーム性の制約(裁定8。DATA
    セクション本体を指す)には抵触しない。

    `iter_records(path)` は独立に自前でファイルを開き直す(モジュール自体は
    ファイルオブジェクトを共有する API を持たない)ため、本関数もここで
    完結する短命な読み取り専用ハンドルを別途開く。ヘッダ部分(高々数KB)を
    2回読むことになるが、DATA セクション本体(ストリームの本体、GB級になり
    得る)は一度も余分に読まない。

    ファイル中に `DATA;` が1つも見つからずEOFに達した場合は ValueError
    (有効なSTEPファイルなら`DATA;`は必ず存在するmandatory markerであり、
    `iter_records`の「壊れていても黙って打ち切る」規約とは異なり、ヘッダ
    複写という本関数の役割上、ここでは早期に失敗を明示する)。
    """
    with open(path, "rb") as f:
        buf = bytearray()
        while True:
            chunk = f.read(_HEADER_READ_CHUNK)
            if not chunk:
                raise ValueError(
                    f"DATA; marker not found before EOF in {path!r}: not a valid STEP file"
                )
            buf += chunk
            idx = buf.find(_DATA_MARKER)
            if idx != -1:
                return bytes(buf[: idx + len(_DATA_MARKER)])


_NON_BMP_THRESHOLD = 0x10000  # U+10000以上は非BMP(UTF-16BEがサロゲートペアになる境界)


def _encode_x2_run(s: str) -> bytes:
    """BMP(U+0080〜U+FFFF)の非ASCII文字の連続ラン(str、1文字以上、全て
    U+10000未満)を `\\X2\\<UTF-16BEの16進大文字>\\X0\\` にエンコードする
    (1ランにつき1組。監督者裁定3、2026-07-25レビューで非BMPの扱いを訂正
    ——モジュールdocstring「非ASCIIエスケープの分岐(修正1)」参照)。
    """
    hex_digits = s.encode("utf-16-be").hex().upper().encode("ascii")
    return b"\\X2\\" + hex_digits + b"\\X0\\"


def _encode_x4_run(s: str) -> bytes:
    """非BMP(U+10000以上)文字の連続ラン(str、1文字以上)を
    `\\X4\\<UCS-4の16進大文字、各文字8桁>\\X0\\` にエンコードする(1ランに
    つき1組。監督者裁定3の訂正——モジュールdocstring「非ASCIIエスケープの
    分岐(修正1)」参照)。

    ifcopenshell 0.8.5 は `\\X2\\` 内のUTF-16BEサロゲートペアを合成復号
    できず、対応する文字を例外を出さず無音で消失させる(実測で確認済み)。
    `\\X4\\` はUCS-4(1文字=32bit=8桁hex)を直接指定するため、ifcopenshell
    自身のheaderライタが非BMP文字に対して使う方式と同じであり、正しく
    round-tripする。
    """
    hex_digits = "".join(f"{ord(ch):08X}" for ch in s).encode("ascii")
    return b"\\X4\\" + hex_digits + b"\\X0\\"


def _encode_step_string_body(text: str) -> bytes:
    """text を STEP 文字列リテラルの中身(外側の `'...'` は含まない)として
    安全なASCIIバイト列に変換する(監督者裁定3。非ASCIIエスケープの分岐と
    バックスラッシュ二重化は2026-07-25レビューで訂正・追加——モジュール
    docstring「非ASCIIエスケープの分岐(修正1)」「バックスラッシュの
    二重化(修正2)」参照)。

    1. ASCII側の変換を先に済ませる(この2規則はどちらもASCII文字同士の
       置換であり、後段で生成する `\\X2\\`/`\\X4\\`/`\\X0\\` という
       エスケープ導入子そのものを誤って二重化する余地がない——導入子は
       この時点ではまだ生成されていないため):
       - `\\`(バックスラッシュ)は `\\\\` に二重化する(STEPのエスケープ
         導入文字そのものであり、未対応だと ifcopenshell がクラッシュする。
         修正2)。
       - `'`(シングルクォート)は `''` に二重化する(STEPの標準エスケープ)。
    2. コードポイント単位でASCII/BMP非ASCII/非BMPの3種に分類し、連続する
       同種のランごとにまとめてエスケープする(異種が隣接すればランを
       切り替える。`\\X2\\...\\X0\\\\X4\\...\\X0\\` のように連続してよい):
       - ASCII(U+0000〜U+007F): エスケープ不要、そのまま出力。
       - BMPの非ASCII(U+0080〜U+FFFF): `_encode_x2_run`。
       - 非BMP(U+10000以上): `_encode_x4_run`(修正1)。
    """
    doubled = text.replace("\\", "\\\\").replace("'", "''")

    parts: list[bytes] = []
    run: list[str] = []
    run_is_non_bmp = False

    def flush() -> None:
        if not run:
            return
        joined = "".join(run)
        parts.append(_encode_x4_run(joined) if run_is_non_bmp else _encode_x2_run(joined))
        run.clear()

    for ch in doubled:
        code_point = ord(ch)
        if code_point < 128:
            flush()
            parts.append(ch.encode("ascii"))
            continue
        is_non_bmp = code_point >= _NON_BMP_THRESHOLD
        if run and is_non_bmp != run_is_non_bmp:
            flush()
        run_is_non_bmp = is_non_bmp
        run.append(ch)
    flush()

    return b"".join(parts)


def _step_string_literal(text: str) -> bytes:
    """text を外側の引用符付きのSTEP文字列リテラル(bytes)に変換する。"""
    return b"'" + _encode_step_string_body(text) + b"'"


def _stamp_header(header: bytes, stamp_lines: tuple[str, str, str]) -> bytes:
    """header(元ファイルの先頭〜最初の `DATA;` まで、verbatim)の
    `FILE_DESCRIPTION` 第1引数(description のタプル)の末尾に stamp_lines
    (3エントリ、STEPエスケープ済みで埋め込む)を追記する(監督者裁定3)。
    既存エントリはバイト単位で温存する。

    括弧の深さ追跡で `FILE_DESCRIPTION` 呼び出し自身の開き括弧(深さ1)と
    description リストの開き括弧(深さ2)を判別し、深さが1に戻る位置
    (=description リストの閉じ括弧)の直前に新エントリを挿入する。

    修正3(Important-4、2026-07-25レビュー): 深さ走査は生の `header` ではなく
    `parser.py` の `_blank_strings`(文字列リテラルの中身を同じ長さの空白に
    置換する、長さを保存する関数)でブランク化した写しの上で行う。既存の
    description エントリの文字列内に釣り合わない丸括弧(例:
    `FILE_DESCRIPTION (('Phase 1)'), '2;1');` の `'Phase 1)'`)があると、
    素朴な括弧深さ追跡はそれを本物の閉じ括弧と誤認し、(a) 既存エントリの
    一部を破壊する、または (b) 深さが最後まで0に戻らず刻印が
    `FILE_DESCRIPTION` 自身の引数として付いてしまいarity違反を起こす——
    いずれも `ifcopenshell.open` は例外を出さずに開けてしまうため無音の
    データ破損になる(実測確認済み。tests/test_textops_rewrite.py の
    `test_header_stamp_preserves_entry_with_unbalanced_{close,open}_paren_in_string`
    参照)。`_blank_strings` は長さを保存するため、ブランク済みの写しで得た
    オフセット(list_open/list_close)はそのまま元の `header` バイト列への
    スライス添字として使える(挿入・保存する内容自体は常に元の `header` から
    切り出すため、文字列の中身がブランクで失われる心配はない——patch.py
    `patch_rel_record` が同じ「ブランク化した写しの上で構造を見て、元バイト
    列に対して操作する」手法を使う前例に倣う)。

    既存の description が空(`()`)の場合は先頭カンマを付けず、1件以上ある
    場合はカンマを1つ付けて追記する(どちらでも STEP のリスト構文として
    妥当な結果になる)。

    `FILE_DESCRIPTION` が見つからない場合は ValueError(有効なSTEPファイルの
    HEADER セクションには必ず存在する mandatory entity)。

    挿入後の刻印3エントリの生存確認について: 本関数は
    `header[:list_close] + insertion + header[list_close:]` という単純な
    バイト列結合で新エントリを組み立てる。Python の bytes 結合自体がこの
    結合を「壊す」余地は無い(挿入するバイト列 `insertion` は必ずそのまま
    結果に現れる)ため、生存を脅かす唯一の要因は `list_close` が本当に
    description リストの閉じ括弧を指しているか、という一点に帰着する。
    その一点は `_blank_strings` の「文字列リテラルの中身を隠す」という
    独立にテストされた契約そのもので保証されるため、本関数内でさらに
    decode-and-compare 式の再検証(_decode_x2_runs 相当のロジックを本
    モジュールに複製する必要が生じ、textopsを軽量に保つ方針に反する)は
    行わない。代わりに、`list_close` がブランク済み写し上で `)` と判定した
    位置が元の `header` バイト列上でも実際に `)` であること(_blank_strings
    の長さ保存契約が破れていないことの安価な整合性チェック)だけを検証する。
    エントリが最終的に元の文字列どおり復元できることの実測証明は
    `tests/test_textops_rewrite.py` の非BMP/クォート/バックスラッシュ/本
    修正の round-trip テスト群(`ifcopenshell.open` で再オープンして
    description を比較する)が担う。
    """
    m = _FILE_DESCRIPTION_RE.search(header)
    if m is None:
        raise ValueError("FILE_DESCRIPTION not found in header; not a valid STEP file")

    # 深さ追跡は _blank_strings でブランク化した写しの上で行う(文字列内の
    # 釣り合わない丸括弧に惑わされないため)。_blank_strings は長さを保存する
    # ので、ここで得るオフセットは元の header バイト列にもそのまま使える。
    blanked = _blank_strings(header)

    call_open = m.end() - 1  # FILE_DESCRIPTION呼び出し自体の開き括弧の位置
    depth = 0
    list_open: int | None = None
    list_close: int | None = None
    i = call_open
    n = len(blanked)
    while i < n:
        c = blanked[i : i + 1]
        if c == b"(":
            depth += 1
            if depth == 2 and list_open is None:
                list_open = i
        elif c == b")":
            if depth == 2 and list_close is None:
                list_close = i
            depth -= 1
            if depth == 0:
                break
        elif c == b";":
            # N1(フェーズ最終レビューの再審): 走査が FILE_DESCRIPTION 文の
            # 外へ出たことの検出。整形式の FILE_DESCRIPTION(...) の括弧内には、
            # 文字列の外に `;`(文の終端子)は現れ得ない。HEADER に未終端の
            # 文字列リテラルがあると `_blank_strings` は文境界を知らないため
            # 次の文の引用符と勝手にペアリングし、深さ追跡が別の文の括弧を
            # 数え始めて**別のエンティティ(FILE_SCHEMA 等)の閉じ括弧で
            # depth==0 に到達**する(結果として刻印3件がそのエンティティへ
            # 紛れ込む)。下の長さ保存チェックは「着地点が元バイト列でも `)`
            # か」しか見ないため、偶然 `)` に着地するこのケースを捕まえられない
            # ——「正しい文の中に留まっているか」はここで fail loud にする
            # (無音で別エンティティを汚染する方がはるかに悪い)。
            raise ValueError(
                "FILE_DESCRIPTION statement is not balanced (statement boundary "
                "crossed before its closing paren); malformed header "
                "(未終端の文字列リテラルがある可能性)"
            )
        i += 1

    if depth != 0:
        # 走査がヘッダ末尾に到達しても depth が 0 に戻らなかった場合
        # (途中で切れたヘッダ等)。上と同じ理由で fail loud にする。
        raise ValueError(
            "FILE_DESCRIPTION statement is not balanced (ran past end of header); "
            "malformed header"
        )
    if list_open is None or list_close is None:
        raise ValueError("FILE_DESCRIPTION has no description list; malformed header")
    if header[list_close : list_close + 1] != b")":
        # 安価な整合性チェック(上のdocstring参照): _blank_stringsが長さ保存
        # 契約を守っていれば、ブランク済み写し上で')'と判定した位置は元の
        # headerバイト列上でも必ず')'であるはず。破れていればここでfail
        # loudにする(無音でdescriptionを壊す方がはるかに悪いため)。
        raise AssertionError(
            "internal invariant violated: _blank_strings did not preserve length "
            "(list_close does not point to ')' in the original header bytes)"
        )

    interior = header[list_open + 1 : list_close]
    encoded_entries = [_step_string_literal(s) for s in stamp_lines]
    if interior.strip() == b"":
        insertion = b",".join(encoded_entries)
    else:
        insertion = b"," + b",".join(encoded_entries)

    return header[:list_close] + insertion + header[list_close:]


def rewrite_without(
    src_path: str | Path,
    out_path: str | Path,
    plan: TextDeletePlan,
    graph: FullGraph,
    source_name: str,
    progress: Callable[[str, int, int], None] | None = None,
) -> RewriteReport:
    """src_path から plan(drop_ids/patch_rel_ids)を適用した出力を out_path に
    ストリーム書き換えで書き出す(ifcopenshell を一度も開かない。モジュール
    docstring参照)。

    progress は ("rewrite", 処理済みレコード数, graph.record_count) で
    レコードごとに(iter_records の1件の yield ごとに)発火する。間引きは
    呼び出し側の責務(brief 契約)。「処理済みレコード数」は壊れたレコードも
    含む累積カウント(= 各時点の records_in)であり、これは
    `graph.record_count`(壊れたレコードを含まない、Task 1の契約)とは
    定義がわずかに異なる——**既知の軽微な限界**: 入力に壊れたレコードが
    存在する場合、進捗表示の「処理済み」が「合計」をわずかに超える形で
    表示され得る(例: 100件中「101/100」)。実際のリライト結果の正しさには
    影響しない(表示上の見た目のみ)。

    契約(M4、フェーズ最終レビュー): `graph` は `scan_full_graph(src_path)`
    で得たもの(=まさにこの `src_path` を対象に走査した `FullGraph`)である
    ことを呼び出し側が保証すること。`plan`(`compute_text_delete_plan` の
    戻り値)の `drop_ids`/`patch_rel_ids` は record id の集合として渡され、
    `graph` 自体は主に `graph.record_count`(progress のtotal)にしか使わない
    ため、`graph` が `src_path` 由来かどうかを本関数は検証しない(record
    規模のコストになる独立な再スキャンが必要になるため——安価な検証手段は
    無い)。`repl.py` は常に同じ呼び出しの中で両方を生成するためこの契約は
    自動的に満たされるが、公開APIとして本関数を直接呼ぶ場合に stale な
    (別のファイルを走査した)`graph` を渡すと、progress の total 表示が
    実際の src_path のレコード数と食い違う、といった無音の誤りが生じ得る
    (drop_ids/patch_rel_ids 自体の適用結果——records_dropped 等——は
    `graph` に依存しないため、直接の破損にはつながらないが、契約違反である
    ことに変わりはない)。

    Raises:
        ValueError: out_path が src_path と同一実体を指す場合(C1: 出力先=
            入力先で原本を無音破壊する事故の防止。出力ファイルを開く前——
            _ensure_sorted_ascending の検証と同じ位置——で検出するため、
            部分出力も残さない)。plan.drop_ids/patch_rel_ids が昇順ソート
            済みでない場合(監督者裁定4)。plan.drop_ids が非空なのに
            src_path から1レコードも読めなかった場合(事後条件、下記参照)。
    """
    if _refers_to_same_file(src_path, out_path):
        raise ValueError(
            f"out_path ({out_path!r}) refers to the same file as src_path "
            f"({src_path!r}); refusing to rewrite in place because opening "
            "out_path for writing would truncate the input before it has been "
            "read (this tool's contract is non-destructive editing of the "
            "original file — choose a different output path)"
        )
    _ensure_sorted_ascending(plan.drop_ids, "drop_ids")
    _ensure_sorted_ascending(plan.patch_rel_ids, "patch_rel_ids")

    deleted_count = int(plan.stats["seeds"]) + int(plan.stats["cascade"])
    stamp_lines = build_provenance_lines(source_name, deleted_count, simplified_count=0)

    header = _read_header(src_path)
    stamped_header = _stamp_header(header, stamp_lines)

    records_in = 0
    records_dropped = 0
    rels_patched = 0
    rels_dropped = 0

    with open(out_path, "wb") as out:
        out.write(stamped_header)
        out.write(b"\n")

        for record in iter_records(src_path):
            records_in += 1

            matched = _match_header(record)
            if matched is None:
                # 壊れたレコード: idが取れないのでdrop判定できない。
                # verbatimで出力し、records_inにも数える(裁定6)。
                out.write(record)
                out.write(b"\n")
            else:
                rec_id = int(matched[0].group(1))
                if _is_member(rec_id, plan.drop_ids):
                    records_dropped += 1
                elif _is_member(rec_id, plan.patch_rel_ids):
                    patched = patch_rel_record(record, plan.drop_ids)
                    if patched is None:
                        rels_dropped += 1
                    else:
                        if patched != record:
                            rels_patched += 1
                        out.write(patched)
                        out.write(b"\n")
                else:
                    out.write(record)
                    out.write(b"\n")

            if progress is not None:
                progress("rewrite", records_in, graph.record_count)

        if plan.drop_ids.size > 0 and records_in == 0:
            # C1 事後条件(保険、監督者裁定5「seeds==0なら書かない」と対になる):
            # 削除するつもりのレコードがあるのに入力から1レコードも読めな
            # かった、という矛盾。src_pathがplan算出時のものと違う(取り違え
            # 等)か、iter_recordsが何らかの理由で空を返した可能性がある——
            # 黙って「削除0件の空DATA」を書いてしまうと最も危険な失敗モード
            # (削除したつもりで中身が別物)になるため、ここでfail loudにする。
            raise ValueError(
                f"plan.drop_ids is non-empty ({plan.drop_ids.size} ids) but 0 "
                f"records were read from src_path ({src_path!r}); refusing to "
                "proceed with a delete plan that could not possibly match "
                "anything in this input (source/plan mismatch?)"
            )

        out.write(b"ENDSEC;\n")
        out.write(b"END-ISO-10303-21;\n")

    bytes_out = Path(out_path).stat().st_size

    return RewriteReport(
        records_in=records_in,
        records_dropped=records_dropped,
        rels_patched=rels_patched,
        rels_dropped=rels_dropped,
        bytes_out=bytes_out,
    )
