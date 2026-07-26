"""scan/reader.py の合成バイト列TDD(cui-design.md §2)。

iter_records は DATA; 以降の `#id=CLASS(...);` レコードを1件ずつ yield する。
文字列 '...' 内の `;` `改行` は区切りと誤認しない('' は二重化エスケープ)。
/* */ コメント(文字列外)は読み飛ばし、出力から除去する。HEADER セクションと
DATA セクションを閉じる ENDSEC; 以降は読まない。

chunk_size を極小値(16, 64)でパラメトライズし、レコード・文字列・コメントの
境界がチャンク境界をまたぐケースを強制的に発生させる。
"""

import inspect
import re
import time
import tracemalloc

import pytest

from ifc_occam.scan.reader import iter_records


_TINY_CHUNK_SIZES = [16, 64]
_RECORD_ID_LINE = re.compile(rb"^#\d+\s*=", re.MULTILINE)


def _write(tmp_path, content: bytes, name: str = "model.ifc"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _wrap_data(body: bytes) -> bytes:
    """HEADER無しの最小 DATA セクションでラップする(基本テスト用)。"""
    return b"DATA;\n" + body + b"\nENDSEC;\n"


def _wrap_full(body: bytes) -> bytes:
    """HEADER付きの完全な STEP ファイル形でラップする(HEADER無視テスト用)。"""
    return (
        b"ISO-10303-21;\n"
        b"HEADER;\n"
        b"FILE_DESCRIPTION((''),'2;1');\n"
        b"FILE_NAME('','',(''),(''),'','','');\n"
        b"FILE_SCHEMA(('IFC4'));\n"
        b"ENDSEC;\n"
        b"DATA;\n"
        + body
        + b"\nENDSEC;\n"
        b"END-ISO-10303-21;\n"
    )


# --- 1. 基本レコード分割 ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_basic_record_splitting(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCPERSON('A');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCPERSON('A');", b"#2=IFCWALL('B');"]


def test_basic_record_splitting_default_chunk_size(tmp_path):
    """chunk_size を明示しない既定値(8MiB相当)でも動くことを確認する。"""
    content = _wrap_data(b"#1=IFCPERSON('A');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path))
    assert records == [b"#1=IFCPERSON('A');", b"#2=IFCWALL('B');"]


# --- 2. 文字列内の ; と改行 ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_semicolon_inside_string_is_not_a_record_terminator(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCTEXT('a;b;c');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCTEXT('a;b;c');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_newline_inside_string_is_preserved_and_not_a_terminator(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCTEXT('line1\nline2');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCTEXT('line1\nline2');", b"#2=IFCWALL('B');"]


# --- 3. '' エスケープ ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_doubled_quote_escape_stays_inside_string(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCTEXT('it''s here');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCTEXT('it''s here');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_multiple_doubled_quotes_in_one_string(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCTEXT('a''b''c''d');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCTEXT('a''b''c''d');", b"#2=IFCWALL('B');"]


# --- 4. /* */ コメント: 文字列外は読み飛ばし、文字列内は温存 ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_comment_inside_record_is_dropped(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCTEST(/* mid-record comment */'X');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCTEST('X');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_comment_containing_semicolon_and_quote_does_not_confuse_scanner(tmp_path, chunk_size):
    """コメント内の ; や ' は区切り/文字列開始と誤認してはならない。"""
    content = _wrap_data(b"#1=IFCTEST(/* has ; and ' inside */'X');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCTEST('X');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_comment_between_records_is_dropped(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCWALL('A');\n/* separator comment */\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('A');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_comment_lookalike_inside_string_is_preserved_not_treated_as_comment(tmp_path, chunk_size):
    """文字列内の `/* ... */` はコメントではなく文字列データとして温存する。"""
    content = _wrap_data(b"#1=IFCTEST('/*not a comment*/');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCTEST('/*not a comment*/');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", range(1, 21))
def test_comment_with_nested_lookalike_ending_at_buffer_boundary_regression(tmp_path, chunk_size):
    """回帰テスト: コメント終端 `*/` の直後で self._pos がバッファ末尾に
    ちょうど届いたとき、境界越え保留ロジック(末尾の孤立 `/` の保留)が
    self._pos より手前の(既に消費済みの)バイトを新バッファに復活させ、
    末尾 `/` を1つ多く出力してしまう不具合があった(chunk_size 1-3 で再現)。
    コメント内部に `/*` に見える並びを含めて `/*` 検出を1回で終わらせず、
    "*/" 直前まで確実に引き延ばす。chunk_size 1-20 を総当たりする。
    """
    content = _wrap_data(b"/* a /* b */\n#3=IFCX();")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#3=IFCX();"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_lone_slash_not_followed_by_star_is_kept(tmp_path, chunk_size):
    """`/` 単体(コメント開始でない)は正しく通常内容として扱われる。"""
    record = b"#1=IFCTEST(1/2);"
    content = _wrap_data(record)
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [record]


def test_data_marker_split_exactly_across_chunk_boundary(tmp_path):
    """`DATA;` トークン自体がチャンク境界で分断されても正しく検出できる。"""
    filler = b"X" * 14  # "DATA;" の開始位置を14にし、chunk_size=16 の境界(16)で分断させる
    content = filler + b"DATA;\n#1=IFCWALL('B');\nENDSEC;\n"
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=16))
    assert records == [b"#1=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_long_string_spanning_many_tiny_chunks(tmp_path, chunk_size):
    long_value = b"x" * 100
    record = b"#1=IFCTEXT('" + long_value + b"');"
    content = _wrap_data(record)
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [record]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_long_comment_spanning_many_tiny_chunks_is_dropped(tmp_path, chunk_size):
    long_comment = b"/* " + b"y" * 100 + b" */"
    record_with_comment = b"#1=IFCTEST(" + long_comment + b"'X');"
    record_expected = b"#1=IFCTEST('X');"
    content = _wrap_data(record_with_comment)
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [record_expected]


# --- 5. 複数行レコード(改行・空白を保持) ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_multiline_record_preserves_internal_whitespace(tmp_path, chunk_size):
    record = b"#1=IFCPOLYLOOP((\n  #2,\n  #3,\n  #4\n));"
    content = _wrap_data(record + b"\n#5=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [record, b"#5=IFCWALL('B');"]


# --- 6. HEADER セクションを yield しない ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_header_section_is_not_yielded(tmp_path, chunk_size):
    content = _wrap_full(b"#1=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('B');"]


# --- 7. ENDSEC 以降を読まない ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_endsec_marker_itself_is_not_yielded(tmp_path, chunk_size):
    content = _wrap_data(b"#1=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_content_after_data_section_endsec_is_never_scanned(tmp_path, chunk_size):
    """DATA を閉じる ENDSEC; の後に有効なレコードらしきものがあっても無視する。"""
    content = _wrap_full(b"#1=IFCWALL('B');") + b"#2=IFCSHOULDNOTAPPEAR('bogus');\n"
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('B');"]


# --- 組み合わせ(回帰確認用) ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_realistic_mixed_sequence(tmp_path, chunk_size):
    body = (
        b"#1=IFCCARTESIANPOINT((0.,0.,0.));\n"
        b"#2=IFCTEXT('a;b''c/*not-comment*/d');\n"
        b"/* comment between records */\n"
        b"#3=IFCPOLYLOOP((\n  #1,\n  #2\n));"
    )
    content = _wrap_data(body)
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [
        b"#1=IFCCARTESIANPOINT((0.,0.,0.));",
        b"#2=IFCTEXT('a;b''c/*not-comment*/d');",
        b"#3=IFCPOLYLOOP((\n  #1,\n  #2\n));",
    ]


# --- 空・欠損ケース ---


def test_empty_data_section_yields_nothing(tmp_path):
    content = _wrap_data(b"")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=16))
    assert records == []


def test_missing_data_section_yields_nothing(tmp_path):
    """DATA; が存在しないファイルは例外を出さず空を返す。"""
    path = _write(tmp_path, b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n")
    records = list(iter_records(path, chunk_size=16))
    assert records == []


def test_iter_records_returns_generator_without_opening_file_eagerly(tmp_path):
    """iter_records() の呼び出し自体はファイルを開かない
    (呼び出し時点では存在しないパスでも例外にならず、最初の next() で開く)。"""
    missing = tmp_path / "does-not-exist.ifc"
    gen = iter_records(missing)
    assert inspect.isgenerator(gen)
    with pytest.raises(FileNotFoundError):
        next(gen)


# --- 複数 DATA セクション(ISO 10303-21 は複数持てる) ---


@pytest.mark.parametrize("chunk_size", _TINY_CHUNK_SIZES)
def test_multiple_data_sections_yield_records_from_all_sections(tmp_path, chunk_size):
    """ISO 10303-21 は DATA セクションを複数許容する。1つ目の ENDSEC; の後に
    2つ目の DATA; が現れたら、そこから先のレコードも yield する
    (単一DATAセクションのみ読む実装は2つ目を黙って落としてしまう)。"""
    content = (
        b"DATA;\n#1=IFCWALL('A');\nENDSEC;\n"
        b"DATA;\n#2=IFCWALL('B');\nENDSEC;\n"
    )
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('A');", b"#2=IFCWALL('B');"]


def test_three_data_sections_yield_records_from_all_sections(tmp_path):
    """3セクション以上でも次の DATA; の再探索が正しく繰り返されることを
    確認する。"""
    content = (
        b"DATA;\n#1=IFCWALL('A');\nENDSEC;\n"
        b"DATA;\n#2=IFCWALL('B');\nENDSEC;\n"
        b"DATA;\n#3=IFCWALL('C');\nENDSEC;\n"
    )
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=16))
    assert records == [
        b"#1=IFCWALL('A');",
        b"#2=IFCWALL('B');",
        b"#3=IFCWALL('C');",
    ]


def test_second_data_section_preceded_by_unrelated_content_is_still_found(tmp_path):
    """2つ目の DATA; の前に無関係な内容(他のセクションらしきもの)があっても
    読み飛ばして2つ目の DATA セクションを見つけられる。"""
    content = (
        b"DATA;\n#1=IFCWALL('A');\nENDSEC;\n"
        b"SOME-OTHER-SECTION;\nJUNK;\nENDSEC;\n"
        b"DATA;\n#2=IFCWALL('B');\nENDSEC;\n"
    )
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=16))
    assert records == [b"#1=IFCWALL('A');", b"#2=IFCWALL('B');"]


def test_single_data_section_followed_by_eof_is_unaffected(tmp_path):
    """回帰確認: DATA セクションが1つだけでそのまま EOF に達する既存動作は
    変わらない(ENDSEC; の後に次の DATA; を再探索しても EOF で安全に
    諦められる)。"""
    content = _wrap_data(b"#1=IFCWALL('A');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=16))
    assert records == [b"#1=IFCWALL('A');"]


# --- 回帰: skip_past は self._pos から検索しなければならない ---
#
# skip_past が search_from=0 (バッファの絶対先頭)から検索していたため、複数
# DATA セクション対応の `while skip_past(...)` ループ(iter_records)で2回目
# 以降に呼ばれた際、まだ物理的に切り詰められていないバッファ内に残る「既に
# yield 済みのレコード本文」が偶然 `DATA;` という並びを含んでいると、それを
# 本来の(2つ目の)DATAセクション開始と誤認してゴミの疑似レコードを生成して
# しまう不具合があった。既定の chunk_size (8MiB) では単一の _fill() でファイル
# 全体を読み切ってしまいバッファが一度も切り詰められないため、単一DATA
# セクションのファイルでも発生する(小さい chunk_size でも、文字列走査中は
# _scan_string が意図的に切り詰めを行わないため再現しうる)。

_SKIP_PAST_REGRESSION_CHUNK_SIZES = [16, 64, 8 * 2**20]


@pytest.mark.parametrize("chunk_size", _SKIP_PAST_REGRESSION_CHUNK_SIZES)
def test_skip_past_ignores_data_marker_inside_already_yielded_record(tmp_path, chunk_size):
    """単一DATAセクション。レコードの文字列ペイロードに `DATA;` が2回出現しても
    2つ目のDATAセクション開始と誤認せず、ちょうど2件だけ(ゴミなしで)yield する。"""
    content = _wrap_data(b"#1=IFCTEXT('DATA; appears DATA; twice');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [
        b"#1=IFCTEXT('DATA; appears DATA; twice');",
        b"#2=IFCWALL('B');",
    ]


@pytest.mark.parametrize("chunk_size", _SKIP_PAST_REGRESSION_CHUNK_SIZES)
def test_skip_past_finds_real_second_data_section_past_data_marker_in_string(tmp_path, chunk_size):
    """2つの実DATAセクション。1つ目のセクション内レコードの文字列に `DATA;` が
    含まれていても、それを飛び越えて本当の2つ目の `DATA;` を見つけ、両セクションの
    レコードだけを(ゴミなしで)yield する。"""
    content = (
        b"DATA;\n#1=IFCTEXT('DATA; appears DATA; twice');\nENDSEC;\n"
        b"DATA;\n#2=IFCWALL('B');\nENDSEC;\n"
    )
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [
        b"#1=IFCTEXT('DATA; appears DATA; twice');",
        b"#2=IFCWALL('B');",
    ]


@pytest.mark.parametrize("chunk_size", _SKIP_PAST_REGRESSION_CHUNK_SIZES)
def test_skip_past_realistic_metadata_description_does_not_yield_garbage(tmp_path, chunk_size):
    """実際に踏んだケース: 'See METADATA; and DATA; fields' のような説明文
    ペイロード(METADATA; 自体にも部分文字列として DATA; を含む)があっても
    ゴミレコードを生成しない。"""
    content = _wrap_data(b"#1=IFCTEXT('See METADATA; and DATA; fields');\n#2=IFCWALL('B');")
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [
        b"#1=IFCTEXT('See METADATA; and DATA; fields');",
        b"#2=IFCWALL('B');",
    ]


# --- 未再現の残懸念: skip_past の再走査開始位置クランプ(防御的修正) ---
#
# docs/plans/2026-07-24-cui-phase1.md Task 1 の残懸念: `search_from = max(0, len(self._buf) - (len(marker)
# - 1))` が `self._pos` でクランプされていない。理論上、次の `_fill()` 後の再探索が
# `self._pos` より最大 `len(marker)-2` バイト手前から始まる余地がある。
#
# 3パターンで RED 化を試みたが、いずれも再現しなかった(以下は再現しないことの
# 証明として残す回帰ガード)。理由: `skip_past` の唯一の呼び出し元
# (`iter_records` の `while scanner.skip_past(...)` ループ)では、呼び出し時点の
# `self._pos` は必ず (a) 初回呼び出しで `self._buf` が空(`self._pos == 0`)、
# または (b) 直前の `_next_record()` が `ENDSEC` 検出で `None` を返した直後
# (`self._pos` は消費済みの `;` の直後を指す = `self._buf[self._pos-1] == b";"`
# が常に成立)、のいずれかに限られる。(b) の場合、たとえ `search_from` が
# `self._pos` より最大4バイト(`len(b"DATA;")-1`)手前になっても、その手前区間の
# 末尾バイトは常に `;` であり、マーカー `DATA;` の先頭4文字 `D`/`A`/`T`/`A` の
# いずれとも一致しない(`;` は `DATA;` の最後の1文字としてしか現れない)ため、
# その区間を起点とする誤マッチは幾何的に成立しない。(EOF 経路は `_fill()` が
# 即座に False を返すため、誤った `search_from` は再利用される前に関数が
# 抜ける。) 以上より現在の呼び出しグラフでは到達不能と判断するが、将来
# `_next_record` の ENDSEC 検出ロジックが変わればこの前提は崩れうるため、
# クランプ自体は防御的に適用する(1行修正、コスト無視できる)。
_UNCLAMPED_STRESS_CHUNK_SIZES = list(range(1, 25))


@pytest.mark.parametrize("chunk_size", _UNCLAMPED_STRESS_CHUNK_SIZES)
def test_skip_past_clamp_attempt1_decoy_immediately_after_endsec(tmp_path, chunk_size):
    """試行1: 1つ目の ENDSEC; の直後(=self._pos)に `DATA`(セミコロンなし)の
    デコイを隙間なく置き、続けて本物の2つ目の DATA; を置く。全 chunk_size で
    ゴミなし・2件のみ yield されることを期待する(再現しなかった)。"""
    content = (
        b"DATA;\n#1=IFCWALL('A');\nENDSEC;"
        b"DATA_DECOY_NOT_REAL"
        b"\nDATA;\n#2=IFCWALL('B');\nENDSEC;\n"
    )
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('A');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _UNCLAMPED_STRESS_CHUNK_SIZES)
def test_skip_past_clamp_attempt2_endsec_padded_with_whitespace(tmp_path, chunk_size):
    """試行2: ENDSEC とその `;` の間に空白を挟み(スキャナは body.strip() で
    判定するため許容される)、`self._pos` 直前の4バイトの内容を変えてみる。
    それでも `self._pos-1` は常に `;` なので誤マッチの余地はないはず。"""
    content = (
        b"DATA;\n#1=IFCWALL('A');\nENDSEC   ;"
        b"\nDATA;\n#2=IFCWALL('B');\nENDSEC;\n"
    )
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('A');", b"#2=IFCWALL('B');"]


@pytest.mark.parametrize("chunk_size", _UNCLAMPED_STRESS_CHUNK_SIZES)
def test_skip_past_clamp_attempt3_only_decoy_no_real_second_section(tmp_path, chunk_size):
    """試行3: 2つ目の本物の DATA; を置かず、デコイ `DATA`(セミコロンなし)だけを
    ENDSEC; の直後に置く。誤マッチが起きれば余分なゴミレコードが生成されるはず
    だが、期待は「1件だけ、ゴミなし」。"""
    content = b"DATA;\n#1=IFCWALL('A');\nENDSEC;DATA_NO_SEMICOLON_HERE\n"
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert records == [b"#1=IFCWALL('A');"]


# --- 型契約: yield されるレコードは bytes であること(bytearray ではない) ---


@pytest.mark.parametrize("chunk_size", [16, 8 * 2**20])
def test_yielded_records_are_bytes_not_bytearray(tmp_path, chunk_size):
    """self._buf が bytearray であっても、公開契約 Iterator[bytes] は保たれる
    (b"".join(pieces) の呼び出し元が bytes なので常に bytes を返す)。bytes と
    bytearray は別系統の型なので isinstance(record, bytes) は bytearray に対し
    False になる(サブクラス関係ではない)。"""
    content = _wrap_data(
        b"#1=IFCPERSON('A');\n#2=IFCTEXT('mid record string');\n#3=IFCWALL('B');"
    )
    path = _write(tmp_path, content)
    records = list(iter_records(path, chunk_size=chunk_size))
    assert len(records) == 3
    for record in records:
        assert isinstance(record, bytes)
        assert not isinstance(record, bytearray)


# --- 性能回帰: 文字列/コメント走査の時間が O(L) であること ---
#
# _scan_string / _skip_comment は _fill() が self._buf に追記するたびバッファ
# 全体を再コピーする実装(bytesの不変コピー)だと、1つの文字列/コメントが
# チャンク境界を多数回またぐケースで O(L^2/chunk_size) になる(実測: 修正前は
# ペイロード倍化で18~41倍の時間。外側ループ(引用符・コメントを含まないプレーン
# な内容)は既に O(L) なので対照群としては1.3倍程度で収まる)。ペイロードを
# 倍にしても実行時間が4倍未満(線形+ノイズ余裕)に収まることを確認する。

_PERF_CHUNK_SIZE = 64
_PERF_SIZE_L = 400 * 1024
_PERF_SIZE_2L = 2 * _PERF_SIZE_L
_PERF_RATIO_CEILING = 4.0


def _elapsed_scanning(path, chunk_size: int) -> float:
    start = time.perf_counter()
    for _ in iter_records(path, chunk_size=chunk_size):
        pass
    return time.perf_counter() - start


def _assert_scan_time_scales_linearly(tmp_path, make_body, label: str):
    path_l = _write(tmp_path, _wrap_data(make_body(_PERF_SIZE_L)), name="l.ifc")
    path_2l = _write(tmp_path, _wrap_data(make_body(_PERF_SIZE_2L)), name="2l.ifc")

    elapsed_l = _elapsed_scanning(path_l, _PERF_CHUNK_SIZE)
    elapsed_2l = _elapsed_scanning(path_2l, _PERF_CHUNK_SIZE)

    ratio = elapsed_2l / elapsed_l if elapsed_l > 0 else float("inf")
    assert ratio < _PERF_RATIO_CEILING, (
        f"{label}: ペイロード倍化で{ratio:.1f}倍の時間(期待: 線形なので"
        f"{_PERF_RATIO_CEILING:.0f}倍未満)。L={elapsed_l:.3f}s 2L={elapsed_2l:.3f}s"
    )


def test_in_string_scan_time_scales_linearly_with_payload_size(tmp_path):
    """Finding 1: 1つの巨大な文字列リテラルがチャンク境界を多数回またいでも
    _scan_string の時間が線形に収まることを確認する回帰テスト。"""
    _assert_scan_time_scales_linearly(
        tmp_path,
        lambda n: b"#1=IFCTEXT('" + b"x" * n + b"');",
        "in_string",
    )


def test_in_comment_scan_time_scales_linearly_with_payload_size(tmp_path):
    """Finding 1: 1つの巨大なコメントがチャンク境界を多数回またいでも
    _skip_comment の時間が線形に収まることを確認する回帰テスト。"""
    _assert_scan_time_scales_linearly(
        tmp_path,
        lambda n: b"/* " + b"y" * n + b" */\n#1=IFCWALL('B');",
        "in_comment",
    )


def test_plain_content_scan_time_scales_linearly_control(tmp_path):
    """対照群: 引用符・コメントを含まないプレーンな内容は元から線形
    (外側ループはチャンク読み込み直前に既にトリミングしている)。修正の
    前後どちらでも通るはずのコントロール。"""
    _assert_scan_time_scales_linearly(
        tmp_path,
        lambda n: b"#1=IFCTEST(" + b"1" * n + b");",
        "plain_control",
    )


# --- メモリ回帰: バッファがファイルサイズに比例して肥大しないこと ---


def test_iter_records_peak_memory_stays_bounded_by_chunk_size(tmp_path):
    """Finding 3: ファイル全体を(意図せず)バッファし続ける退行を検知する。
    chunk_size=64KiB で ~5MB の合成ファイル(引用符・コメントを含まない
    小さなレコードの繰り返し)を走査しても、ピークトレース済みメモリが
    5*chunk_size + 1MB(余裕)を超えないことを確認する。フルバッファ化
    (chunk_size に関わらずファイルサイズに比例するメモリ使用)は確実に
    検知できるが、非フレーキーであるよう緩めに取った境界。"""
    chunk_size = 64 * 1024
    target_size = 5 * 1024 * 1024
    record = b"#1=IFCWALL('x');\n"
    body = record * (target_size // len(record) + 1)
    content = _wrap_data(body)
    path = _write(tmp_path, content)

    tracemalloc.start()
    try:
        count = 0
        for _ in iter_records(path, chunk_size=chunk_size):
            count += 1
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    bound = 5 * chunk_size + 1024 * 1024
    assert count > 0
    assert peak < bound, f"peak={peak} bound={bound}(フルバッファ化の疑いあり)"


# --- 統合テスト(実データ: small.ifc / large.ifc) ---


def test_small_ifc_record_count_matches_grep_of_record_ids(small_ifc_path):
    """small.ifc の総レコード数が、`#\\d+=` 形のレコードID行の grep 数と一致する。

    実データは `#1 = IFCCOLOURRGB(...)` のように id と `=` の間に空白を含むため、
    素の `#\\d+=` ではなく行頭アンカー付き `^#\\d+\\s*=` (MULTILINE) で数える
    (文字列内の偶然の類似文字列を拾わないよう行頭アンカーで安全側にしている)。
    """
    raw = small_ifc_path.read_bytes()
    expected_count = len(_RECORD_ID_LINE.findall(raw))
    assert expected_count > 0

    records = list(iter_records(small_ifc_path))

    assert len(records) == expected_count


def test_large_ifc_scan_throughput(large_ifc_path):
    """large.ifc で走査速度を実測し MB/s を報告する(目標 >=30MB/s, cui-design.md §2/§7)。

    未達でも実測値を記録して先へ進む方針(最適化判断は監督者/Task 8)なので、
    ここでは壊滅的な性能劣化のみ検知する緩いフロア(>1MB/s)だけを assert する。
    """
    file_size = large_ifc_path.stat().st_size

    start = time.perf_counter()
    count = 0
    for _ in iter_records(large_ifc_path):
        count += 1
    elapsed = time.perf_counter() - start

    size_mb = file_size / (1024 * 1024)
    mb_per_sec = size_mb / elapsed if elapsed > 0 else float("inf")
    print(
        f"\n[test_large_ifc_scan_throughput] size={size_mb:.1f}MB records={count} "
        f"elapsed={elapsed:.2f}s throughput={mb_per_sec:.1f}MB/s (target >=30MB/s)"
    )

    assert count > 0
    assert mb_per_sec > 1, "壊滅的な性能劣化(1MB/s未満)を検知"
