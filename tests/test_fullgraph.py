"""scan/fullgraph.py(全レコード参照グラフ)のTDD (cui-design.md §8予告、
docs/plans/2026-07-25-cui-phase3.md Task 1)。

`scan_full_graph(path) -> FullGraph` は reader.iter_records を再利用し、
DATAセクションの**全レコード**(block/frontier/intermediateの分類をしない。
scan_records/parser.pyが行う構造的高速化——block/単純frontierのrefsを
捨てる——とは別物の契約)から「クラス名 + 文字列ブランク後の `#\\d+` 全参照
(重複除去なし・自己参照も保持)」だけを抽出し、CSR(numpy)形の参照グラフに
組む。Phase 3(テキストレベル削除)の土台であり、この段では削除計画そのもの
は作らない。GUID/Name/属性の意味解釈は行わない(parser.pyのフル解釈より軽い)。

検証する契約(タスクブリーフ Step 1 (a)-(e)):
  (a) 参照が CSR(ref_indptr/ref_targets)に正しく入る。ids は昇順ソート
      済みで、CSRの各行はids と同じ並びに整列する(ファイル中の記述順とは
      独立)。
  (b) 文字列リテラル内の `#123` のような見た目は参照と誤認しない
      (parser.py の `_extract_refs`/`_blank_strings` をそのまま再利用)。
  (c) 解決不能な参照(存在しないid)は -1(aggregate.py `_build_graph` の
      searchsorted+clamp+等値チェックと同型のミスガードパターン。実装側の
      docstring参照)。
  (d) in_degree は解決済み参照の**出現回数**(重複除去なし)。Task 2の
      カウントダウンが `alive_ref_count = in_degree.copy()` から出発して
      dead レコードの各参照出現を1つずつ減算する設計と整合させるため、
      ユニーク参照元数ではなく生の出現回数と正確に一致する必要がある。
  (e) class_table(クラス種類数のみ=有界のインターン表・重複なし)と
      class_codes(ids に整列)の対応。

加えて、parser.py/scan_records とは異なり**frontier/block クラスも refs を
保持する**ことを固定する(全レコード参照グラフという別の契約であることの
回帰ガード)。自己参照・同一レコード内の重複参照・壊れたレコードの除外も
個別に固定する。

最後に、small.ifc で record_count とクラス別件数が scan_records の
class_counts と完全一致することを統合テストで確認する(タスクブリーフ
Step 3)。
"""

from __future__ import annotations

import numpy as np

from ifc_occam.scan.fullgraph import FullGraph, scan_full_graph
from ifc_occam.scan.pipeline import scan_records


# --- テスト用ヘルパー(tests/test_pipeline.py の _write/_wrap_full と同型) ---


def _write(tmp_path, content: bytes, name: str = "model.ifc"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _wrap_full(body: bytes, schema: str = "IFC4") -> bytes:
    """HEADER付きの完全なSTEPファイル形でラップする。"""
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


def _idx(graph: FullGraph, record_id: int) -> int:
    """graph.ids 上で record_id が位置するindexを返す(1件のみ存在する前提)。"""
    matches = np.nonzero(graph.ids == record_id)[0]
    assert len(matches) == 1, f"expected exactly one match for id={record_id}, got {matches}"
    return int(matches[0])


def _resolved_target_ids(graph: FullGraph, record_id: int) -> list[int]:
    """record_id の行の ref_targets を、ids上のindexではなく元のrecord id
    表現に変換して返す(可読性のため。解決不能(-1)はそのまま-1で返す)。"""
    idx = _idx(graph, record_id)
    start, end = int(graph.ref_indptr[idx]), int(graph.ref_indptr[idx + 1])
    return [int(graph.ids[t]) if t != -1 else -1 for t in graph.ref_targets[start:end]]


def _class_of(graph: FullGraph, record_id: int) -> str:
    idx = _idx(graph, record_id)
    return graph.class_table[int(graph.class_codes[idx])]


# --- (a) CSR構築 + ids昇順ソート(ファイル記述順とは独立) ---


def test_ids_sorted_ascending_with_refs_correctly_placed_in_csr_rows(tmp_path):
    """ファイル中の記述順が id の昇順と無関係でも、ids は昇順ソートされ、
    各レコードの参照(ref_indptr/ref_targets の対応する行)はソート後の
    行位置に正しく整列する。"""
    body = (
        b"#30=IFCTESTREL(#10,#20);\n"
        b"#10=IFCTESTLEAF();\n"
        b"#20=IFCTESTLEAF();\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert graph.ids.tolist() == [10, 20, 30]
    assert graph.record_count == 3
    assert _resolved_target_ids(graph, 30) == [10, 20]
    assert _resolved_target_ids(graph, 10) == []
    assert _resolved_target_ids(graph, 20) == []


# --- (b) 文字列リテラル内の #n を参照と誤認しない ---


def test_ref_lookalike_inside_string_literal_is_not_extracted(tmp_path):
    body = (
        b"#1=IFCTESTREL('see #123 and #456 in string',#2);\n"
        b"#2=IFCTESTLEAF();\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert _resolved_target_ids(graph, 1) == [2]


# --- (c) 解決不能な参照は -1 ---


def test_unresolvable_reference_resolves_to_negative_one(tmp_path):
    body = b"#1=IFCTESTREL(#999);\n"  # #999は存在しない
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    idx = _idx(graph, 1)
    start, end = int(graph.ref_indptr[idx]), int(graph.ref_indptr[idx + 1])
    assert graph.ref_targets[start:end].tolist() == [-1]


# --- (d) in_degreeは解決済み参照の出現回数(未解決は無視) ---


def test_in_degree_counts_resolved_incoming_references_only(tmp_path):
    body = (
        b"#1=IFCTESTREL(#10,#999);\n"  # #10は解決、#999は未解決
        b"#2=IFCTESTREL(#10);\n"
        b"#10=IFCTESTLEAF();\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert int(graph.in_degree[_idx(graph, 10)]) == 2
    assert int(graph.in_degree[_idx(graph, 1)]) == 0
    assert int(graph.in_degree[_idx(graph, 2)]) == 0


# --- (e) class_table(重複なしインターン表)とclass_codesの対応 ---


def test_class_table_and_class_codes_correspond_to_record_classes(tmp_path):
    body = (
        b"#1=IFCWALL(#2,#3);\n"
        b"#2=IFCOWNERHISTORY($,$);\n"
        b"#3=IFCWALL($,$);\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert sorted(graph.class_table) == ["IFCOWNERHISTORY", "IFCWALL"]
    assert len(graph.class_table) == len(set(graph.class_table))  # インターン(重複なし)
    assert _class_of(graph, 1) == "IFCWALL"
    assert _class_of(graph, 2) == "IFCOWNERHISTORY"
    assert _class_of(graph, 3) == "IFCWALL"


# --- 自己参照は特別扱いせず、自分自身の行に解決したまま保持する ---


def test_self_reference_is_kept_and_resolves_to_its_own_row(tmp_path):
    """自己参照(#10=...(#10))は参照として保持し、自分自身の行に解決する
    (後段のカウントダウンが自然に処理できるよう、特別扱いしない)。"""
    body = b"#10=IFCTESTREL(#10);\n"
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    idx = _idx(graph, 10)
    start, end = int(graph.ref_indptr[idx]), int(graph.ref_indptr[idx + 1])
    assert graph.ref_targets[start:end].tolist() == [idx]
    assert int(graph.in_degree[idx]) == 1


# --- 同一レコード内の重複参照は除去しない ---


def test_duplicate_reference_within_one_record_is_not_deduplicated(tmp_path):
    """同じ参照が1レコード内に複数回現れても除去しない(出現回数どおりに
    CSR/in_degreeへ反映する。Task 2 のカウントダウンが
    alive_ref_count=in_degree.copy() から出発し、dead レコードの各出現を
    1つずつ減算する設計との整合を保つための固定)。"""
    body = (
        b"#1=IFCTESTREL(#2,#2,#3);\n"
        b"#2=IFCTESTLEAF();\n"
        b"#3=IFCTESTLEAF();\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert _resolved_target_ids(graph, 1) == [2, 2, 3]
    assert int(graph.in_degree[_idx(graph, 2)]) == 2
    assert int(graph.in_degree[_idx(graph, 3)]) == 1


# --- block/frontierクラスもrefsを保持する(scan_recordsとの契約差の回帰ガード) ---


def test_refs_extracted_for_block_classified_class_unlike_scan_records(tmp_path):
    """IFCOWNERHISTORYはparser.pyのブロック分類でrefsを持たない扱いだが、
    fullgraphは全レコード一律でrefsを抽出する(scan_records/aggregate.pyの
    「block/単純frontierはrefsを捨てる」構造的高速化とは別の契約であること
    の回帰ガード)。"""
    body = (
        b"#1=IFCOWNERHISTORY(#2,#3,$,.ADDED.,$,$,$,0);\n"
        b"#2=IFCPERSON();\n"
        b"#3=IFCPERSON();\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert _resolved_target_ids(graph, 1) == [2, 3]


def test_refs_extracted_for_frontier_classified_class_unlike_scan_records(tmp_path):
    """IFCFACEはparser.pyのfrontier分類でweight=1のみを持ちrefsを格納しない
    扱いだが、fullgraphは全レコード一律でrefsを抽出する。"""
    body = (
        b"#1=IFCFACEOUTERBOUND(#2,.T.);\n"
        b"#2=IFCPOLYLOOP(());\n"
        b"#3=IFCFACE((#1));\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert _resolved_target_ids(graph, 3) == [1]


# --- 壊れたレコードの除外 ---


def test_malformed_record_is_excluded_from_the_graph(tmp_path):
    """壊れたレコード(閉じ括弧が無い等、_match_headerがNoneを返す基準と
    同じ)はids/class_table/refsのどこにも現れない(scan_records/parse_record
    と同じ「静かに読み飛ばす」規約)。"""
    body = (
        b"#1=IFCTESTLEAF();\n"
        b"#2=IFCBROKEN(#1,#3;\n"
        b"#3=IFCTESTLEAF();\n"
    )
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert graph.record_count == 2
    assert graph.ids.tolist() == [1, 3]
    assert "IFCBROKEN" not in graph.class_table


# --- 契約: dtype/shape ---


def test_field_dtypes_and_shapes_match_the_contract(tmp_path):
    body = b"#1=IFCTESTLEAF();\n#2=IFCTESTREL(#1);\n"
    path = _write(tmp_path, _wrap_full(body))
    graph = scan_full_graph(path)

    assert graph.ids.dtype == np.int64
    assert graph.class_codes.dtype == np.int32
    assert graph.ref_indptr.dtype == np.int64
    assert graph.ref_targets.dtype == np.int64
    assert graph.in_degree.dtype == np.int64
    assert graph.ids.shape == (graph.record_count,)
    assert graph.class_codes.shape == (graph.record_count,)
    assert graph.in_degree.shape == (graph.record_count,)
    assert graph.ref_indptr.shape == (graph.record_count + 1,)
    assert graph.ref_targets.shape == (int(graph.ref_indptr[-1]),)


# --- 空DATAセクション ---


def test_empty_data_section_produces_an_empty_graph_without_crashing(tmp_path):
    path = _write(tmp_path, _wrap_full(b""))
    graph = scan_full_graph(path)

    assert graph.record_count == 0
    assert graph.ids.tolist() == []
    assert graph.class_table == []
    assert graph.ref_indptr.tolist() == [0]
    assert graph.ref_targets.tolist() == []
    assert graph.in_degree.tolist() == []


# --- 統合: small.ifc で scan_records との一致確認(タスクブリーフ Step 3) ---


def test_small_ifc_record_count_and_class_counts_match_scan_records(small_ifc_path):
    """small.ifc全体で、record_countとクラス別件数がscan_recordsの
    class_countsと完全一致することを確認する。"""
    graph = scan_full_graph(small_ifc_path)
    raw = scan_records(small_ifc_path)

    assert graph.record_count == raw.total_records
    assert graph.record_count == len(graph.ids)

    counts: dict[str, int] = {}
    codes, freq = np.unique(graph.class_codes, return_counts=True)
    for code, count in zip(codes, freq):
        counts[graph.class_table[int(code)]] = int(count)

    assert counts == raw.class_counts

    # 整合性アサーション(cui-phase3 Task 2 同乗の小修正): 解決済み参照の
    # 総数と in_degree の総和が一致する(並べ替えロジック・参照解決ロジック
    # の大規模回帰検知網——一方だけがズレるとこの等式が崩れる)。
    assert graph.in_degree.sum() == int((graph.ref_targets >= 0).sum())
