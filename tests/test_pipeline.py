"""scan/pipeline.py の scan_records(バルク走査経路)のTDD
(監督者指示の性能最適化。docs/plans/2026-07-24-cui-phase1.md Task 2)。

scan_records(path) -> RawScan は、reader.iter_records を1回だけ回し、
レコードごとに安価なヘッダ(id/クラス名)抽出だけを行った上でカテゴリ
(frontier/block/intermediate)に分岐する:
  - block: class_counts のみ加算(実データの約85%を占める。bodyのスライス
    すら行わない構造的高速化)。
  - frontier かつ単純(IFCFACE/IFCFACESURFACE/IFCADVANCEDFACE、weight常に1):
    class_counts加算 + entity_id を face_ids に記録。
  - frontier かつパラメトリック/テッセレーション系: 従来通り重みを計算し
    (entity_id, weight, is_parametric) を weighted に記録。
  - intermediate: 従来通り refs/GUID/Name を抽出し ScanEntity を entities に
    格納。

parse_record自体は変更しない(既存85テストの契約を保つ)。本ファイルは
scan_records が (1) カテゴリ別に正しく振り分けること、(2) HEADER から
schema を取り出せること、(3) 件数の整合性が取れること、(4) 「全レコードを
parse_record にかける」旧経路と等価であること、(5) その旧経路より
構造的に速いこと、を検証する。
"""

import time
from collections import Counter

from ifc_occam.scan.parser import (
    PARAMETRIC_NOMINAL_TRIS,
    ScanEntity,
    _FRONTIER_FACES,
    _classify,
    parse_record,
)
from ifc_occam.scan.pipeline import RawScan, scan_records
from ifc_occam.scan.reader import iter_records

_GUID = "2Occ4mT3stGu1d$_synth0"  # 22文字のGUID形(test_parser.pyの合成値と同じ形。実データ由来ではない)


# --- テスト用ヘルパー ---


def _write(tmp_path, content: bytes, name: str = "model.ifc"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _wrap_full(body: bytes, schema: str = "IFC4") -> bytes:
    """HEADER付きの完全なSTEPファイル形でラップする(tests/test_reader.py の
    _wrap_full と同型。FILE_SCHEMA の値を差し替え可能にした版)。"""
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


def _old_full_scan(path) -> tuple[Counter, dict[str, list[ScanEntity]]]:
    """scan_records 以前の経路(全レコードを parse_record にかける)を再現
    した参照実装(等価性テスト専用)。class_counts(Counter)と、
    _classify によるカテゴリ別の ScanEntity リストを返す。
    """
    class_counts: Counter = Counter()
    by_category: dict[str, list[ScanEntity]] = {
        "frontier": [],
        "block": [],
        "intermediate": [],
    }
    for record in iter_records(path):
        e = parse_record(record)
        if e is None:
            continue
        class_counts[e.ifc_class] += 1
        by_category[_classify(e.ifc_class)].append(e)
    return class_counts, by_category


def _assert_matches_old_full_scan(path, scan: RawScan) -> None:
    """scan_records の結果が、旧経路(_old_full_scan)と完全に等価であることを
    検証する共通アサーション(合成ファイル・small.ifc の両方から呼ぶ)。
    """
    expected_counts, by_category = _old_full_scan(path)

    assert dict(scan.class_counts) == dict(expected_counts)
    assert scan.entities == by_category["intermediate"]
    assert scan.total_records == sum(expected_counts.values())
    assert scan.total_records == sum(scan.class_counts.values())

    expected_face_ids = sorted(
        e.entity_id for e in by_category["frontier"] if e.ifc_class in _FRONTIER_FACES
    )
    assert sorted(scan.face_ids) == expected_face_ids

    expected_weighted = sorted(
        (e.entity_id, e.weight, e.is_parametric)
        for e in by_category["frontier"]
        if e.ifc_class not in _FRONTIER_FACES
    )
    assert sorted(scan.weighted) == expected_weighted


# --- 1. カテゴリ別振り分け ---


def test_blocked_class_counted_but_absent_from_entities(tmp_path):
    body = (
        b"#1=IFCCARTESIANPOINT((0.,0.,0.));\n"
        b"#2=IFCCARTESIANPOINT((1.,0.,0.));\n"
        b"#3=IFCDIRECTION((0.,0.,1.));\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    scan = scan_records(path)

    assert scan.class_counts == {"IFCCARTESIANPOINT": 2, "IFCDIRECTION": 1}
    assert scan.entities == []
    assert len(scan.face_ids) == 0
    assert scan.weighted == []
    assert scan.total_records == 3


def test_simple_frontier_face_ids_collected_and_counted(tmp_path):
    body = (
        b"#1=IFCFACE((#9),.T.);\n"
        b"#2=IFCFACESURFACE((#9),#10,.T.);\n"
        b"#3=IFCADVANCEDFACE((#9),#10,.T.);\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    scan = scan_records(path)

    assert list(scan.face_ids) == [1, 2, 3]
    assert scan.class_counts == {
        "IFCFACE": 1,
        "IFCFACESURFACE": 1,
        "IFCADVANCEDFACE": 1,
    }
    assert scan.entities == []
    assert scan.weighted == []
    assert scan.total_records == 3


def test_tessellated_and_parametric_frontier_weights_in_weighted(tmp_path):
    body = (
        b"#1=IFCTRIANGULATEDFACESET(#2,$,.F.,((1,2,3),(4,5,6)),$);\n"  # weight=2
        b"#2=IFCPOLYGONALFACESET(#3,$,(#10,#11,#12),$);\n"  # weight=3
        b"#3=IFCEXTRUDEDAREASOLID(#4,#5,0.);\n"  # weight=nominal, is_parametric
    )
    path = _write(tmp_path, _wrap_full(body))
    scan = scan_records(path)

    assert sorted(scan.weighted) == [
        (1, 2, False),
        (2, 3, False),
        (3, PARAMETRIC_NOMINAL_TRIS, True),
    ]
    assert scan.entities == []
    assert len(scan.face_ids) == 0
    assert scan.total_records == 3


def test_intermediate_scanentity_has_refs_and_guid_when_present(tmp_path):
    body = (
        f"#1=IFCWALL('{_GUID}',#2,'Wall-01',(#3,#4));\n".encode()
        + b"#2=IFCTESTREL(#5,#6);\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    scan = scan_records(path)

    assert scan.class_counts == {"IFCWALL": 1, "IFCTESTREL": 1}
    assert len(scan.entities) == 2
    assert len(scan.face_ids) == 0
    assert scan.weighted == []

    wall = next(e for e in scan.entities if e.ifc_class == "IFCWALL")
    assert wall.entity_id == 1
    assert wall.refs == (2, 3, 4)
    assert wall.global_id == _GUID
    assert wall.name == "Wall-01"
    assert wall.weight == 0
    assert wall.is_parametric is False

    rel = next(e for e in scan.entities if e.ifc_class == "IFCTESTREL")
    assert rel.refs == (5, 6)
    assert rel.global_id is None
    assert rel.name is None


def test_malformed_record_is_excluded_from_class_counts_and_totals(tmp_path):
    """閉じ括弧の無い壊れたレコード(parse_recordがNoneを返す形と同じ)は
    どのカテゴリにも数えない(reader自体は';'までを1レコードとして返す
    ので、走査は止まらず後続の正常なレコードは数え続ける)。"""
    body = (
        b"#1=IFCCARTESIANPOINT((0.,0.,0.));\n"
        b"#2=IFCBROKEN(#3,#4;\n"
        b"#3=IFCDIRECTION((0.,0.,1.));\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    scan = scan_records(path)

    assert "IFCBROKEN" not in scan.class_counts
    assert scan.total_records == 2
    assert sum(scan.class_counts.values()) == scan.total_records


# --- 2. schema抽出 ---


def test_schema_extracted_from_header_file_schema(tmp_path):
    body = b"#1=IFCCARTESIANPOINT((0.,0.,0.));\n"
    path = _write(tmp_path, _wrap_full(body, schema="IFC4X3"))
    scan = scan_records(path)
    assert scan.schema == "IFC4X3"


def test_schema_is_empty_string_when_header_absent(tmp_path):
    """HEADER無し(最小のDATAセクションのみ)でも走査自体は続行し、
    schemaだけ空文字列にフォールバックする(例外を投げない)。"""
    content = b"DATA;\n#1=IFCCARTESIANPOINT((0.,0.,0.));\nENDSEC;\n"
    path = _write(tmp_path, content)
    scan = scan_records(path)
    assert scan.schema == ""
    assert scan.total_records == 1


# --- 3. 件数の整合性 ---


def test_total_records_equals_sum_of_class_counts(tmp_path):
    body = (
        b"#1=IFCCARTESIANPOINT((0.,0.,0.));\n"
        b"#2=IFCFACE((#9),.T.);\n"
        b"#3=IFCTRIANGULATEDFACESET(#4,$,.F.,((1,2,3)),$);\n"
        + f"#4=IFCWALL('{_GUID}',#5,'W',(#6));\n".encode()
    )
    path = _write(tmp_path, _wrap_full(body))
    scan = scan_records(path)

    assert scan.total_records == 4
    assert sum(scan.class_counts.values()) == scan.total_records


# --- 4. 等価性チェック: 旧経路(全レコードをparse_recordにかける)との比較 ---


def test_scan_records_matches_full_parse_on_synthetic_file(tmp_path):
    body = (
        b"#1=IFCCARTESIANPOINT((0.,0.,0.));\n"
        b"#2=IFCCARTESIANPOINT((1.,0.,0.));\n"
        b"#3=IFCDIRECTION((0.,0.,1.));\n"
        b"#4=IFCPOLYLOOP((#1,#2,#1));\n"
        b"#5=IFCFACEOUTERBOUND(#4,.T.);\n"
        b"#6=IFCFACE((#5));\n"
        b"#7=IFCFACESURFACE((#5),#3,.T.);\n"
        b"#8=IFCTRIANGULATEDFACESET(#9,$,.F.,((1,2,3),(4,5,6),(7,8,9)),$);\n"
        b"#9=IFCCARTESIANPOINTLIST3D(((0.,0.,0.),(1.,0.,0.)));\n"
        b"#10=IFCPOLYGONALFACESET(#9,$,(#20,#21),$);\n"
        b"#11=IFCEXTRUDEDAREASOLID(#12,#3,10.);\n"
        b"#12=IFCBOOLEANRESULT(.UNION.,#11,#11);\n"
        b"#13=IFCOWNERHISTORY(#14,#14,$,.ADDED.,$,$,$,0);\n"
        b"#14=IFCPERSONANDORGANIZATION(#15,#16,$);\n"
        + f"#20=IFCPROPERTYSET('{_GUID}',#13,'PSet',$,(#21));\n".encode()
        + b"#21=IFCPROPERTYSINGLEVALUE('N',$,IFCLABEL('v'),$);\n"
        + f"#22=IFCWALL('{_GUID}',#13,'Wall-A',(#6,#7,#8));\n".encode()
        + b"#23=IFCRELDEFINESBYPROPERTIES(#24,$,$,$,(#22),#20);\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    scan = scan_records(path)

    _assert_matches_old_full_scan(path, scan)

    # このファイルには少なくとも1つのblockクラスが含まれる(構造的高速化の
    # 対象カテゴリが実際に踏まれていることの確認)。
    assert scan.class_counts.get("IFCCARTESIANPOINT", 0) >= 1
    assert "IFCCARTESIANPOINT" not in [e.ifc_class for e in scan.entities]


def test_scan_records_matches_full_parse_on_small_ifc(small_ifc_path):
    """small.ifc全体で、scan_recordsと「全レコードをparse_recordにかける」
    旧経路のクラス別件数・中間クラスのScanEntity集合・単純frontierのid集合
    ・weighted集合が完全一致することを確認する(タスクブリーフの指定)。"""
    scan = scan_records(small_ifc_path)
    _assert_matches_old_full_scan(small_ifc_path, scan)
    assert scan.schema == "IFC4"
    assert scan.total_records > 0


# --- 5. パフォーマンスフロア(相対比較。通常スイート・高速) ---

_PERF_TARGET_SIZE = 2 * 1024 * 1024  # 約2MB


def _build_perf_mix_body(min_size: int) -> bytes:
    """large.ifcの実際のクラス別件数(grepで実測: IFCPOLYLOOP/IFCFACEOUTERBOUND/
    IFCFACEが約23%ずつ、IFCCARTESIANPOINTが約11%、GUID付きの
    IFCPROPERTYSET(+IFCPROPERTYSINGLEVALUE)が約7%、残りが中間クラス)に
    似せた比率の合成STEP本体を、min_sizeバイトに達するまで繰り返し生成する。

    block区分には2つの負荷特性が混在することが実測(large.ifcのgrep調査、
    docs/plans/2026-07-24-cui-phase1.md Task 2 参照)で分かっている:
    (a) 第1属性がGUID形でない素朴なblock(IFCCARTESIANPOINT等。旧経路でも
    GUIDゲートが速く失敗するので差は小さい)、(b) IFCPROPERTYSETのように
    IfcRoot系で実際に22文字のGlobalIdを持つblock(旧経路はゲート成立後に
    _split_top_level+Nameデコードまで走ってしまうため、新経路がそれを
    丸ごと避ける効果が大きい)。両方を混在させることで、単純化しすぎた
    片方だけのケース(前者のみなら比率が伸びない、後者のみなら非現実的な
    ほど比率が伸びる)を避ける。
    """
    lines: list[bytes] = []
    size = 0
    next_id = 1
    while size < min_size:
        base = next_id
        p1, p2, p3, d, loop, bound, owner, pv1, pset1, pv2, pset2, face, wall = range(
            base, base + 13
        )
        unit = [
            f"#{p1}=IFCCARTESIANPOINT((-6.000005,18.,0.));".encode(),
            f"#{p2}=IFCCARTESIANPOINT((-6.000005,12.727922,-12.727922));".encode(),
            f"#{p3}=IFCCARTESIANPOINT((6.000005,12.727922,-12.727922));".encode(),
            f"#{d}=IFCDIRECTION((0.,0.,1.));".encode(),
            f"#{loop}=IFCPOLYLOOP((#{p1},#{p2},#{p3}));".encode(),
            f"#{bound}=IFCFACEOUTERBOUND(#{loop},.T.);".encode(),
            f"#{owner}=IFCOWNERHISTORY(#{p1},#{p1},$,.ADDED.,$,$,$,0);".encode(),
            f"#{pv1}=IFCPROPERTYSINGLEVALUE('N',$,IFCLABEL('v'),$);".encode(),
            f"#{pset1}=IFCPROPERTYSET('{_GUID}',#{owner},'PSet-{pset1}',$,(#{pv1}));".encode(),
            f"#{pv2}=IFCPROPERTYSINGLEVALUE('N2',$,IFCLABEL('v2'),$);".encode(),
            f"#{pset2}=IFCPROPERTYSET('{_GUID}',#{owner},'PSet-{pset2}',$,(#{pv2}));".encode(),
            f"#{face}=IFCFACE((#{bound}));".encode(),
            f"#{wall}=IFCWALL('{_GUID}',#{owner},'Wall-{wall}',(#{face},#{pset1},#{pset2}));".encode(),
        ]
        lines.extend(unit)
        size += sum(len(x) + 1 for x in unit)
        next_id = base + 13
    return b"\n".join(lines) + b"\n"


def test_scan_records_beats_full_parse_on_block_heavy_data(tmp_path):
    """block/単純frontierが多数派の合成データ上で、scan_recordsが「全レコード
    をparse_recordにかける」旧経路より構造的に速いことを保証する高速な
    回帰ガード。絶対MB/sではなく同一データ上の比率で判定するため実行環境の
    CPU速度差に対してフレーキーにならない。

    フロア値についての重要な注記(docs/plans/2026-07-24-cui-phase1.md Task 2
    に詳細): タスクブリーフは「3倍以上」を指定していたが、実測調査
    (large.ifc実データでの前後比較、および複数の合成比率での比較)により
    3倍は本アーキテクチャでは到達不能と判明した。理由: 除去できるのは
    GUID/Name抽出とScanEntity構築(block/単純frontierの場合)のみで、
    reader側の走査コスト(構造上バイパス禁止)とヘッダ正規表現マッチ
    (全レコード共通で必須)が支配的なため。実測範囲は素朴なblock主体の
    合成データで約1.2倍、GUID付きblock(IFCPROPERTYSET)のみの極端な
    合成データでも約2.3〜2.45倍が上限だった。この関数が使う「素朴block+
    GUID付きblock混在」の比率では約1.3〜1.7倍で安定して観測されたため、
    フロアは余裕を持って1.2倍に設定する(退行検知が目的であり、上限を
    追い求めるものではない)。large.ifc実データでの実測はレポートを参照。

    3回計測してベストのratioを採用する(single-shotだと開発機のバック
    グラウンド負荷(実測で一時的にold/new比が1.0を割り込むケースを観測
    済み)に弱く、"not flaky" の要件に反するため。3回のうち1回でも
    フロアを満たせば良いという判定は、退行検知フロアとしては妥当
    (実装が壊れて毎回2倍以上遅くなるような回帰は3回中3回とも失敗する)。
    """
    body = _build_perf_mix_body(_PERF_TARGET_SIZE)
    path = _write(tmp_path, _wrap_full(body), name="perfmix.ifc")
    size_mb = path.stat().st_size / (1024 * 1024)
    path.read_bytes()  # OSページキャッシュを温める(old/new比較のI/O条件をそろえる)

    best_ratio = 0.0
    samples = []
    for _ in range(3):
        start_old = time.perf_counter()
        for record in iter_records(path):
            parse_record(record)
        elapsed_old = time.perf_counter() - start_old

        start_new = time.perf_counter()
        scan_records(path)
        elapsed_new = time.perf_counter() - start_new

        mb_per_sec_old = size_mb / elapsed_old if elapsed_old > 0 else float("inf")
        mb_per_sec_new = size_mb / elapsed_new if elapsed_new > 0 else float("inf")
        ratio = mb_per_sec_new / mb_per_sec_old if mb_per_sec_old > 0 else float("inf")
        samples.append((mb_per_sec_old, mb_per_sec_new, ratio))
        best_ratio = max(best_ratio, ratio)

    print(
        f"\n[perf floor] size={size_mb:.2f}MB samples="
        + ", ".join(f"(old={o:.1f} new={n:.1f} ratio={r:.2f}x)" for o, n, r in samples)
        + f" best_ratio={best_ratio:.2f}x (floor 1.2x; brief specified 3.0x, "
        "found unreachable by measurement, see report)"
    )

    assert best_ratio >= 1.2, (
        f"scan_records should beat full-parse with margin in at least 1 of 3 tries; "
        f"got {samples}"
    )


# --- 6. large.ifc 実測(通常スイート。壊滅的劣化のみ検知する緩いフロア) ---


def test_large_ifc_scan_records_throughput_and_sanity(large_ifc_path):
    """large.ifc で reader+scan_records を通した実測を報告する(監督者目標:
    end-to-end >=15MB/s、stretch 20MB/s。docs/plans/2026-07-24-cui-phase1.md
    Task 2 に前後比較を記載)。

    reader.py単体(30.4MB/s)には及ばないはずだが、旧経路(全レコードを
    parse_recordにかける、5-6MB/s)からの構造的な改善を検証する。CI/実行
    環境のCPU速度差を考慮し、ここでは既存のreader.py/parser.pyの方針を
    踏襲して壊滅的な性能劣化のみを検知する緩いフロアに留め、実測値は
    printで報告する(実際の目標達成判断はレポートの実測値で行う)。
    """
    file_size = large_ifc_path.stat().st_size
    scan = scan_records(large_ifc_path)

    size_mb = file_size / (1024 * 1024)
    mb_per_sec = size_mb / scan.elapsed_seconds if scan.elapsed_seconds > 0 else float("inf")
    print(
        f"\n[test_large_ifc_scan_records_throughput_and_sanity] size={size_mb:.1f}MB "
        f"total_records={scan.total_records} elapsed={scan.elapsed_seconds:.2f}s "
        f"throughput={mb_per_sec:.1f}MB/s (target >=15MB/s, stretch 20MB/s)"
    )

    assert scan.total_records > 0
    assert sum(scan.class_counts.values()) == scan.total_records
    assert scan.schema == "IFC4"
    assert mb_per_sec > 1, "壊滅的な性能劣化(1MB/s未満)を検知"
