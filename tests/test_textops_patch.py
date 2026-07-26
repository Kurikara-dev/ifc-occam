"""textops/patch.py(IFCREL* レコードの参照リストのバイト列パッチ)のTDD
(docs/plans/2026-07-25-cui-phase3.md Task 3)。

`patch_rel_record(record, dead_ids) -> bytes | None` は、record(1件の
`#id=CLASS(...);` 形バイト列。reader.iter_records の戻り値と同型)と
dead_ids(削除確定済み record id 集合。np.ndarray 昇順int64 —
TextDeletePlan.drop_ids と同型 — または frozenset[int])だけを入力に、
参照リストからの dead #id 除去・カンマ正規化・drop判定を行う純粋関数。
FullGraph も TextDeletePlan も ifcopenshell も import しない(record自身の
バイト列と dead_ids だけで完結する)。クラス名(IFCREL* かどうか)も一切
見ない——呼び出し元(Task 4)が既に候補として絞り込んでいる前提。

検証する契約(brief 規則1-5 + 監督者裁定):
  規則1: 文字列リテラル内(''エスケープ含む)は絶対に触らない。
  規則2: 属性トップレベルの括弧リスト内の dead #id を除去し、カンマを
    正規化する。
  規則3: 除去の結果、トップレベル属性の参照リストが空 () になった → None。
  規則4: リスト外(単独属性)の dead 参照が残る → None。
  規則5: dead 参照が無ければ入力をそのまま返す(同一オブジェクト、
    コピーしない)。dead_ids が空の場合も同じ。
  裁定4: `_match_header` が None(壊れたレコード)なら入力をそのまま返す。
  裁定5: 除去は深さ非依存(ネスト括弧でも直接囲むリスト内でカンマ正規化)。
    規則3の空リスト判定は属性トップレベル(depth<=1)のみに適用し、ネスト
    したリスト(depth>=2)が空になっても親からは取り除かず () のまま残す
    (実データのIFCREL*にネスト参照リストは現れないための既知の限界)。
  裁定6: カンマ正規化の具体規則(直後のカンマ+その直後の空白を優先して
    除去、無ければ直前のカンマ+トークン直前の空白を除去)。複数除去が
    隣接・重複しても先頭カンマ・末尾カンマ・`,,` が現れない。
  裁定7: 規則5は同一オブジェクトを返す意味(`is` で確認する)。

加えて、frozenset版とndarray版が同一結果になること、`$`/実データ風の
GUID/OwnerHistory省略混在レコード、レコード内部の改行・空白のverbatim
保持を固定する。
"""

from __future__ import annotations

import numpy as np

from ifc_occam.textops.patch import patch_rel_record


def _dead(*ids: int) -> np.ndarray:
    """dead_ids の ndarray 版(昇順 int64。TextDeletePlan.drop_ids の契約と
    同型)をテストで書きやすくするヘルパー。"""
    return np.array(sorted(ids), dtype=np.int64)


# --- 規則5: dead参照が無い場合は同一オブジェクトを返す(コピーしない) ---


def test_rule5_no_dead_reference_returns_same_object():
    record = b"#5=IFCRELASSOCIATESMATERIAL(#1,#2,#3);"
    result = patch_rel_record(record, _dead(99))
    assert result is record


def test_rule5_empty_dead_ids_ndarray_returns_same_object():
    """裁定7: dead_ids が空(ndarray)の場合も同一オブジェクトを返す
    (早期returnの高速路)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#1,#2,#3);"
    result = patch_rel_record(record, _dead())
    assert result is record


def test_rule5_empty_dead_ids_frozenset_returns_same_object():
    """裁定7: dead_ids が空(frozenset)の場合も同一オブジェクトを返す。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#1,#2,#3);"
    result = patch_rel_record(record, frozenset())
    assert result is record


# --- 裁定4: 壊れたレコード(_match_header が None)は入力をそのまま返す ---


def test_broken_record_missing_header_returns_same_object():
    record = b"not a valid step record"
    result = patch_rel_record(record, _dead(1))
    assert result is record


def test_broken_record_unbalanced_parens_returns_same_object():
    """閉じ括弧が対応しておらず `_match_header` が None を返すケース
    (`stripped.endswith(b")")` が False)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#1,#2;"
    result = patch_rel_record(record, _dead(1))
    assert result is record


def test_broken_record_unmatched_internal_closing_paren_returns_same_object():
    """`_match_header` の外形チェック(先頭ヘッダ+末尾が')'であること)は
    通過するが、body内部の括弧が対応していない壊れた入力
    (`#5=IFCRELASSOCIATESMATERIAL(#1));` → body=`#1)`)に対しては、
    例外を投げず入力をそのまま返す(防御的フォールバック)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#1));"
    result = patch_rel_record(record, _dead(1))
    assert result is record


# --- 規則1: 文字列リテラル内は絶対に触らない ---


def test_rule1_string_literal_hash_lookalike_is_preserved():
    """文字列内の見た目だけの"#2"は、実際の参照#2(dead)がリスト内に別途
    あってパッチが発生しても書き換えられない。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL('AAAA #2 BBBB',(#1,#2,#3));"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL('AAAA #2 BBBB',(#1,#3));"


def test_rule1_doubled_quote_escaped_string_hash_lookalike_is_preserved():
    """`''`(STEPの二重化エスケープ)を含む文字列内の"#9"も同様に保持される
    (parser.py の _blank_strings の '' エスケープ対応をそのまま利用)。"""
    record = b"#7=IFCRELASSOCIATESMATERIAL('It''s a #9 test',(#1,#9,#3));"
    result = patch_rel_record(record, _dead(9))
    assert result == b"#7=IFCRELASSOCIATESMATERIAL('It''s a #9 test',(#1,#3));"


# --- 規則2: 属性トップレベルの括弧リスト内のdead除去+カンマ正規化 ---


def test_rule2_dead_token_removed_from_list_among_other_top_level_attrs():
    """リストは複数属性のうち1つ(他の属性は素の参照#9/#8)。リスト内の
    deadのみ除去され、リスト外の属性(#9,#8)は無関係に保持される。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#9,(#1,#2,#3),#8);"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL(#9,(#1,#3),#8);"


# --- 規則3: 除去の結果、トップレベル属性の参照リストが空()になった → None ---


def test_rule3_list_emptied_by_removal_drops_whole_record():
    record = b"#5=IFCRELASSOCIATESMATERIAL(#9,(#1,#2),#8);"
    result = patch_rel_record(record, _dead(1, 2))
    assert result is None


def test_rule3_single_element_list_emptied_drops_whole_record():
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1));"
    result = patch_rel_record(record, _dead(1))
    assert result is None


# --- 規則4: リスト外(単独属性)のdead参照が残る → None ---


def test_rule4_bare_top_level_attribute_dead_drops_whole_record():
    """リスト外の単独属性(#8)がdead → リストが健全でも全体をdrop。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#9,(#1,#2,#3),#8);"
    result = patch_rel_record(record, _dead(8))
    assert result is None


def test_rule4_bare_attribute_dead_takes_priority_over_list_patch():
    """リスト内にもdead(#2)があり同時に単独属性(#8)もdeadの場合でも
    規則4によりNoneを返す(リストのパッチ計算の結果に関わらず)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#9,(#1,#2,#3),#8);"
    result = patch_rel_record(record, _dead(2, 8))
    assert result is None


# --- 裁定5: ネスト括弧(属性内タプル)。除去は深さ非依存、規則3は
#     属性トップレベル(depth<=1)のみに適用する ---


def test_nested_list_partial_removal_normalizes_commas_within_inner_list_only():
    """ネストしたタプル((#1,#2),(#3,#4))で#2(内側リストの要素)がdead。
    内側リストのみカンマ正規化され、外側リストの要素数(2個)は変わらない。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(((#1,#2),(#3,#4)));"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL(((#1),(#3,#4)));"


def test_nested_list_emptied_by_removal_stays_as_empty_parens_not_dropped():
    """内側リスト(#1,#2)の両方がdeadで空()になっても、規則3は属性トップ
    レベル(外側リスト、depth=1)のみに適用されるため、この()は外側リスト
    から取り除かれず、レコードもdropされない(監督者裁定5: 文書化済みの
    既知の限界。実データのIFCREL*にネスト参照リストは現れないためYAGNI)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(((#1,#2),(#3,#4)));"
    result = patch_rel_record(record, _dead(1, 2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL(((),(#3,#4)));"


def test_deeply_nested_dead_reference_is_still_removed_depth_independent():
    """3階層ネスト(((#5,#6)))でも depth非依存に、直接囲む括弧内で除去・
    カンマ正規化される(裁定5)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((((#5,#6))));"
    result = patch_rel_record(record, _dead(5))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((((#6))));"


# --- 裁定6: カンマ正規化の5パターン(brief/裁定記載のverbatim期待値) ---


def test_comma_normalization_middle_element_no_spaces():
    """(#1,#2,#3) で #2 dead → (#1,#3)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,#2,#3));"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#1,#3));"


def test_comma_normalization_middle_element_with_spaces():
    """(#1, #2, #3) で #2 dead → (#1, #3)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1, #2, #3));"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#1, #3));"


def test_comma_normalization_two_consecutive_interior_elements():
    """(#1,#2,#3,#4) で #2 と #3 dead(隣接除去) → (#1,#4)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,#2,#3,#4));"
    result = patch_rel_record(record, _dead(2, 3))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#1,#4));"


def test_comma_normalization_leading_element():
    """(#2,#3) で #2(先頭) dead → (#3)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#2,#3));"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#3));"


def test_comma_normalization_trailing_element():
    """(#1,#2) で #2(末尾) dead → (#1)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,#2));"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#1));"


# --- カンマ正規化の追加カバレッジ(隣接除去の重複スパン、非隣接複数除去) ---


def test_comma_normalization_all_elements_dead_empties_list_and_drops_record():
    """3要素全滅(除去スパンが隣接・重複するケース)。先頭カンマ・末尾
    カンマ・`,,` が残らず正しく空リストになり、規則3でdropされる。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,#2,#3));"
    result = patch_rel_record(record, _dead(1, 2, 3))
    assert result is None


def test_comma_normalization_two_non_adjacent_dead_elements():
    """(#1,#2,#3,#4,#5) で #2 と #4(隣接しない2箇所) dead → (#1,#3,#5)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,#2,#3,#4,#5));"
    result = patch_rel_record(record, _dead(2, 4))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#1,#3,#5));"


def test_comma_normalization_handles_newline_after_comma():
    """カンマ直後の空白が改行を含む場合も、その空白ぶんまとめて除去する
    (`bytes.isspace()` は `\\n` を含む)。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,\n#2,\n#3));"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#1,\n#3));"


# --- frozenset版とndarray版で同一結果になること ---


def test_frozenset_and_ndarray_dead_ids_produce_identical_patch_result():
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,#2,#3));"
    result_ndarray = patch_rel_record(record, _dead(2))
    result_frozenset = patch_rel_record(record, frozenset({2}))
    assert result_ndarray == result_frozenset == b"#5=IFCRELASSOCIATESMATERIAL((#1,#3));"


def test_frozenset_and_ndarray_dead_ids_produce_identical_drop_result():
    """規則4のdropもndarray/frozensetどちらでも同一(None)になる。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(#1);"
    result_ndarray = patch_rel_record(record, _dead(1))
    result_frozenset = patch_rel_record(record, frozenset({1}))
    assert result_ndarray is None
    assert result_frozenset is None


def test_frozenset_and_ndarray_dead_ids_produce_identical_nested_result():
    """ネスト括弧の除去(裁定5)でも両者は同一結果になる。"""
    record = b"#5=IFCRELASSOCIATESMATERIAL(((#1,#2),(#3,#4)));"
    result_ndarray = patch_rel_record(record, _dead(2))
    result_frozenset = patch_rel_record(record, frozenset({2}))
    expected = b"#5=IFCRELASSOCIATESMATERIAL(((#1),(#3,#4)));"
    assert result_ndarray == result_frozenset == expected


# --- 実データ風: GUID/OwnerHistory省略($)混在レコード ---


def test_realistic_record_with_unset_attributes_is_patched_correctly():
    """GlobalId(GUID文字列)・OwnerHistory省略($)・Name(文字列)・
    Description省略($)・RelatedObjectsリスト・末尾の単独参照、という
    実データ風のIFCRELASSOCIATESMATERIAL形。リスト内のdead参照のみ除去
    され、GUID文字列・$・他の単独参照(#400)は無関係に保持される。"""
    record = (
        b"#500=IFCRELASSOCIATESMATERIAL('1oZ6r_5Rb1a$W4Kx6yqDoP',$,"
        b"'MaterialAssoc',$,(#100,#200,#300),#400);"
    )
    result = patch_rel_record(record, _dead(200))
    assert result == (
        b"#500=IFCRELASSOCIATESMATERIAL('1oZ6r_5Rb1a$W4Kx6yqDoP',$,"
        b"'MaterialAssoc',$,(#100,#300),#400);"
    )


def test_realistic_record_with_derived_star_attribute_is_untouched():
    """`*`(導出属性)が混在していても、リスト内のdead参照のみ除去される
    (`*` はそもそも `#\\d+` にマッチしないため無関係に保持される)。"""
    record = b"#600=IFCRELASSOCIATESMATERIAL(*,(#1,#2,#3),$);"
    result = patch_rel_record(record, _dead(2))
    assert result == b"#600=IFCRELASSOCIATESMATERIAL(*,(#1,#3),$);"


# --- レコード内部の改行・空白の保持(verbatim) ---


def test_internal_newlines_outside_removed_span_are_preserved_verbatim():
    """dead参照の除去対象でない部分の改行・空白はverbatimに保持される
    (Global Constraints: レコード間区切りの改行1つへの正規化のみ許容。
    レコード内部の改行はパッチ対象外の部分では保持される)。"""
    record = (
        b"#5=IFCRELASSOCIATESMATERIAL(\n"
        b"  #9,\n"
        b"  (#1,#2,#3),\n"
        b"  #8\n"
        b");"
    )
    result = patch_rel_record(record, _dead(2))
    assert result == (
        b"#5=IFCRELASSOCIATESMATERIAL(\n"
        b"  #9,\n"
        b"  (#1,#3),\n"
        b"  #8\n"
        b");"
    )


# --- 裁定1: レコード規模の昇順ソート済み ndarray に対する searchsorted +
#     クランプ + 等値ガードの境界値(被覆ロック。Task 3 レビュー M2) ---


def _large_even_dead_ids() -> np.ndarray:
    """2..2,000,000 の偶数だけを持つ昇順ソート済み dead_ids(100万件)。
    奇数は alive、2,000,000 が最大値、それを超える値は範囲外。"""
    return np.arange(2, 2_000_002, 2, dtype=np.int64)


def test_large_sorted_dead_ids_membership_at_boundaries():
    """レコード規模(100万件)の dead_ids で、最小値未満・最大値ちょうど・
    最大値超過・中間のヒット/ミスを1レコード内で同時に判定できる
    (set/dict へ変換せず searchsorted で判定する裁定1の境界値ロック)。"""
    dead = _large_even_dead_ids()
    record = b"#5=IFCRELASSOCIATESMATERIAL((#1,#2,#2000000,#2000001,#999999,#1000000));"
    result = patch_rel_record(record, dead)
    # #1(最小値未満・奇数) #2000001(最大値超過) #999999(奇数) が生存
    assert result == b"#5=IFCRELASSOCIATESMATERIAL((#1,#2000001,#999999));"


def test_large_sorted_dead_ids_above_max_is_not_dead():
    """dead_ids の最大値を超える参照だけを含むレコードは無変更(同一
    オブジェクト)。searchsorted が size を返す位置でのクランプ+等値ガードが
    効いていることのロック(ガードが無ければ IndexError または誤判定)。"""
    dead = _large_even_dead_ids()
    record = b"#5=IFCRELASSOCIATESMATERIAL((#2000002,#3));"
    assert patch_rel_record(record, dead) is record
