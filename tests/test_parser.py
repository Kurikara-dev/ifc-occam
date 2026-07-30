"""scan/parser.py の合成レコードTDD(cui-design.md §3)。

parse_record(record: bytes) -> ScanEntity | None は、reader.iter_records が
yield する1レコード(`#id=CLASS(...);` 形、空白保持済み)を解釈し、クラス名を
3分類(フロンティア/ブロック/中間)して重み・参照・GUID・Name を抽出する。

- フロンティア: IFCFACE系(weight=1)/ IFCTRIANGULATEDFACESET(CoordIndexの
  `),(` 個数+1) / IFCPOLYGONALFACESET(Facesリストの要素数) /
  パラメトリック立体6種(weight=PARAMETRIC_NOMINAL_TRIS, is_parametric=True)。
  いずれも refs は空(グラフを軽くする)。
- ブロック: 点・方向・配置・ループ・境界・エッジ・頂点・スタイル・単位・
  OwnerHistory・プロパティ/数量セット類。weight=0, refs=空。
- 中間: 上記以外すべて。refs を格納(文字列内の `#123` は参照と誤認しない)。
- GUID/Name: 第1属性が22文字の `[0-9A-Za-z_$]` 文字列なら global_id として
  採録し、その場合のみ第3属性を Name として抽出(\\X2\\ エスケープはデコード)。

CoordIndex=属性index3 / Faces=属性index2 という位置は、ifcopenshell
0.8.5 のスキーマ定義(IFC4 / IFC4X3 双方)を実際に問い合わせて検証済み
(IFC4 と IFC4X3 で Closed/Normals の宣言順が入れ替わっているが、CoordIndex
は両方とも index3、Faces は両方とも index2 で不変)。
\\X2\\ デコードのアルゴリズムは、ifcopenshell が実際の \\X2\\ エスケープ
(IFCBUILDINGELEMENTPROXY の Name/Description相当)を復号した結果と一致する
ことを事前に確認済み(以降のテストで使うGUID・バイト列・復号後の文字列は
合成値。実データの形だけを模している)。
"""

import re
import time
from collections import Counter

import pytest

from ifc_occam.scan.parser import (
    PARAMETRIC_NOMINAL_TRIS,
    ScanEntity,
    _BLOCK_EXACT,
    _BLOCK_PREFIXES,
    _FRONTIER_ALL,
    parse_record,
)
from ifc_occam.scan.reader import iter_records


# --- 1. 基本のid/クラス名抽出 ---


def test_basic_intermediate_record_extracts_id_and_class():
    e = parse_record(b"#42=IFCWALLTYPE(#1,#2,'Name',$,$,$,$,$,$);")
    assert e is not None
    assert e.entity_id == 42
    assert e.ifc_class == "IFCWALLTYPE"


def test_class_name_with_surrounding_spaces_is_recognized():
    """実データは `#1 = IFCCOLOURRGB(...)` のように id/クラス名の前後に
    空白を含むことがある(reader.py のテストと同じ実データ観察に基づく)。"""
    e = parse_record(b"#1 = IFCWALL (#2,#3);")
    assert e is not None
    assert e.entity_id == 1
    assert e.ifc_class == "IFCWALL"


def test_class_name_is_uppercased_even_if_lowercase_in_source():
    """STEPのキーワードは大文字小文字を区別しない。"""
    e = parse_record(b"#1=ifcwall(#2,#3);")
    assert e is not None
    assert e.ifc_class == "IFCWALL"


def test_malformed_record_without_hash_returns_none():
    assert parse_record(b"garbage no id here") is None


def test_malformed_record_with_unbalanced_parens_returns_none():
    assert parse_record(b"#1=IFCWALL(#2,#3") is None


def test_returns_none_for_empty_bytes():
    assert parse_record(b"") is None


# --- 2. フロンティア: 面クラス(weight=1) ---


@pytest.mark.parametrize("cls", ["IFCFACE", "IFCFACESURFACE", "IFCADVANCEDFACE"])
def test_face_frontier_classes_get_weight_one(cls):
    record = f"#1={cls}(#2,.T.);".encode()
    e = parse_record(record)
    assert e.weight == 1
    assert e.refs == ()
    assert e.is_parametric is False


# --- 3. フロンティア: IFCTRIANGULATEDFACESET(CoordIndexの `),(` カウント) ---


def test_triangulated_faceset_single_triangle_weight_is_one():
    record = b"#1=IFCTRIANGULATEDFACESET(#2,$,.F.,((1,2,3)),$);"
    e = parse_record(record)
    assert e.weight == 1
    assert e.refs == ()
    assert e.is_parametric is False


def test_triangulated_faceset_multiple_triangles_counted_via_close_open():
    record = b"#1=IFCTRIANGULATEDFACESET(#2,$,.F.,((1,2,3),(4,5,6),(7,8,9)),$);"
    e = parse_record(record)
    assert e.weight == 3


def test_triangulated_faceset_ignores_normals_list_when_counting():
    """Normals(2番目の属性)にも入れ子タプルが3個あるが、CoordIndex(4番目の
    属性、0-indexで3)だけを数える。全体を無差別にカウントすると誤って
    Normalsの分も加算してしまう罠(ifcopenshellのスキーマ定義で確認済み:
    Coordinates, Normals, Closed, CoordIndex, PnIndex の順)。"""
    record = (
        b"#1=IFCTRIANGULATEDFACESET(#2,"
        b"((0.,0.,1.),(0.,0.,1.),(0.,0.,1.)),"
        b".F.,"
        b"((1,2,3),(4,5,6)),"
        b"$);"
    )
    e = parse_record(record)
    assert e.weight == 2


def test_triangulated_faceset_empty_coordindex_is_zero():
    record = b"#1=IFCTRIANGULATEDFACESET(#2,$,.F.,(),$);"
    e = parse_record(record)
    assert e.weight == 0


def test_triangulated_faceset_missing_coordindex_attribute_is_zero():
    """属性数が想定より少ない壊れたレコードでも例外を出さず0にする。"""
    record = b"#1=IFCTRIANGULATEDFACESET(#2);"
    e = parse_record(record)
    assert e.weight == 0


# --- 4. フロンティア: IFCPOLYGONALFACESET(Facesリストの要素数) ---


def test_polygonal_faceset_weight_is_faces_ref_count():
    record = b"#1=IFCPOLYGONALFACESET(#2,$,(#10,#11,#12),$);"
    e = parse_record(record)
    assert e.weight == 3
    assert e.refs == ()


def test_polygonal_faceset_single_face():
    record = b"#1=IFCPOLYGONALFACESET(#2,$,(#10),$);"
    e = parse_record(record)
    assert e.weight == 1


def test_polygonal_faceset_empty_faces_is_zero():
    record = b"#1=IFCPOLYGONALFACESET(#2,$,(),$);"
    e = parse_record(record)
    assert e.weight == 0


# --- 5. フロンティア: パラメトリック立体(名目値 PARAMETRIC_NOMINAL_TRIS) ---


@pytest.mark.parametrize(
    "cls",
    [
        "IFCEXTRUDEDAREASOLID",
        "IFCREVOLVEDAREASOLID",
        "IFCSWEPTDISKSOLID",
        "IFCBOOLEANRESULT",
        "IFCBOOLEANCLIPPINGRESULT",
        "IFCCSGSOLID",
    ],
)
def test_parametric_solids_get_nominal_weight_and_flag(cls):
    record = f"#1={cls}(#2,#3,0.);".encode()
    e = parse_record(record)
    assert e.weight == PARAMETRIC_NOMINAL_TRIS
    assert e.is_parametric is True
    assert e.refs == ()


def test_parametric_nominal_tris_constant_is_16():
    assert PARAMETRIC_NOMINAL_TRIS == 16


# --- 6. ブロック: 重みなし・refs格納なし ---


@pytest.mark.parametrize(
    "cls,body",
    [
        ("IFCCARTESIANPOINT", b"(0.,0.,0.)"),
        ("IFCDIRECTION", b"((0.,0.,1.))"),
        ("IFCAXIS1PLACEMENT", b"(#2,#3)"),
        ("IFCAXIS2PLACEMENT2D", b"(#2,#3)"),
        ("IFCAXIS2PLACEMENT3D", b"(#2,#3,#4)"),
        ("IFCLOCALPLACEMENT", b"(#2,#3)"),
        ("IFCGRIDPLACEMENT", b"(#2,#3)"),
        ("IFCPOLYLOOP", b"(#2,#3,#4)"),
        ("IFCEDGELOOP", b"(#2,#3)"),
        ("IFCVERTEXLOOP", b"(#2)"),
        ("IFCFACEBOUND", b"(#2,.T.)"),
        ("IFCFACEOUTERBOUND", b"(#2,.T.)"),
        ("IFCEDGE", b"(#2,#3)"),
        ("IFCORIENTEDEDGE", b"(*,*,#2,.T.)"),
        ("IFCEDGECURVE", b"(#2,#3,#4,.T.)"),
        ("IFCVERTEXPOINT", b"(#2)"),
        ("IFCSURFACESTYLE", b"('x',.NOTDEFINED.,(#2))"),
        ("IFCSURFACESTYLERENDERING", b"(#2,0.,#3,$,$,$,$,$,.NOTDEFINED.)"),
        ("IFCCURVESTYLE", b"('x',$,$,$,$)"),
        ("IFCFILLAREASTYLE", b"('x',(#2),$)"),
        ("IFCTEXTSTYLE", b"('x',$,$,$,$)"),
        ("IFCCOLOURRGB", b"($,0.,0.,0.)"),
        ("IFCCOLOURRGBLIST", b"(((0.,0.,0.),(1.,1.,1.)))"),
        ("IFCSIUNIT", b"(*,.LENGTHUNIT.,$,.METRE.)"),
        ("IFCCONVERSIONBASEDUNIT", b"(#2,.LENGTHUNIT.,'x',#3)"),
        ("IFCDERIVEDUNIT", b"((#2),.MASSDENSITYUNIT.,$)"),
        ("IFCMONETARYUNIT", b"('JPY')"),
        ("IFCUNITASSIGNMENT", b"((#2,#3))"),
        ("IFCOWNERHISTORY", b"(#2,#3,$,.ADDED.,$,$,$,0)"),
        ("IFCELEMENTQUANTITY", b"('g',#2,'N',$,$,(#3,#4))"),
        ("IFCCOMPLEXPROPERTY", b"('N','g','U',(#2,#3))"),
        ("IFCPROPERTYSET", b"('g',#2,'N',$,(#3,#4))"),
        ("IFCPROPERTYSINGLEVALUE", b"('N',$,IFCLABEL('v'),$)"),
        ("IFCQUANTITYLENGTH", b"('N',$,$,1.5,$)"),
        ("IFCQUANTITYAREA", b"('N',$,$,1.5,$)"),
    ],
)
def test_block_classes_have_no_refs_and_no_weight(cls, body):
    record = f"#1={cls}".encode() + body + b";"
    e = parse_record(record)
    assert e is not None, cls
    assert e.refs == (), cls
    assert e.weight == 0, cls
    assert e.is_parametric is False, cls


def test_cartesianpointlist3d_is_blocked_despite_looking_like_a_container():
    """設計書で明示されるケース: テッセレーション座標コンテナ
    IFCCARTESIANPOINTLIST3D は前方一致 `IFCCARTESIANPOINT*` によりブロック
    (refs=空, weight=0)に分類されなければならない。"""
    record = b"#1=IFCCARTESIANPOINTLIST3D(((0.,0.,0.),(1.,0.,0.),(0.,1.,0.)));"
    e = parse_record(record)
    assert e.refs == ()
    assert e.weight == 0


# --- 7. 罠の検証: frontierの厳密一致 vs blockの前方一致の非衝突 ---


def test_block_class_with_real_guid_shape_still_extracts_global_id_and_name():
    """IFCPROPERTYSET等ブロック分類のクラスも実際にはIfcRoot系で22文字の
    GlobalIdを持ちうる。GUID/Name抽出はrefs/weightの3分類と独立(形式一致
    のみ)に行われるので、ブロック分類でもglobal_id/nameは抽出される
    (refs/weightは分類どおり0のまま)。"""
    record = f"#1=IFCPROPERTYSET('{_GUID22}',#2,'PSet-01',$,(#3,#4));".encode()
    e = parse_record(record)
    assert e.refs == ()
    assert e.weight == 0
    assert e.global_id == _GUID22
    assert e.name == "PSet-01"


def test_no_frontier_class_is_shadowed_by_a_block_prefix():
    """IFCFACE を前方一致で扱うと IFCFACEBOUND / IFCFACEOUTERBOUND 等を
    誤って frontier 化してしまう罠がある(設計書の警告)。frontier 判定は
    全て厳密一致で行うため、block の前方一致プレフィックスがどの frontier
    クラス名の先頭にもならないことをモジュール定数そのもので固定する。"""
    for frontier_cls in _FRONTIER_ALL:
        assert not frontier_cls.startswith(_BLOCK_PREFIXES), frontier_cls
        assert frontier_cls not in _BLOCK_EXACT, frontier_cls


def test_facebound_and_faceouterbound_are_block_not_frontier():
    for cls, body in [("IFCFACEBOUND", b"(#2,.T.)"), ("IFCFACEOUTERBOUND", b"(#2,.T.)")]:
        record = f"#1={cls}".encode() + body + b";"
        e = parse_record(record)
        assert e.weight == 0, cls
        assert e.refs == (), cls


def test_facetedbrep_like_class_starting_with_ifcface_is_not_accidentally_frontier():
    """IFCFACETEDBREP は文字列として `IFCFACE` で始まるが、frontierの厳密
    一致判定に無関係なので中間(refs保持)として扱われなければならない。
    前方一致でfrontierを判定していたら誤ってweight=1になってしまう罠。"""
    record = b"#1=IFCFACETEDBREP(#2);"
    e = parse_record(record)
    assert e.weight == 0
    assert e.is_parametric is False
    assert e.refs == (2,)


# --- 8. 中間: refs抽出(文字列内の#参照誤認・入れ子括弧の罠) ---


def test_intermediate_class_extracts_refs_from_flat_and_nested_lists():
    record = b"#1=IFCTESTREL(#2,$,$,#3,(#4,#5,#6));"
    e = parse_record(record)
    assert e.refs == (2, 3, 4, 5, 6)
    assert e.weight == 0


def test_ref_like_pattern_inside_string_is_not_extracted():
    record = b"#1=IFCTESTREL('see #123 and #456 in string');"
    e = parse_record(record)
    assert e.refs == ()


def test_real_refs_alongside_string_containing_ref_lookalike():
    record = b"#1=IFCTESTREL(#2,'Desc #999 fake',(#3,#4),#5);"
    e = parse_record(record)
    assert e.refs == (2, 3, 4, 5)


def test_doubled_quote_inside_string_does_not_break_ref_scanning():
    """'' はSTEPの二重化エスケープ(バックスラッシュではない)。文字列終端の
    誤検出により後続の本物の参照を見失わないことを確認する。"""
    record = b"#1=IFCTESTREL(#2,'it''s a #777 fake ref',(#3),#4);"
    e = parse_record(record)
    assert e.refs == (2, 3, 4)


def test_deeply_nested_parens_with_typed_value_wrapper_still_finds_real_refs():
    """属性内ネスト括弧: 型付き値ラッパー(例: IFCLABEL(...))や多重リストが
    混在しても、文字列外の `#digit` だけを正しく全て拾う。"""
    record = (
        b"#1=IFCTESTREL(#2,IFCLABEL('#999 fake inside nested wrapper'),"
        b"(#3,(#4,#5)),#6);"
    )
    e = parse_record(record)
    assert e.refs == (2, 3, 4, 5, 6)


def test_semicolon_and_comma_inside_string_do_not_confuse_ref_scanning():
    record = b"#1=IFCTESTREL(#2,'a;b,c#3fake',#4);"
    e = parse_record(record)
    assert e.refs == (2, 4)


# --- 9. GUID抽出(第1属性が22文字の base64 風文字列) ---

_GUID22 = "2Occ4mT3stGu1d$_synth0"  # 合成値(実データ由来ではない)。22文字・GUIDアルファベットの形だけ本物と同じ


def test_guid_extracted_when_first_attribute_matches_22char_pattern():
    record = f"#1=IFCWALL('{_GUID22}',#2,'Wall-01',$);".encode()
    e = parse_record(record)
    assert e.global_id == _GUID22


def test_guid_not_extracted_when_first_attribute_is_a_ref():
    record = b"#1=IFCPOLYLOOP(#2,#3,#4);"
    e = parse_record(record)
    assert e.global_id is None


def test_guid_not_extracted_when_first_attribute_string_is_too_short():
    record = b"#1=IFCTESTREL('tooshort');"
    e = parse_record(record)
    assert e.global_id is None


def test_guid_gate_rejects_21_and_23_char_strings():
    for guid in (_GUID22[:-1], _GUID22 + "X"):
        record = f"#1=IFCWALL('{guid}',#2,'N',$);".encode()
        e = parse_record(record)
        assert e.global_id is None, guid


def test_guid_gate_accepts_alphabet_boundaries_digit_underscore_dollar():
    """GUIDアルファベット [0-9A-Za-z_$] の境界文字(数字・アンダースコア・
    ドル)を含む22文字も採録されることを確認する。"""
    guid = "0123456789_$abcXYZpqrs"
    assert len(guid) == 22
    record = f"#1=IFCWALL('{guid}',#2,'N',$);".encode()
    e = parse_record(record)
    assert e.global_id == guid


# --- 10. Name抽出(第3属性、\\X2\\デコード含む) ---


def test_name_extracted_as_third_attribute_when_guid_present():
    record = f"#1=IFCWALL('{_GUID22}',#2,'Wall-01',$);".encode()
    e = parse_record(record)
    assert e.name == "Wall-01"


def test_name_is_none_when_third_attribute_is_dollar():
    record = f"#1=IFCWALL('{_GUID22}',#2,$,$);".encode()
    e = parse_record(record)
    assert e.name is None


def test_name_is_none_when_no_guid_gate_match():
    record = b"#1=IFCPOLYLOOP(#2,#3,#4);"
    e = parse_record(record)
    assert e.name is None


def test_name_unescapes_doubled_quotes():
    record = f"#1=IFCWALL('{_GUID22}',#2,'It''s here',$);".encode()
    e = parse_record(record)
    assert e.name == "It's here"


def test_name_decodes_x2_utf16be_escape_matching_ifcopenshell_ground_truth():
    """\\X2\\ エスケープが正しく復号されることを固定する合成回帰値。全角文字
    (漢字+隅付き括弧)を含む名称 + \\X0\\ 後の素通り部分(連番付き末尾)という、
    small.ifc実データで観測された構造を模したケース。GUID・バイト列・復号後
    の文字列はいずれも合成(実データ由来ではない)。"""
    record = (
        b"#1=IFCWALL('" + _GUID22.encode() + b"',#2,"
        b"'\\X2\\301030C630B930C890E867503011518667F1\\X0\\ (000001)',$);"
    )
    e = parse_record(record)
    assert e.name == "【テスト部材】円柱 (000001)"
    assert e.name == "【テスト部材】円柱 (000001)"


def test_name_decodes_x2_escape_matching_description_ground_truth():
    """\\X2\\ エスケープのみで構成される合成バイト列(全角文字のみ、\\X0\\後の
    素通り部分なし)。Name抽出と同じデコード経路を通ることの追加確認として、
    第3属性位置にこのバイト列を置いて検証する(値は合成、実データ由来ではない)。"""
    record = (
        b"#1=IFCWALL('" + _GUID22.encode() + b"',#2,"
        b"'\\X2\\FF21FF2990E854C1\\X0\\',$);"
    )
    e = parse_record(record)
    assert e.name == "ＡＩ部品"
    assert e.name == "ＡＩ部品"


def test_name_passes_through_unrecognized_escapes_like_s_and_pa():
    r"""\S\ と \PA\ はデコードせず、そのままの文字列として残す
    (\X2\ 以外は仕様上パススルーする方針)。"""
    record = (
        b"#1=IFCWALL('" + _GUID22.encode() + b"',#2,"
        b"'literal \\S\\X \\PA\\ end',$);"
    )
    e = parse_record(record)
    assert e.name == "literal \\S\\X \\PA\\ end"


# --- 11. 統合テスト(実データ: small.ifc) ---


def test_small_ifc_face_count_matches_raw_regex_count_over_file(small_ifc_path):
    """small.ifc 全体をパースし、IFCFACE のクラス別件数が生ファイルへの
    正規表現カウントと一致することを確認する(タスクブリーフの指定)。"""
    raw = small_ifc_path.read_bytes()
    expected_face_count = len(re.findall(rb"=\s*IFCFACE\s*\(", raw))
    assert expected_face_count > 0

    face_count = 0
    for record in iter_records(small_ifc_path):
        e = parse_record(record)
        if e is not None and e.ifc_class == "IFCFACE":
            face_count += 1

    assert face_count == expected_face_count


def test_small_ifc_class_counts_match_ifcopenshell_for_sample_product_classes(small_ifc_path):
    """small.ifc を1回フルパースし、製品系3クラスの件数が ifcopenshell の
    by_type 件数と一致することを確認する(タスクブリーフの指定)。"""
    ifcopenshell = pytest.importorskip("ifcopenshell")
    model = ifcopenshell.open(str(small_ifc_path))
    sample_classes = ["IfcBuildingElementProxy", "IfcPipeSegment", "IfcPipeFitting"]
    expected = {cls.upper(): len(model.by_type(cls)) for cls in sample_classes}
    assert all(n > 0 for n in expected.values()), expected

    counts = Counter()
    for record in iter_records(small_ifc_path):
        e = parse_record(record)
        if e is not None:
            counts[e.ifc_class] += 1

    for cls, expected_n in expected.items():
        assert counts[cls] == expected_n, f"{cls}: got {counts[cls]} expected {expected_n}"


def test_large_ifc_full_parse_throughput_and_sanity(large_ifc_path):
    """large.ifc で reader.iter_records + parse_record を通した実測を報告する。

    reader.py単体は30.4MB/s(docs/plans/2026-07-24-cui-phase1.md Task 1)だが、parser.py の
    レコードごとの処理(正規表現によるヘッダ/GUIDマッチ、中間クラスの
    refs抽出、frontierの重み計算)が乗るため、これより遅くなるのは想定内。
    プロファイル(300,000レコード)で `_split_top_level` が1バイトずつの
    Pythonループになっていた実装上の穴を見つけて修正済み(find/正規表現の
    ジャンプ方式に変更、docs/plans/2026-07-24-cui-phase1.md Task 2 参照)。それでも合成テストの
    シンプルな操作の積み重ねで reader 単体より1桁近く遅い実測値になって
    いる。目標値は本タスクのブリーフに明記されていないため、壊滅的な
    劣化(1MB/s未満)のみを検知するフロアに留め、最適化判断自体は
    監督者/Task 8 に委ねる(reader.py の性能に対する既存方針を踏襲)。
    """
    from ifc_occam.scan.reader import iter_records as _iter_records

    file_size = large_ifc_path.stat().st_size
    start = time.perf_counter()
    total = 0
    none_count = 0
    for record in _iter_records(large_ifc_path):
        total += 1
        if parse_record(record) is None:
            none_count += 1
    elapsed = time.perf_counter() - start

    size_mb = file_size / (1024 * 1024)
    mb_per_sec = size_mb / elapsed if elapsed > 0 else float("inf")
    print(
        f"\n[test_large_ifc_full_parse_throughput_and_sanity] size={size_mb:.1f}MB "
        f"records={total} none={none_count} elapsed={elapsed:.2f}s "
        f"throughput={mb_per_sec:.1f}MB/s"
    )

    assert total > 0
    assert none_count == 0, "large.ifc の全レコードは解釈可能であるべき(未知の壊れ方の検知)"
    assert mb_per_sec > 1, "壊滅的な性能劣化(1MB/s未満)を検知"
