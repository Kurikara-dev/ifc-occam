"""STEP (ISO 10303-21) の DATA セクションを、バイト列のままレコード単位で
切り出すストリーマ (cui-design.md §2)。

デコードはしない(GUID・クラス名はASCII。日本語は \\X2\\ エスケープ済みで
ASCII安全なので、そのままバイト列を返せば呼び出し側で困らない)。1文字ずつの
Pythonループは行わず、`bytes.find` で次の区切り(引用符 `'` / セミコロン `;` /
コメント開始 `/*`)へジャンプする状態機械で走査する。

ISO 10303-21 は DATA セクションを複数持つことを許容する。`ENDSEC;` で1つの
DATA セクションが終わっても、そこで打ち切らずファイル終端まで次の `DATA;`
を探し続け、見つかれば続けてレコードを yield する(`iter_records` 参照)。

性能上の要点(実データで踏んだ3つの罠。詳細は各メソッドのdocstring):
1. バッファ切り詰め: 見つかった区切りの直前までを「消費済み」にするのに
   `self._buf = self._buf[pos:]` を毎回行うと、まだ大きい(直近読み込んだ
   チャンク分の)バッファ全体を毎回コピーすることになる。`self._pos` という
   整数カーソルで消費済み位置を追跡し、バッファを物理的に縮めるのは次の
   チャンクを読み込む直前だけに限定する(`_next_record` / `_find_nearest`)。
2. 探索範囲の打ち切り: `'` / `;` / `/*` を毎回バッファ末尾まで独立に探すと、
   実データにまず出現しない `/*` の「見つからない」確認だけで残りバッファ
   全体をスキャンしてしまい、レコード数分繰り返すと実質 O(ファイルサイズ^2)
   になる。各探索を `end=それまでの最良位置` で打ち切る(`_find_nearest`)。
3. バッファ追記のコピーコスト: `self._buf` が(不変の)`bytes` だと、
   `self._buf += chunk` は毎回バッファ全体を新規コピーする。`_next_record`
   の外側ループはチャンク読み込み直前に切り詰めるので影響を受けないが、
   `_scan_string` / `_skip_comment` は文字列・コメントの開始位置を跨いで
   切り詰められないため意図的に切り詰めを行わない(各メソッドのdocstring
   参照)。その結果、1つの文字列/コメントがチャンク境界を多数回またぐと
   `_fill` 呼び出し毎にバッファ全体を再コピーし、O(L^2/chunk_size) になる
   (実測: ペイロード倍化で数倍~数十倍の時間。tests/test_reader.py の
   性能回帰テスト参照)。`self._buf` を `bytearray` にすることで `+=` を
   amortized O(追加分)の追記に変え、切り詰めずに追記し続けても走査全体が
   線形時間に収まるようにしている。
"""

from pathlib import Path
from typing import Iterator

_DATA_MARKER = b"DATA;"
_ENDSEC_KEYWORD = b"ENDSEC"


def iter_records(path: str | Path, chunk_size: int = 8 * 2**20) -> Iterator[bytes]:
    """`DATA;` 以降の `#id=CLASS(...);` レコードを1件ずつ yield する。

    - 文字列リテラル `'...'` の中の `;` や改行は区切りと誤認しない
      (STEP のエスケープは `''` の二重化。バックスラッシュではない)
    - `/* ... */` コメント(文字列の外)は読み飛ばし、返すバイト列にも含めない。
      文字列の中に同じ見た目の並びがあってもコメントとして扱わない(温存する)
    - レコードは複数行にまたがってよい。レコード内部の改行・空白はそのまま
      保持する(レコード先頭の空白・コメントのみ除去して返す)
    - HEADER セクションは読まない(最初の `DATA;` より前は無視する)
    - ISO 10303-21 は DATA セクションを複数持つことを許容する。1つの DATA
      セクションを閉じる `ENDSEC;` に達してもそこで終了せず、ファイル終端
      まで次の `DATA;` を探し続ける。見つかればそこからのレコードも yield
      する(ファイル中に存在する全 DATA セクションを横断する)。以降
      `DATA;` が見つからなければ、そこで終了する

    ファイルが `DATA;` を1つも含まない、または途中で壊れている(文字列や
    コメントが閉じずに EOF に達する)場合は、そこまでに確定した分だけを
    返して黙って打ち切る(例外を投げない)。
    """
    with open(path, "rb") as f:
        scanner = _RecordScanner(f, chunk_size)
        while scanner.skip_past(_DATA_MARKER):
            yield from scanner.iter_records()


class _RecordScanner:
    """チャンク読みしながら引用符・コメントを認識してレコード境界を検出する。

    `self._pos` は self._buf の中で「まだ処理していないデータの開始位置」を
    指す整数カーソル。区切りが見つかった際は self._buf を切り詰めず
    `self._pos` だけを進める(O(1))。バッファの物理的な切り詰めは、次のチャンクを
    読み込む直前(self._pos より前を本当に捨ててよいタイミング)にのみ行う。
    """

    def __init__(self, f, chunk_size: int) -> None:
        self._f = f
        self._chunk_size = chunk_size
        self._buf = bytearray()
        self._pos = 0
        self._eof = False

    def _fill(self) -> bool:
        """次のチャンクを読み buf に追記するだけ(切り詰めはしない)。
        1バイト以上読めたら True、EOF に達していたら False。

        self._buf は bytearray なので `+=` は amortized O(追加分)で追記
        できる(bytes の不変コピーと違い、既存内容を毎回丸ごと再コピー
        しない)。これにより、切り詰めを行わない _scan_string /
        _skip_comment から繰り返し呼ばれても全体で線形時間に収まる。
        """
        if self._eof:
            return False
        chunk = self._f.read(self._chunk_size)
        if not chunk:
            self._eof = True
            return False
        self._buf += chunk
        return True

    def skip_past(self, marker: bytes) -> bool:
        """marker が見つかるまで読み進め、見つかったらその直後から buf を
        再開する。ファイル終端まで見つからなければ False。

        注意: 引用符・コメントを認識しない単純なバイト列検索なので、
        たとえば HEADER 内の文字列リテラルが偶然 `DATA;` という並びを
        含んでいた場合、それを本来の DATA セクション開始と誤認する
        (quote/comment-blind)。

        回帰(修正済み): 検索開始位置は `self._pos`(まだ処理していない
        データの先頭)からでなければならない。かつて `search_from = 0`
        (バッファの絶対先頭)から検索していたため、複数DATAセクション対応の
        `while skip_past(...)` ループ(モジュール関数 `iter_records`)で2回目
        以降に呼ばれた際、まだ物理的に切り詰められていないバッファ内に残る
        「既に yield 済みのレコード本文」(self._pos より手前)が偶然
        `DATA;` という並びを含んでいると、それを本来の(次の)DATAセクション
        開始と誤認してゴミの疑似レコードを生成していた。既定の chunk_size
        (8MiB)では単一の `_fill()` でファイル全体を読み切ってしまいバッファ
        が一度も切り詰められないため、DATAセクションが1つだけのファイルでも
        発生した(tests/test_reader.py の回帰テスト参照)。
        """
        search_from = self._pos
        while True:
            idx = self._buf.find(marker, search_from)
            if idx != -1:
                self._buf = self._buf[idx + len(marker):]
                self._pos = 0
                return True
            # marker がチャンク境界をまたぐ可能性があるので、末尾の
            # (len(marker)-1) バイトだけ次回の再走査対象として残す。
            # self._pos より前(既に消費済み)には戻らないようクランプする
            # (防御的修正。現在の唯一の呼び出し元 iter_records の呼び出し方では
            # self._pos 到達時点で self._buf[self._pos-1] が必ず b";" であり、
            # b";" は marker の先頭4文字 "DATA" のいずれとも一致しないため
            # この区間での誤マッチは理論上到達不能と分析済み。ただし将来
            # _next_record 側のロジック変更でこの前提が崩れる可能性に備えた
            # 防御的多層化として、コストがほぼ無い1行修正を適用する。
            # tests/test_reader.py の
            # test_skip_past_clamp_attempt{1,2,3}_* で再現を試みたが
            # 再現しなかった(un-reproduced)。詳細はテスト側のコメント参照)。
            search_from = max(self._pos, len(self._buf) - (len(marker) - 1))
            if not self._fill():
                return False

    def iter_records(self) -> Iterator[bytes]:
        while True:
            record = self._next_record()
            if record is None:
                return
            yield record

    def _next_record(self) -> bytes | None:
        """次の1レコード(末尾 `;` まで、先頭の空白・コメント除去済み)を返す。
        DATA セクションを閉じる ENDSEC; に達したら None を返す。ファイルが
        壊れていて文字列/コメント/レコードが閉じずに EOF に達した場合も
        None(黙って打ち切る)。空レコード(連続する `;` 等)は読み飛ばして
        次のレコードを探す。
        """
        while True:  # 空レコードをスキップして次を探すためのループ
            pieces: list[bytes] = []

            while True:
                found = self._find_nearest(self._pos)
                if found is None:
                    # '/*' がチャンク境界で分断されている可能性があるので
                    # 末尾1バイトは次回の再走査対象として残す。
                    # ただし self._pos がすでにバッファ末尾まで進んでいる場合
                    # (直前が _scan_string/_skip_comment でちょうどバッファの
                    # 終端まで消費し終えた場合)は、その「最後の1バイト」は
                    # 実際には既に処理済みであり、保留すると同じバイトを
                    # 新しい self._buf の先頭として二重に扱ってしまう。
                    # self._pos より手前には戻さないようにクランプする。
                    safe_upto = len(self._buf)
                    if self._buf.endswith(b"/"):
                        safe_upto = max(safe_upto - 1, self._pos)
                    if safe_upto > self._pos:
                        pieces.append(self._buf[self._pos:safe_upto])
                    # self._pos より前(pieces に転記済み)は不要になったので、
                    # 次のチャンクを読み込む前にここでまとめて捨てる。
                    self._buf = self._buf[safe_upto:]
                    self._pos = 0
                    if not self._fill():
                        return None  # 未終端のレコードで EOF
                    continue

                pos, needle = found
                if needle == b";":
                    pieces.append(self._buf[self._pos:pos + 1])
                    self._pos = pos + 1
                    record = b"".join(pieces).strip()
                    if not record or record == b";":
                        break  # 空レコード。外側ループでやり直す
                    body = record[:-1].strip()  # 末尾の ';' を外して再 strip
                    if body == _ENDSEC_KEYWORD:
                        return None  # DATA セクション終端
                    return record
                elif needle == b"'":
                    pieces.append(self._buf[self._pos:pos])
                    self._pos = pos
                    string_bytes = self._scan_string()
                    if string_bytes is None:
                        return None  # 未終端の文字列で EOF
                    pieces.append(string_bytes)
                else:  # needle == b"/*"
                    pieces.append(self._buf[self._pos:pos])
                    self._pos = pos
                    if not self._skip_comment():
                        return None  # 未終端のコメントで EOF
                    # コメント本体は pieces に加えない(読み飛ばす)

    def _find_nearest(self, start: int) -> tuple[int, bytes] | None:
        """start 以降で最も近い `;` / `'` / `/*` を (位置, トークン) で返す。
        どれも見つからなければ None。

        各 needle の探索は `end=それまでの最良位置` で打ち切る(bytes.find に
        end を渡すだけで O(1) 追加コストなし)。これが無いと、実データには
        まず出現しない `/*` を探すためだけに毎レコード「残りバッファ全体」を
        末尾までスキャンしてしまい(見つからない確認そのものが O(残りサイズ)
        かかる)、レコード数×平均残りサイズ で実質 O(ファイルサイズ^2) になる
        (実測で確認済みの致命的な性能劣化)。`;` は全レコードに必ず1個ある
        ので最初に調べて範囲を早期に絞り込み、以降の探索を安価にする。
        """
        best_pos = -1
        best_needle = b""
        for needle in (b";", b"'", b"/*"):
            end = best_pos if best_pos != -1 else len(self._buf)
            idx = self._buf.find(needle, start, end)
            if idx != -1:
                best_pos = idx
                best_needle = needle
        if best_pos == -1:
            return None
        return best_pos, best_needle

    def _scan_string(self) -> bytearray | None:
        """self._buf[self._pos] が開始の `'` である前提で、対応する終端の
        `'` までを(両端含め、`''` エスケープはそのまま)切り出して返し、
        self._pos をその直後へ進める。未終端で EOF に達したら None。

        戻り値の型注釈は実際の戻り値(self._buf のスライス)に合わせて
        `bytearray | None` にしている(self._buf が bytearray なので、その
        スライスも bytearray であり bytes ではない)。呼び出し元 `_next_record`
        は `pieces`(bytearray/bytes 混在)を最終的に `b"".join(pieces)` で
        結合するため、`bytes.join` は常に `bytes` を返す(呼び出し元の
        オブジェクトの型に従う)ので、公開契約 `iter_records -> Iterator[bytes]`
        はこの中間表現が bytearray であっても保たれる。ここで毎回 bytes に
        変換すると文字列1本ごとに不要なコピーが増えるため、変換はしない。

        文字列の開始位置(self._pos)を跨いでバッファが切り詰められると
        参照が壊れるため、ここでは _fill() のみ使い(切り詰めは行わない)、
        探索中に取得した絶対位置がすべて有効なまま保たれるようにしている。
        切り詰めない分バッファは文字列の長さ分だけ育つが、self._buf が
        bytearray であるため _fill の追記自体は amortized O(追加分)であり、
        文字列がチャンク境界を何度またいでも走査全体は O(文字列長) に収まる
        (self._buf が bytes のままだと `+=` 毎回の全体再コピーで
        O(文字列長^2/chunk_size) になっていた)。
        """
        start = self._pos
        search_from = start + 1
        while True:
            idx = self._buf.find(b"'", search_from)
            if idx == -1:
                search_from = len(self._buf)  # ここまでは ' が無いと確定済み
                if not self._fill():
                    return None  # 未終端の文字列で EOF
                continue
            if idx + 1 >= len(self._buf):
                # '' (エスケープ) かどうかの判定に次の1バイトが要るが未取得。
                if not self._fill():
                    # これ以上読めない = このクォートが本当の終端。
                    result = self._buf[start:idx + 1]
                    self._pos = idx + 1
                    return result
                continue
            if self._buf[idx + 1:idx + 2] == b"'":
                search_from = idx + 2  # '' エスケープ。文字列内に留まる
                continue
            result = self._buf[start:idx + 1]
            self._pos = idx + 1
            return result

    def _skip_comment(self) -> bool:
        """self._buf[self._pos:self._pos+2] が `/*` である前提で、対応する
        `*/` まで読み飛ばし、self._pos をその直後へ進める(コメント本体は
        呼び出し側に返さない)。未終端で EOF なら False。

        _scan_string と同様、ここでも切り詰めは行わない(_fill() のみ)。
        self._buf が bytearray なので、これも O(コメント長) に収まる
        (理由は _scan_string の docstring 参照)。
        """
        search_from = self._pos + 2
        while True:
            idx = self._buf.find(b"*/", search_from)
            if idx == -1:
                safe_upto = len(self._buf)
                if self._buf.endswith(b"*"):
                    safe_upto -= 1
                search_from = safe_upto
                if not self._fill():
                    return False  # 未終端のコメントで EOF
                continue
            self._pos = idx + 2
            return True
