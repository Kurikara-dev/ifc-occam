"""textops/rewrite.py(ストリーム書き換え + 由来刻印)のTDD
(docs/plans/2026-07-25-cui-phase3.md Task 4)。

`rewrite_without(src_path, out_path, plan, graph, source_name, progress=None)
-> RewriteReport` は、Task 1(FullGraph)・Task 2(TextDeletePlan)・
Task 3(patch_rel_record)を繋いで、元のIFCファイルを一度も ifcopenshell で
フルオープンせずに「delete_idsのskip + patch_rel_idsのバイト列パッチ +
他レコードverbatim」のストリーム書き換えを行い、出力ヘッダに由来刻印
(FILE_DESCRIPTION.description への3行追記)を施す。

本テストは `TextDeletePlan` を手組みして(Task 2の`compute_text_delete_plan`
経由ではなく直接構築して)rewrite_without自体の書き換え機械(drop/patch/
verbatim/ヘッダ刻印/ソート検証)だけを焦点化する——カスケード・sweepの
正しさはtests/test_textops_plan.pyの責務であり、ここでは前提として与える。

検証する契約(brief Step 1 (a)-(f)):
  (a) drop_ids に含まれるレコードは出力から消える。
  (b) patch_rel_ids の候補はpatch_rel_record適用後の姿(参照リスト縮小)で
      出力される。
  (c) それ以外のレコードはverbatim(byte-for-byte)で残る。
  (d) ヘッダのFILE_DESCRIPTION.descriptionに既存エントリを保存したまま
      3行の由来刻印が追記される。
  (e) 出力はifcopenshell.openで開ける。
  (f) 未ソートのdrop_idsを渡すとValueError(ストリーム開始前、出力ファイルは
      作成されない)。

加えて監督者裁定の被覆:
  - patch_rel_recordがNoneを返す(rule4: 単独属性のdead)場合はrels_dropped
    としてカウントされ、records_dropped(drop_ids由来のdrop)とは区別される
    (裁定7)。
  - 壊れたレコード(_match_headerがNone)はrecords_inにカウントされ
    verbatimで出力される(graph.record_countには含まれないこととの対比、
    裁定6/7)。
  - 複数DATAセクションの入力は単一DATAセクションに統合される
    (Global Constraints)。
  - deleted_count = stats["seeds"] + stats["cascade"](swept/rels_dropped
    は「要素」ではないため混ぜない、裁定2)。
  - FILE_NAME.originating_systemはテキストレベルでは書き換えない
    (裁定3、brief は description 3エントリのみを要求)。
  - source_nameの'(クォート)二重化・非ASCII文字の\\X2\\...\\X0\\エスケープ
    (裁定3、日本語ファイル名でのround-trip証明)。
  - patch_rel_ids の昇順ソート前提も(drop_idsと同様に)1回検証する
    (裁定4「patch_rel_idsも同様に検証してよい」)。
  - progressは("rewrite", 処理済みレコード数, graph.record_count)で
    レコードごとに発火し、間引きは呼び出し側の責務(呼ばれる回数=records_in)。
  - RewriteReport.bytes_outは実際の出力ファイルサイズと一致する。

Step 3(等価性試験、本フェーズ受け入れの核)は別ファイル
tests/test_cui_phase3_equivalence.py に分離する(small.ifc実データを使う
重い統合テストのため、rewrite.py自体の機械的な単体/統合テストとは責務が
異なる)。
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

import ifcopenshell

from ifc_occam.core.provenance import build_provenance_lines
from ifc_occam.scan.fullgraph import scan_full_graph
from ifc_occam.scan.reader import iter_records
from ifc_occam.textops.plan import TextDeletePlan
from ifc_occam.textops.rewrite import (
    RewriteReport,
    _encode_step_string_body,
    rewrite_without,
)
from tests.fixtures_ifc import build_wall_with_window_ifc


# ---------------------------------------------------------------------------
# テストヘルパー
# ---------------------------------------------------------------------------


def _make_plan(
    drop_ids: list[int],
    patch_rel_ids: list[int] | None = None,
    *,
    seeds: int | None = None,
    cascade: int = 0,
    swept: int = 0,
    rels_dropped: int = 0,
    rels_patched: int | None = None,
) -> TextDeletePlan:
    """TextDeletePlanを手組みするテスト用ヘルパー(drop_ids/patch_rel_idsは
    昇順にソートして渡す——本ヘルパーはソート済み前提の通常テスト用。
    未ソートを試すテストはTextDeletePlanを直接構築する)。"""
    patch_list = list(patch_rel_ids or [])
    if seeds is None:
        seeds = len(drop_ids)
    if rels_patched is None:
        rels_patched = len(patch_list)
    return TextDeletePlan(
        drop_ids=np.array(sorted(drop_ids), dtype=np.int64),
        patch_rel_ids=np.array(sorted(patch_list), dtype=np.int64),
        stats={
            "seeds": seeds,
            "cascade": cascade,
            "swept": swept,
            "rels_dropped": rels_dropped,
            "rels_patched": rels_patched,
        },
    )


def _wrap_full(body: bytes, schema: str = "IFC4") -> bytes:
    """HEADER付きの完全なSTEPファイル形でラップする
    (tests/test_fullgraph.py の `_wrap_full` と同型)。"""
    return (
        b"ISO-10303-21;\n"
        b"HEADER;\n"
        b"FILE_DESCRIPTION((''),'2;1');\n"
        b"FILE_NAME('','',(''),(''),'','','');\n"
        b"FILE_SCHEMA(('" + schema.encode() + b"'));\n"
        b"ENDSEC;\n"
        b"DATA;\n"
        + body
        + b"\nENDSEC;\n"
    )


def _wrap_multi_data(body1: bytes, body2: bytes, schema: str = "IFC4") -> bytes:
    """2つのDATAセクションを持つSTEPファイル形でラップする
    (Global Constraints「マルチDATAセクション入力は単一DATAに統合」の検証用)。"""
    return (
        b"ISO-10303-21;\n"
        b"HEADER;\n"
        b"FILE_DESCRIPTION((''),'2;1');\n"
        b"FILE_NAME('','',(''),(''),'','','');\n"
        b"FILE_SCHEMA(('" + schema.encode() + b"'));\n"
        b"ENDSEC;\n"
        b"DATA;\n" + body1 + b"\nENDSEC;\n"
        b"DATA;\n" + body2 + b"\nENDSEC;\n"
    )


def _wrap_with_custom_description(file_description_stmt: bytes, body: bytes = b"") -> bytes:
    """`_wrap_full` の亜種: FILE_DESCRIPTION文だけをテストケースごとに
    差し替えられる(I4: 文字列内に釣り合わない括弧を含むヘッダの再現用)。"""
    return (
        b"ISO-10303-21;\n"
        b"HEADER;\n"
        + file_description_stmt + b"\n"
        b"FILE_NAME('','',(''),(''),'','','');\n"
        b"FILE_SCHEMA(('IFC4'));\n"
        b"ENDSEC;\n"
        b"DATA;\n"
        + body
        + b"\nENDSEC;\n"
    )


def _write(tmp_path, content: bytes, name: str = "model.ifc"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _build_wall_window_fixture(tmp_path, name: str = "src.ifc"):
    """build_wall_with_window_ifc() を書き出し、(ifcopenshell.file, src_path) を返す。"""
    f = build_wall_with_window_ifc()
    src_path = tmp_path / name
    f.write(str(src_path))
    return f, src_path


# ---------------------------------------------------------------------------
# (a): drop_ids に含まれるレコードは出力から消える + (e): ifcopenshell.open で開ける
# ---------------------------------------------------------------------------


def test_full_closure_drop_removes_all_targeted_classes_and_opens_with_ifcopenshell(tmp_path):
    f, src_path = _build_wall_window_fixture(tmp_path)
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    wall_id = f.by_type("IfcWall")[0].id()
    opening_id = f.by_type("IfcOpeningElement")[0].id()
    window_id = f.by_type("IfcWindow")[0].id()
    voids_id = f.by_type("IfcRelVoidsElement")[0].id()
    fills_id = f.by_type("IfcRelFillsElement")[0].id()
    assembly_gid = f.by_type("IfcElementAssembly")[0].GlobalId

    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(
        drop_ids=[wall_id, opening_id, window_id, voids_id, fills_id],
        patch_rel_ids=[],
        seeds=1,
        cascade=4,
    )

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert isinstance(report, RewriteReport)
    assert report.records_dropped == 5
    assert report.records_in == graph.record_count  # このfixtureに壊れたレコードは無い

    reopened = ifcopenshell.open(str(out_path))
    assert reopened.by_type("IfcWall") == []
    assert reopened.by_type("IfcOpeningElement") == []
    assert reopened.by_type("IfcWindow") == []
    assert reopened.by_type("IfcRelVoidsElement") == []
    assert reopened.by_type("IfcRelFillsElement") == []
    with pytest.raises(RuntimeError):
        reopened.by_guid(wall_gid)

    # 無関係の要素(アセンブリ)は残存する
    assert reopened.by_guid(assembly_gid) is not None


def test_output_file_opens_successfully_with_ifcopenshell_open(tmp_path):
    """brief (e) の最小確認(他テストでも暗黙に確認済みだが、直接対応させる)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    reopened = ifcopenshell.open(str(out_path))
    assert reopened is not None
    assert len(reopened.by_type("IfcProduct")) == len(f.by_type("IfcProduct"))


# ---------------------------------------------------------------------------
# (b): patch_rel_ids は patch_rel_record 適用後の姿になる
# (c): それ以外のレコードはverbatim
# ---------------------------------------------------------------------------


def test_patch_shrinks_related_objects_list_and_untouched_records_stay_verbatim(tmp_path):
    f, src_path = _build_wall_window_fixture(tmp_path)
    member1, member2 = f.by_type("IfcBeam")
    member1_id, member2_gid = member1.id(), member2.GlobalId
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    window_name = f.by_type("IfcWindow")[0].Name
    agg_id = f.by_type("IfcRelAggregates")[0].id()

    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[member1_id], patch_rel_ids=[agg_id])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert report.records_dropped == 1
    assert report.rels_patched == 1
    assert report.rels_dropped == 0

    reopened = ifcopenshell.open(str(out_path))
    rel = reopened.by_type("IfcRelAggregates")[0]
    assert [o.GlobalId for o in rel.RelatedObjects] == [member2_gid]

    # (c) 無関係のレコード(壁・窓・残った部材)はverbatimで残る
    assert reopened.by_guid(wall_gid) is not None
    assert reopened.by_guid(member2_gid) is not None
    window = reopened.by_type("IfcWindow")[0]
    assert window.Name == window_name


def test_patch_candidate_dropped_via_bare_dead_attribute_counts_as_rels_dropped_not_records_dropped(
    tmp_path,
):
    """rule4(単独属性のdead参照)によりpatch_rel_recordがNoneを返すケース:
    records_dropped(drop_ids由来のdrop)ではなくrels_dropped(patch由来のdrop)
    としてカウントされる(裁定7の区別)。意図的に「半端な」プラン
    (wallのみdrop、cascade先のopening/fills_relは残す)を与え、
    rewrite_without自体の機械的な挙動だけを焦点化する。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    wall_gid = f.by_type("IfcWall")[0].GlobalId
    wall_id = f.by_type("IfcWall")[0].id()
    voids_id = f.by_type("IfcRelVoidsElement")[0].id()

    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[wall_id], patch_rel_ids=[voids_id])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert report.records_dropped == 1  # wallのみ(drop_ids由来)
    assert report.rels_dropped == 1  # voids_relがbare dead属性でNone(patch由来)
    assert report.rels_patched == 0

    reopened = ifcopenshell.open(str(out_path))
    assert reopened.by_type("IfcRelVoidsElement") == []
    with pytest.raises(RuntimeError):
        reopened.by_guid(wall_gid)


def test_patch_candidate_with_internally_unbalanced_parens_passes_through_unchanged(tmp_path):
    """patch_rel_record が「入力と同じバイト列」を返すケース(body内部の括弧が
    対応していない壊れた入力。patch.py docstring 参照——理論上の保険ではなく
    実在する安全網、Task3レビューで実測確認済み)。rewrite_without 自身はこの
    結果を rels_patched/rels_dropped のどちらにも数えず、verbatim で出力する
    (裁定7の定義: 両カウンタとも「実際にNoneまたは異なるバイト列を返した数」
    であり、「入力と同一バイト列を返した」場合は該当しない)。"""
    body = b"#1=IFCTESTLEAF();\n" b"#5=IFCRELASSOCIATESMATERIAL(#1));\n"
    src_path = _write(tmp_path, _wrap_full(body))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[1], patch_rel_ids=[5])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert report.records_dropped == 1  # #1のみ
    assert report.rels_patched == 0
    assert report.rels_dropped == 0

    out_records = list(iter_records(out_path))
    assert out_records == [b"#5=IFCRELASSOCIATESMATERIAL(#1));"]


# ---------------------------------------------------------------------------
# (d): ヘッダ刻印 + 既存description保存
# ---------------------------------------------------------------------------


def test_header_stamp_preserves_existing_description_entries_and_appends_three(tmp_path):
    f = build_wall_with_window_ifc()
    f.header.file_description.description = (
        "ViewDefinition [CoordinationView]",
        "Another pre-existing note",
    )
    src_path = tmp_path / "src.ifc"
    f.write(str(src_path))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    rewrite_without(src_path, out_path, plan, graph, source_name="original.ifc")

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description

    assert description[:2] == ("ViewDefinition [CoordinationView]", "Another pre-existing note")

    expected_stamp = build_provenance_lines("original.ifc", deleted_count=0, simplified_count=0)
    assert description[2:] == expected_stamp


def test_header_stamp_deleted_count_uses_seeds_plus_cascade_only_not_swept_or_rels(tmp_path):
    """裁定2: deleted_count = stats['seeds'] + stats['cascade']。swept/
    rels_droppedは「要素」ではないため混ぜない。stats合計とlen(drop_ids)を
    意図的に不一致にしてこの式が正しく実装されていることを固定する。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    wall_id = f.by_type("IfcWall")[0].id()
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)

    plan = _make_plan(
        drop_ids=[wall_id],
        patch_rel_ids=[],
        seeds=1,
        cascade=0,
        swept=999,
        rels_dropped=999,
    )

    rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == "Deleted 1 elements (incl. cascade); simplified 0" for d in description)


def test_originating_system_is_not_modified_at_text_level(tmp_path):
    """裁定3: FILE_NAME.originating_systemのテキストレベル書き換えはやらない。"""
    f = build_wall_with_window_ifc()
    f.header.file_name.originating_system = "Original System XYZ"
    src_path = tmp_path / "src.ifc"
    f.write(str(src_path))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    reopened = ifcopenshell.open(str(out_path))
    assert reopened.header.file_name.originating_system == "Original System XYZ"


def test_header_stamp_round_trips_non_ascii_source_name(tmp_path):
    """裁定3(監督者の明示要求): 日本語ファイル名をsource_nameに渡し、出力を
    ifcopenshell.openで開いた際にヘッダのdescriptionから元の文字列が
    復元できることを確認する(round-trip証明)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    non_ascii_source_name = "図面データ.ifc"
    rewrite_without(src_path, out_path, plan, graph, source_name=non_ascii_source_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {non_ascii_source_name}" for d in description)


def test_header_stamp_round_trips_single_quote_in_source_name(tmp_path):
    """裁定3: source_name内の'(シングルクォート)が''二重化され、
    ifcopenshell.openでの復元後は元の文字列に戻ること。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    source_name_with_quote = "it's a test model.ifc"
    rewrite_without(src_path, out_path, plan, graph, source_name=source_name_with_quote)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {source_name_with_quote}" for d in description)


def test_header_stamp_round_trips_mixed_quote_and_non_ascii_source_name(tmp_path):
    """裁定3: クォートと非ASCIIが同時に混在するケースの複合round-trip。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    tricky_name = "図面's データ.ifc"
    rewrite_without(src_path, out_path, plan, graph, source_name=tricky_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {tricky_name}" for d in description)


# ---------------------------------------------------------------------------
# 修正1(Important-1、監督者裁定3の訂正): 非BMP文字(U+10000以上)は \X4\ を使う。
#
# 裁定3原文は「非ASCIIは\X2\」だったが、これは誤りだった。ifcopenshell 0.8.5 は
# \X2\ 内のUTF-16BEサロゲートペアを合成復号できず、対応する文字を例外を出さず
# 無音で消失させる(実測: source_name="\U0001F600"をエンコードしifcopenshell.open
# で開くと、Sourceエントリの絵文字が消えて空文字列になる)。ifcopenshell自身の
# headerライタは非BMP文字に\X4\<UCS-4の32bit・8桁hex>\X0\を使い、これは正しく
# round-tripする。以下はいずれもifcopenshell.openでの完全一致round-tripを
# 直接証明する(BMPのみのケースは既存のtest_header_stamp_round_trips_non_ascii_
# source_nameがカバーしているため再掲しない)。
# ---------------------------------------------------------------------------


def test_header_stamp_round_trips_lone_non_bmp_emoji_source_name(tmp_path):
    """(b) 絵文字 U+1F600 単独。修正前は\\X2\\のサロゲートペアがifcopenshellに
    合成復号されず無音で消失する(Source: が空文字列になる)ことを確認済み。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    emoji_source_name = "\U0001F600"
    rewrite_without(src_path, out_path, plan, graph, source_name=emoji_source_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {emoji_source_name}" for d in description)


def test_header_stamp_round_trips_lone_supplementary_kanji_source_name(tmp_path):
    """(c) 補助面漢字 U+29E3D(「𩸽」)単独。絵文字だけでなく実在の漢字も
    \\X2\\では同様に消失することを固定する。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    kanji_source_name = "\U00029E3D"
    rewrite_without(src_path, out_path, plan, graph, source_name=kanji_source_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {kanji_source_name}" for d in description)


def test_header_stamp_round_trips_ascii_bmp_and_non_bmp_mixed_across_run_boundary(tmp_path):
    """(d) ASCII + BMP + 非BMP の混在で、かつBMP(図)と非BMP(😀)が直接隣接し
    ラン境界(\\X2\\...\\X0\\\\X4\\...\\X0\\)を跨ぐケース。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    tricky_name = "before\u56f3\U0001F600after.ifc"  # ASCII + 図(BMP) + 😀(非BMP) + ASCII
    rewrite_without(src_path, out_path, plan, graph, source_name=tricky_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {tricky_name}" for d in description)


def test_header_stamp_round_trips_two_consecutive_non_bmp_characters(tmp_path):
    """(e) 非BMPが連続2文字以上(異なる非BMP文字が隣接し、1組の\\X4\\...\\X0\\に
    まとめられる)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    two_non_bmp_name = "\U0001F600\U00029E3D"  # 😀 + 𩸽、連続
    rewrite_without(src_path, out_path, plan, graph, source_name=two_non_bmp_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {two_non_bmp_name}" for d in description)


# ---------------------------------------------------------------------------
# 修正2(Important-2): バックスラッシュは \\ に二重化する(未対応だと
# ifcopenshell.open がセグメンテーションフォルトで落ちる——表示崩れではなく
# プロセス即死)。
# ---------------------------------------------------------------------------


def test_header_stamp_round_trips_backslash_in_source_name_without_crashing(tmp_path):
    """source_nameにWindowsパス区切りのバックスラッシュを含めても、出力を
    ifcopenshell.openした際にクラッシュ(セグメンテーションフォルト)も
    例外も起きず、Source: 行が元の文字列と完全一致で復元されること。

    修正前(バックスラッシュ無変換)は実測でifcopenshell.openが exit code 139
    (セグメンテーションフォルト)でプロセスごと落ちることを確認済み
    (このテスト自体をsubprocessで単独実行して確認した。RED観測の詳細は
    docs/plans/2026-07-25-cui-phase3.md Task 4 の追記部分を参照)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    backslash_source_name = r"C:\path\to\file.ifc"
    rewrite_without(src_path, out_path, plan, graph, source_name=backslash_source_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {backslash_source_name}" for d in description)


def test_header_stamp_round_trips_quote_backslash_and_non_ascii_all_mixed(tmp_path):
    """'(クォート)と\\(バックスラッシュ)と非ASCII(BMP+非BMP)が同時に混在する
    文字列でもround-tripすること(3種のエスケープ機構すべてを同時に踏む複合
    ケース)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    mixed_name = "C:\\図's\\😀\\data.ifc"
    rewrite_without(src_path, out_path, plan, graph, source_name=mixed_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {mixed_name}" for d in description)


# ---------------------------------------------------------------------------
# (f): 未ソートのdrop_ids/patch_rel_idsを渡すとValueError
# ---------------------------------------------------------------------------


def test_unsorted_drop_ids_raises_value_error_and_leaves_no_output_file(tmp_path):
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)

    plan = TextDeletePlan(
        drop_ids=np.array([5, 2, 8], dtype=np.int64),  # 未ソート
        patch_rel_ids=np.array([], dtype=np.int64),
        stats={"seeds": 0, "cascade": 0, "swept": 0, "rels_dropped": 0, "rels_patched": 0},
    )

    with pytest.raises(ValueError):
        rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert not out_path.exists()


def test_unsorted_patch_rel_ids_raises_value_error_and_leaves_no_output_file(tmp_path):
    """裁定4「patch_rel_idsも同様に検証してよい」の実装確認。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)

    plan = TextDeletePlan(
        drop_ids=np.array([], dtype=np.int64),
        patch_rel_ids=np.array([9, 3, 7], dtype=np.int64),  # 未ソート
        stats={"seeds": 0, "cascade": 0, "swept": 0, "rels_dropped": 0, "rels_patched": 0},
    )

    with pytest.raises(ValueError):
        rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert not out_path.exists()


def test_unsorted_drop_ids_error_message_hints_at_duplicate_record_ids(tmp_path):
    """M3(Minor、フェーズ最終レビュー): 狭義単調増加違反(入力IFCに重複
    record idがあると起こり得る)のエラーメッセージに、その可能性を示す
    ヒントが含まれること。fail-loud自体の挙動(ValueErrorを出す・出力
    ファイルを残さない)は変えない(既存の
    test_unsorted_drop_ids_raises_value_error_and_leaves_no_output_file が
    そのまま固定している)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)

    plan = TextDeletePlan(
        drop_ids=np.array([5, 2, 8], dtype=np.int64),  # 未ソート
        patch_rel_ids=np.array([], dtype=np.int64),
        stats={"seeds": 0, "cascade": 0, "swept": 0, "rels_dropped": 0, "rels_patched": 0},
    )

    with pytest.raises(ValueError, match="duplicate"):
        rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")


# ---------------------------------------------------------------------------
# 壊れたレコード(_match_headerがNone): records_inには数えるがgraph.record_countには
# 含まれない。verbatimで出力される(裁定6/7)。
# ---------------------------------------------------------------------------


def test_broken_record_is_counted_in_records_in_but_not_in_graph_record_count_and_is_verbatim(
    tmp_path,
):
    body = (
        b"#1=IFCWALL();\n"
        b"#2=IFCBROKEN(#1,#3;\n"  # 閉じ括弧が無い壊れたレコード
        b"#3=IFCMATERIAL();\n"
    )
    src_path = _write(tmp_path, _wrap_full(body))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert graph.record_count == 2  # #2は壊れているためグラフには載らない
    assert report.records_in == 3  # だがrewriteは#2も読んだレコードとして数える
    assert report.records_dropped == 0

    out_records = list(iter_records(out_path))
    assert out_records == [
        b"#1=IFCWALL();",
        b"#2=IFCBROKEN(#1,#3;",
        b"#3=IFCMATERIAL();",
    ]


# ---------------------------------------------------------------------------
# マルチDATAセクション入力は単一DATAセクションに統合される(Global Constraints)
# ---------------------------------------------------------------------------


def test_multi_data_section_input_is_consolidated_into_a_single_output_data_section(tmp_path):
    body1 = b"#1=IFCWALL();\n"
    body2 = b"#2=IFCMATERIAL();\n"
    src_path = _write(tmp_path, _wrap_multi_data(body1, body2))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert report.records_in == 2

    out_bytes = out_path.read_bytes()
    assert out_bytes.count(b"DATA;") == 1
    assert out_bytes.count(b"ENDSEC;") == 2  # HEADER用+統合後の単一DATA用
    assert out_bytes.count(b"END-ISO-10303-21;") == 1

    records = list(iter_records(out_path))
    assert records == [b"#1=IFCWALL();", b"#2=IFCMATERIAL();"]


# ---------------------------------------------------------------------------
# progress: ("rewrite", 処理済みレコード数, graph.record_count) でレコードごとに発火
# ---------------------------------------------------------------------------


def test_progress_callback_fires_once_per_record_with_running_count_and_fixed_total(tmp_path):
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    calls: list[tuple[str, int, int]] = []

    def progress(stage: str, done: int, total: int) -> None:
        calls.append((stage, done, total))

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc", progress=progress)

    assert len(calls) == report.records_in == graph.record_count
    assert all(stage == "rewrite" for stage, _done, _total in calls)
    assert all(total == graph.record_count for _stage, _done, total in calls)
    assert [done for _stage, done, _total in calls] == list(range(1, report.records_in + 1))


def test_progress_omitted_defaults_to_none_and_rewrite_still_succeeds(tmp_path):
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert report.records_in == graph.record_count


# ---------------------------------------------------------------------------
# RewriteReport.bytes_out は実際の出力ファイルサイズと一致する
# ---------------------------------------------------------------------------


def test_bytes_out_matches_actual_output_file_size(tmp_path):
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert report.bytes_out == out_path.stat().st_size
    assert report.bytes_out > 0


# ---------------------------------------------------------------------------
# Phase I Task3: rewrite_without 自身が書く区切り(DATA本体の各レコード後と
# 末尾の ENDSEC;/END-ISO-10303-21;)をCRLF統一する。ヘッダは入力のverbatim
# コピーであり本修正の対象外——このフィクスチャは ifcopenshell.file.write()
# 産(ヘッダCRLF)なので結果として全行CRLFになるが、LF改行の入力を食わせれば
# ヘッダLF+DATA CRLFの混在になる(テキストモードのみ・実害なし。Phase I
# 最終レビューM-2の判定)。統一前はDATA側だけLFで、裸のLF(`\r\n`の一部で
# ないもの)が残っていた。
# ---------------------------------------------------------------------------


def test_rewrite_without_output_is_fully_crlf_with_no_lone_lf(tmp_path):
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    out_bytes = out_path.read_bytes()
    assert out_bytes.count(b"\n") > 0
    # 全ての LF が直前の CR を伴う(= 裸の LF が1つも無い)ことの確認。
    assert out_bytes.count(b"\n") == out_bytes.count(b"\r\n")


# ---------------------------------------------------------------------------
# 監督者による ⚠️ の引き取り(Task 4 再レビュー): BMP/非BMP の閾値そのもの
# (U+FFFF と U+10000)と空の source_name は、実装者もレビュアも実測して
# いなかった(分岐の `>= 0x10000` はコード読解のみで正しいと判断されていた)。
# ---------------------------------------------------------------------------


def test_encode_step_string_body_switches_run_kind_exactly_at_u10000():
    """U+FFFF(BMPの最大)は \\X2\\、U+10000(非BMPの最小)は \\X4\\ になり、
    隣接すると2つの独立したランに分かれること(閾値の境界固定)。

    round-trip ではなくエンコーダ直叩きで固定する: U+FFFF は Unicode の
    noncharacter であり、ifcopenshell 側の扱いに依存させたくないため、
    ここでは「本モジュールが出力するバイト列」だけを検証する。
    """
    assert _encode_step_string_body("￿") == b"\\X2\\FFFF\\X0\\"
    assert _encode_step_string_body("\U00010000") == b"\\X4\\00010000\\X0\\"
    assert _encode_step_string_body("￿\U00010000") == (
        b"\\X2\\FFFF\\X0\\" + b"\\X4\\00010000\\X0\\"
    )


def test_header_stamp_round_trips_minimum_non_bmp_codepoint(tmp_path):
    """非BMPの最小コードポイント U+10000 が実際に ifcopenshell で round-trip
    すること(閾値の直上が \\X4\\ 側へ正しく落ちることの実測)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    boundary_name = "a\U00010000b.ifc"
    rewrite_without(src_path, out_path, plan, graph, source_name=boundary_name)

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d == f"Source: {boundary_name}" for d in description)


def test_header_stamp_accepts_empty_source_name(tmp_path):
    """source_name が空文字列でも例外なく書き出せ、出力が ifcopenshell で
    開けること(刻印3行の構造自体は保たれる)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    rewrite_without(src_path, out_path, plan, graph, source_name="")

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert any(d.startswith("Source:") for d in description)


# ---------------------------------------------------------------------------
# C1(Critical、フェーズ最終レビュー): out_path == src_path だと
# `open(out_path, "wb")` が `iter_records(src_path)` より先に実行されるため、
# 出力先=入力先のとき原本を無音で truncate してしまう。ライブラリ層(本命)の
# ガード: 出力ファイルを開く前(_ensure_sorted_ascendingの検証と同じ位置)で
# 同一実体を検出したら ValueError。原本の代わりに small.ifc 実体は使わず、
# tmp_path 上の合成フィクスチャのコピーで再現・検証する(絶対の禁止事項:
# 原本IFCを変更・上書き・削除しない)。
# ---------------------------------------------------------------------------


def test_out_path_same_as_src_path_raises_and_leaves_copy_byte_identical(tmp_path):
    """(i): out_path == src_path(tmp_path上のコピーに対して)で ValueError が
    出て、コピーが1バイトも変わっていないこと(サイズとハッシュで確認)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    before_size = src_path.stat().st_size
    before_hash = hashlib.sha256(src_path.read_bytes()).hexdigest()

    with pytest.raises(ValueError):
        rewrite_without(src_path, src_path, plan, graph, source_name="x.ifc")

    assert src_path.stat().st_size == before_size
    assert hashlib.sha256(src_path.read_bytes()).hexdigest() == before_hash


def test_out_path_same_as_src_path_as_plain_strings_also_raises(tmp_path):
    """repl.py からの通常の呼び出し規約(str)でも同じ判定が効くこと(Pathオブジェクト
    限定のガードになっていないことの確認)。"""
    f, src_path = _build_wall_window_fixture(tmp_path)
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    with pytest.raises(ValueError):
        rewrite_without(str(src_path), str(src_path), plan, graph, source_name="x.ifc")


def test_out_path_differing_only_by_case_still_raises_as_same_file(tmp_path):
    """(ii): 大文字小文字違いの表記でも同一実体を指すパスは弾く(Windowsの
    既定ファイルシステムは大文字小文字を区別しないため、os.path.samefile で
    正しく同一と判定できる——表記が違うだけで通り抜けるガードになっていない
    ことの確認)。"""
    f, src_path = _build_wall_window_fixture(tmp_path, name="src.ifc")
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    upper_variant = src_path.with_name(src_path.name.upper())
    assert str(upper_variant) != str(src_path)  # 文字列としては異なる表記

    with pytest.raises(ValueError):
        rewrite_without(src_path, upper_variant, plan, graph, source_name="x.ifc")

    assert src_path.exists()  # コピー自体も無傷(参考確認)


def test_out_path_with_dot_slash_prefix_still_raises_as_same_file(tmp_path, monkeypatch):
    """(ii): `./` 付きなど表記が異なるが同一実体を指すパスも弾く。"""
    f, src_path = _build_wall_window_fixture(tmp_path, name="src.ifc")
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    monkeypatch.chdir(tmp_path)
    dotted_variant = f"./{src_path.name}"
    assert dotted_variant != str(src_path)

    with pytest.raises(ValueError):
        rewrite_without(src_path.name, dotted_variant, plan, graph, source_name="x.ifc")


def test_nonempty_drop_ids_with_zero_records_in_raises_value_error(tmp_path):
    """(iv) 事後条件(保険): plan.drop_ids が非空なのに records_in == 0
    (入力から1レコードも読めなかった)場合は矛盾として fail loud にする
    (監督者裁定5「seeds==0なら書かない」と対になる保険)。DATA;セクションは
    あるがレコードが1件も無いファイルに対し、drop_idsが非空のplanを与える。"""
    src_path = _write(tmp_path, _wrap_full(b""))  # DATA;はあるがレコード無し
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    assert graph.record_count == 0

    plan = _make_plan(drop_ids=[1], patch_rel_ids=[], seeds=1)

    with pytest.raises(ValueError):
        rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")


def test_zero_drop_ids_with_zero_records_in_does_not_raise(tmp_path):
    """事後条件の反対側: drop_ids が空なら records_in == 0 でも矛盾ではない
    (単に削除対象なしの空DATAセクションを素通りさせるだけ)ので例外は出ない。"""
    src_path = _write(tmp_path, _wrap_full(b""))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    report = rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    assert report.records_in == 0


# ---------------------------------------------------------------------------
# I4(Important、フェーズ最終レビュー): `_stamp_header` は括弧の深さだけを
# 追跡し文字列リテラルを見ないため、既存descriptionの文字列内に釣り合わない
# 丸括弧があると、深さ追跡が狂って既存エントリを破壊したり刻印がarity違反の
# 位置に付いたりする(いずれも ifcopenshell.open は成功するため無音)。修正:
# 深さ走査の前に parser.py の `_blank_strings`(長さを保存する)でヘッダを
# ブランク化し、そのオフセットを元バイト列への挿入位置として使う。
# ---------------------------------------------------------------------------


def test_header_stamp_preserves_entry_with_unbalanced_close_paren_in_string(tmp_path):
    """既存descriptionの文字列内に釣り合わない')'があっても、そのエントリを
    バイト単位で保存し3件の刻印を正しくdescriptionリスト末尾に追記できること。
    修正前は文字列中の')'をリストの閉じ括弧と誤認し、既存エントリの後半
    (')'以降)を消失させた上で刻印をその位置に割り込ませていた。"""
    body = b"#1=IFCWALL();\n"
    file_description = b"FILE_DESCRIPTION (('Phase 1)'), '2;1');"
    src_path = _write(tmp_path, _wrap_with_custom_description(file_description, body))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert description[0] == "Phase 1)"  # 既存エントリがバイト単位で保存されている
    expected_stamp = build_provenance_lines("x.ifc", deleted_count=0, simplified_count=0)
    assert description[1:] == expected_stamp
    # arity不変(FILE_DESCRIPTIONは2引数のまま): FILE_NAME/FILE_SCHEMAも正常に読める
    assert reopened.header.file_name is not None
    assert reopened.header.file_schema.schema_identifiers == ("IFC4",)


def test_header_stamp_preserves_entry_with_unbalanced_open_paren_in_string(tmp_path):
    """既存descriptionの文字列内に釣り合わない'('があっても壊れないこと。
    修正前は深さ追跡が最後まで0に戻らず、刻印3件がFILE_DESCRIPTION自身の
    引数として'2;1'の後ろに追加され、arity違反(2引数→5引数)を起こしていた。"""
    body = b"#1=IFCWALL();\n"
    file_description = b"FILE_DESCRIPTION (('Phase (1'), '2;1');"
    src_path = _write(tmp_path, _wrap_with_custom_description(file_description, body))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")

    reopened = ifcopenshell.open(str(out_path))
    description = reopened.header.file_description.description
    assert description[0] == "Phase (1"
    expected_stamp = build_provenance_lines("x.ifc", deleted_count=0, simplified_count=0)
    assert description[1:] == expected_stamp
    assert reopened.header.file_schema.schema_identifiers == ("IFC4",)


def test_header_stamp_raises_when_file_description_statement_is_unbalanced(tmp_path):
    """FILE_DESCRIPTION文の括弧が閉じないまま(未終端の文字列リテラル等で)
    ヘッダ末尾に到達した場合は ValueError で fail loud にすること(N1)。

    `_blank_strings` は文境界を知らないため、未終端の文字列リテラルがあると
    次の文の引用符と勝手にペアリングし、深さ追跡が0に戻らないままヘッダ末尾
    まで走る。修正前はこの異常終了を「depth==0 で break した正常終了」と
    区別せず、刻印3件が別のエンティティ(FILE_SCHEMA 等)へ紛れ込んだ
    (長さ保存の整合性チェックは着地点が元バイト列でも `)` であれば通って
    しまうため、これを捕まえられない)。無音で別エンティティを汚染するより
    落とす方が良い。
    """
    body = b"#1=IFCWALL();\n"
    file_description = b"FILE_DESCRIPTION (('unterminated), '2;1');"
    src_path = _write(tmp_path, _wrap_with_custom_description(file_description, body))
    out_path = tmp_path / "out.ifc"
    graph = scan_full_graph(src_path)
    plan = _make_plan(drop_ids=[], patch_rel_ids=[])

    with pytest.raises(ValueError, match="not balanced"):
        rewrite_without(src_path, out_path, plan, graph, source_name="x.ifc")
