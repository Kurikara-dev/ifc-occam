"""scan/aggregate.py(参照グラフ集計)のTDD (cui-design.md §4、docs/plans/2026-07-24-cui-phase1.md Task 3)。

合成 RawScan フィクスチャで以下を担保する:
  - 製品同定: IFCPRODUCTDEFINITIONSHAPE を参照するエンティティ=製品。
  - 重み伝播: 共有形状を2製品が参照 → expanded は2倍・unique は1倍。
  - ダイヤモンド参照(1つの製品自身の到達集合内での合流)は二重加算しない。
  - 循環参照は無限ループも二重加算もしない(訪問済みマークで安全に終端)。
  - 到達不能な参照(blockクラス等でRawScanに存在しないid)は重み0として無害に解決。
  - proxy_names(IFCBUILDINGELEMENTPROXYのName頻度上位20)。
  - parametric_count(製品の到達集合にパラメトリック名目重みを含むか)。
  - elements(GlobalId列)・est_fullopen_bytes・stats の expanded降順ソート。

続く節で small.ifc / large.ifc の統合テスト(GUI版 diagnose との順位相関)を行う。
"""

from __future__ import annotations

from array import array

import numpy as np
import pytest

from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.extract import extract_model
from ifc_occam.scan.aggregate import (
    FULLOPEN_BYTES_MULTIPLIER,
    ClassScanStats,
    ScanResult,
    aggregate_scan,
    scan_file,
)
from ifc_occam.scan.parser import ScanEntity
from ifc_occam.scan.pipeline import RawScan, scan_records

_GUID = "2Occ4mT3stGu1d$_synth0"  # 22文字のGUID形(test_parser.pyの合成値と同じ形。実データ由来ではない)


# --- テスト用ヘルパー ---


def _entity(entity_id, ifc_class, refs=(), global_id=None, name=None) -> ScanEntity:
    return ScanEntity(
        entity_id=entity_id,
        ifc_class=ifc_class,
        refs=tuple(refs),
        weight=0,
        is_parametric=False,
        global_id=global_id,
        name=name,
    )


def _raw(entities=(), face_ids=(), weighted=(), schema="IFC4", elapsed_seconds=0.25) -> RawScan:
    entities = list(entities)
    weighted = list(weighted)
    face_ids_arr = array("q", face_ids)
    total_records = len(entities) + len(face_ids_arr) + len(weighted)
    return RawScan(
        class_counts={},  # aggregate.py はこのフィールドを使わない(pipeline.pyの責務と分離)
        face_ids=face_ids_arr,
        weighted=weighted,
        entities=entities,
        schema=schema,
        total_records=total_records,
        elapsed_seconds=elapsed_seconds,
    )


def _stats_by_class(result: ScanResult) -> dict[str, ClassScanStats]:
    return {s.ifc_class: s for s in result.stats}


# --- 1. 製品同定 ---


def test_entity_referencing_pds_is_identified_as_product():
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2,), global_id=_GUID),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert {s.ifc_class for s in result.stats} == {"IFCWALL"}
    assert _stats_by_class(result)["IFCWALL"].element_count == 1


def test_entity_not_referencing_pds_is_not_a_product():
    """PDS を参照しないエンティティ(製品を参照するだけの関係エンティティ等)は
    製品として数えない。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2,), global_id=_GUID),
            _entity(3, "IFCRELDEFINESBYPROPERTIES", refs=(1,)),  # 製品(1)を参照するが PDS は参照しない
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert {s.ifc_class for s in result.stats} == {"IFCWALL"}


def test_grid_like_class_referencing_pds_via_any_attribute_is_still_a_product():
    """cui-design.md §4: 'Grid の FootPrint 等も自然に入る' — PDS参照さえあれば
    製品名がGridのような非典型クラスでも同定される(スキーマ表を持たない設計)。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCGRID", refs=(2, 99), global_id=_GUID),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert {s.ifc_class for s in result.stats} == {"IFCGRID"}


# --- 2. 重み伝播: 基本(単純フロンティア2つ) ---


def test_single_product_with_two_simple_faces_counts_both_in_expanded_and_unique():
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 10, 11), global_id=_GUID),
        ],
        face_ids=[10, 11],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.element_count == 1
    assert stats.est_faces_expanded == 2
    assert stats.est_faces_unique == 2
    assert stats.parametric_count == 0


def test_tessellated_and_parametric_weighted_leaves_contribute_their_weight():
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 20, 21), global_id=_GUID),
        ],
        weighted=[(20, 5, False), (21, 16, True)],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.est_faces_expanded == 21  # 5 + 16
    assert stats.parametric_count == 1  # 到達集合に is_parametric=True の要素を含む


# --- 3. 共有形状を2製品が参照 → expanded は2倍・unique は1倍 ---


def test_shape_shared_by_two_products_doubles_expanded_but_not_unique():
    shared = _entity(100, "IFCSHAPEREPRESENTATION", refs=(200,))
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(12, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 100), global_id=_GUID),
            _entity(11, "IFCWALL", refs=(12, 100), global_id=_GUID),
            shared,
        ],
        face_ids=[200],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.element_count == 2
    assert stats.est_faces_expanded == 2  # 共有フェースが製品ごとに1回ずつ = 2
    assert stats.est_faces_unique == 1  # 共有フェースは初回訪問(製品1)のみ数える


def test_shared_shape_unique_attribution_is_first_product_by_ascending_entity_id():
    """unique の帰属は「初回訪問時のみ数える」を昇順entity_idの決定的順序で行う
    (先に処理される製品=id昇順で先の方が『勝つ』)。"""
    shared = _entity(100, "IFCSHAPEREPRESENTATION", refs=(200,))
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(12, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(11, "IFCWALL", refs=(12, 100), global_id=_GUID),
            _entity(1, "IFCWALL", refs=(2, 100), global_id=_GUID),
            shared,
        ],
        face_ids=[200],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    # 個々の製品別内訳はScanResultで直接見えないため、クラス集計値(2倍/1倍)自体で
    # 帰属ロジックの健全性を確認する(このテストは主にidの昇順ソート自体の回帰ガード)。
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.est_faces_expanded == 2
    assert stats.est_faces_unique == 1


# --- 4. ダイヤモンド参照(1製品内での合流)は二重加算しない ---


def test_diamond_within_single_product_does_not_double_count_in_expanded():
    """製品(1) → コンテナ(3) → {A(4), B(5)} → 共有フェース(6) という合流構造。
    A/B の2経路経由でも共有フェースは1回だけ数える(expanded・unique とも)。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 3), global_id=_GUID),
            _entity(3, "IFCSHAPEREPRESENTATION", refs=(4, 5)),
            _entity(4, "IFCFACETEDBREP", refs=(6,)),
            _entity(5, "IFCFACETEDBREP", refs=(6,)),
        ],
        face_ids=[6],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.est_faces_expanded == 1  # 2ではない
    assert stats.est_faces_unique == 1


def test_duplicate_ref_within_single_entity_refs_tuple_does_not_double_count():
    """1エンティティの refs タプル自体に同じidが重複して現れるケース(parserは
    重複除去しない設計、docs/plans/2026-07-24-cui-phase1.md Task 2 参照)。aggregate側でdedupする。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 10, 10, 10), global_id=_GUID),
        ],
        face_ids=[10],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.est_faces_expanded == 1
    assert stats.est_faces_unique == 1


# --- 5. 循環参照は無限ループも二重加算もしない ---


def test_cycle_terminates_and_still_counts_weight_reachable_outside_the_cycle():
    """3⇄4 の循環に加え、4からFace(5)への到達も持つ構造。無限ループせず、
    循環部分の重みは0扱い(そこには何も重みがないため)、Face(5)は正しく1回だけ数える。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 3), global_id=_GUID),
            _entity(3, "IFCCYCLENODE", refs=(4,)),
            _entity(4, "IFCCYCLENODE", refs=(3, 5)),  # 3への循環 + Face(5)への到達
        ],
        face_ids=[5],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.est_faces_expanded == 1
    assert stats.est_faces_unique == 1


def test_self_referencing_entity_does_not_hang():
    """エンティティが自分自身を参照する退化ケース。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 3), global_id=_GUID),
            _entity(3, "IFCCYCLENODE", refs=(3, 5)),
        ],
        face_ids=[5],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.est_faces_expanded == 1


# --- 6. 到達不能な参照(absent id)は重み0として無害に解決 ---


def test_ref_to_absent_id_resolves_to_zero_without_crashing():
    """blockクラス等でRawScanに全く現れないidへの参照(設計上の意図的な欠落)は
    重み0として扱い、例外を出さない。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 9999), global_id=_GUID),  # 9999はどこにも存在しない
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.est_faces_expanded == 0
    assert stats.est_faces_unique == 0


# --- 7. proxy_names ---


def test_proxy_names_counts_frequencies_among_proxy_products_only():
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="Bolt"),
            _entity(3, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="Bolt"),
            _entity(4, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="Nut"),
            _entity(5, "IFCWALL", refs=(2,), global_id=_GUID, name="Bolt"),  # 他クラスは無視
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert result.proxy_names == [("Bolt", 2), ("Nut", 1)]


def test_proxy_names_skips_none_name_and_truncates_to_top_20():
    entities = [_entity(2, "IFCPRODUCTDEFINITIONSHAPE")]
    for i in range(25):
        entities.append(
            _entity(100 + i, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name=f"Proxy{i}")
        )
    entities.append(
        _entity(999, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name=None)
    )
    raw = _raw(entities=entities)
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert len(result.proxy_names) == 20
    assert all(count == 1 for _, count in result.proxy_names)
    names = {n for n, _ in result.proxy_names}
    assert None not in names
    # 昇順entity_id順で先着した最初の20件(Proxy0..Proxy19)が残る
    assert names == {f"Proxy{i}" for i in range(20)}


# --- 7b. proxy_names: タグ接頭辞集計 (docs/plans/2026-07-25-cui-phase2.md Task 3) ---
#
# 背景(Task 8実測、docs/cui-measurements.md「Task 8」章・「5. 鉄骨ファブ系の所見」):
# 実データのproxy Nameは連番付き(例「【曲折円柱】曲折円柱 (1903)」)で、素朴な
# Name完全一致の頻度集計では上位20件が全てcount=1になり判断材料として無力。
# 一方、Name先頭の「【カテゴリ】」タグ接頭辞だけを抜き出して集計すると
# 100%同一タグに束ねられることを実証済み(mini: 456/456、small: 936/936)。
# `_compute_proxy_names` の集計キーをこのタグ接頭辞方式に変更する。


def test_proxy_names_groups_tag_prefixed_names_by_their_shared_prefix():
    """(a) 接頭辞付きName群は「【カテゴリ】」部分(括弧含む全体)に束ねられる。
    連番部分("ST-001"等)が異なっていても同一キーとして集計される。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="【手摺】ST-001"),
            _entity(3, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="【手摺】ST-002"),
            _entity(4, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="【手摺】ST-003"),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert result.proxy_names == [("【手摺】", 3)]


def test_proxy_names_without_tag_prefix_uses_full_name_as_key():
    """(b) 「【」始まりでないNameはこれまで通りName全体をキーにする。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="Bolt"),
            _entity(3, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="Bolt"),
            _entity(4, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="Nut"),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert result.proxy_names == [("Bolt", 2), ("Nut", 1)]


def test_proxy_names_mixes_tag_prefixed_and_plain_keys_independently():
    """(c) タグ接頭辞付きと接頭辞なしが混在しても、それぞれ独立に正しく集計される。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="【手摺】ST-001"),
            _entity(3, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="【手摺】ST-002"),
            _entity(4, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="【曲折円柱】(1)"),
            _entity(5, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="Bolt"),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    # count同数(1)の項目は entity_id 昇順で先着した方が先(Counter.most_common の
    # 「同数は最初に出現した順」という仕様、products は entity_id 昇順で処理される)。
    assert result.proxy_names == [
        ("【手摺】", 2),
        ("【曲折円柱】", 1),
        ("Bolt", 1),
    ]


def test_proxy_names_excludes_empty_and_none_names_even_with_tag_aggregation():
    """(d) 空文字列/None のNameは(タグ接頭辞集計に変わっても)既存挙動どおり
    数えない。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name="【手摺】ST-001"),
            _entity(3, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name=""),
            _entity(4, "IFCBUILDINGELEMENTPROXY", refs=(2,), global_id=_GUID, name=None),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert result.proxy_names == [("【手摺】", 1)]


# --- 7c. proxy_names: タグ接頭辞regexの境界(レビューア指摘) ---
#
# `_proxy_name_key`(正規表現マッチそのもの)と`_compute_proxy_names`(集計への
# 反映)の両方で、以下3つの境界を固定する:
#   (1) 空タグ(タグの中身が空文字列) → キーは括弧のみの「【】」。
#   (2) タグのみ・本文なし → タグ全体がそのままキー。
#   (3) 非対称括弧(閉じなし/開きなし) → regex不一致でName全体にフォールバック。
# 実装は変更せず、手動実行(scratch script)で下記の期待値を実際に確認済み。


def test_proxy_name_key_on_empty_tag_returns_bracket_pair_only_key():
    """(1) 空タグ『【】ST-001』と『【】』単体 → いずれもキーは括弧のみの
    『【】』になる(`_PROXY_TAG_PREFIX_RE`の`[^】]*`は空文字列にもマッチするため、
    タグの中身が空でもマッチ自体は成立する)。"""
    from ifc_occam.scan.aggregate import _proxy_name_key

    assert _proxy_name_key("【】ST-001") == "【】"
    assert _proxy_name_key("【】") == "【】"


def test_proxy_name_key_on_tag_only_with_no_body_returns_the_tag_itself():
    """(2) タグのみ・本文なし『【手摺】』→ 末尾に連番等の本文が無くてもマッチは
    成立し、タグ全体(括弧含む)がそのままキーになる。"""
    from ifc_occam.scan.aggregate import _proxy_name_key

    assert _proxy_name_key("【手摺】") == "【手摺】"


def test_proxy_name_key_on_asymmetric_brackets_falls_back_to_full_name():
    """(3) 非対称括弧 — 閉じ括弧なし『【手摺ST-001』・開き括弧なし
    『手摺】ST-001』→ いずれもregex不一致(前者は`】`が無いため`[^】]*】`が
    完結せず、後者は`^【`で始まらないため先頭マッチしない)となり、Name全体が
    そのままキーにフォールバックする。"""
    from ifc_occam.scan.aggregate import _proxy_name_key

    assert _proxy_name_key("【手摺ST-001") == "【手摺ST-001"
    assert _proxy_name_key("手摺】ST-001") == "手摺】ST-001"


def test_compute_proxy_names_boundary_regex_cases_are_aggregated_consistently():
    """`_compute_proxy_names`の直接呼び出しでも、上記`_proxy_name_key`の境界規則が
    集計結果にそのまま反映されることを確認する(`_Product`を直接構築し、
    aggregate_scan全体は経由しない最小構成)。空タグの2件(『【】ST-001』
    『【】』)は同じキー『【】』に束ねられ(count=2)、非対称括弧の2件はregex
    不一致のためそれぞれのName全体が別々のキーとして数えられる(count=1ずつ、
    順序はproducts引き渡し順=先に現れた方が先着というCounter.most_commonの
    仕様どおり)。"""
    from ifc_occam.scan.aggregate import _Product, _compute_proxy_names

    products = [
        _Product(raw_id=1, ifc_class="IFCBUILDINGELEMENTPROXY", global_id=_GUID, name="【】ST-001"),
        _Product(raw_id=2, ifc_class="IFCBUILDINGELEMENTPROXY", global_id=_GUID, name="【】"),
        _Product(raw_id=3, ifc_class="IFCBUILDINGELEMENTPROXY", global_id=_GUID, name="【手摺】"),
        _Product(raw_id=4, ifc_class="IFCBUILDINGELEMENTPROXY", global_id=_GUID, name="【手摺ST-001"),
        _Product(raw_id=5, ifc_class="IFCBUILDINGELEMENTPROXY", global_id=_GUID, name="手摺】ST-001"),
    ]
    result = _compute_proxy_names(products)
    assert result == [
        ("【】", 2),
        ("【手摺】", 1),
        ("【手摺ST-001", 1),
        ("手摺】ST-001", 1),
    ]


# --- 8. parametric_count ---


def test_parametric_count_counts_products_whose_reachable_set_has_a_parametric_leaf():
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(12, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 30), global_id=_GUID),  # パラメトリック経由
            _entity(11, "IFCWALL", refs=(12, 40), global_id=_GUID),  # 単純frontierのみ
        ],
        face_ids=[40],
        weighted=[(30, 16, True)],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.element_count == 2
    assert stats.parametric_count == 1


# --- 9. elements(GlobalId列) ---


def test_elements_maps_class_to_global_ids_in_ascending_entity_id_order():
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(20, "IFCWALL", refs=(2,), global_id="B" * 22),
            _entity(10, "IFCWALL", refs=(2,), global_id="A" * 22),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    assert result.elements == {"IFCWALL": ["A" * 22, "B" * 22]}


def test_product_without_global_id_is_excluded_from_elements_but_still_counted():
    """防御的挙動: global_id が None の製品(通常起こらないはずだが)は elements
    (apply用のGID列)には含めないが、element_count には数える(要素自体は存在)。"""
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2,), global_id=None),
            _entity(3, "IFCWALL", refs=(2,), global_id="C" * 22),
        ],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.element_count == 2
    assert result.elements == {"IFCWALL": ["C" * 22]}


# --- 10. stats の expanded 降順ソート ---


def test_stats_sorted_descending_by_est_faces_expanded():
    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2, 10), global_id=_GUID),
            _entity(3, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(4, "IFCDOOR", refs=(3, 11, 12, 13), global_id=_GUID),
            _entity(5, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(6, "IFCWINDOW", refs=(5,), global_id=_GUID),
        ],
        face_ids=[10, 11, 12, 13],
    )
    result = aggregate_scan(raw, path="m.ifc", file_size=100)
    expanded = [s.est_faces_expanded for s in result.stats]
    assert expanded == sorted(expanded, reverse=True)
    assert [s.ifc_class for s in result.stats] == ["IFCDOOR", "IFCWALL", "IFCWINDOW"]


# --- 11. est_fullopen_bytes ---


def test_est_fullopen_bytes_is_file_size_times_constant_multiplier():
    raw = _raw(entities=[])
    result = aggregate_scan(raw, path="m.ifc", file_size=12345)
    # Task 8実測により7→14に校正(docs/cui-measurements.md「Task 8」章、
    # ifc_occam/scan/aggregate.py の FULLOPEN_BYTES_MULTIPLIER コメント参照)。
    assert FULLOPEN_BYTES_MULTIPLIER == 14
    assert result.est_fullopen_bytes == 12345 * 14


# --- 12. ScanResult 直下フィールドの配線 ---


def test_top_level_fields_are_plumbed_from_inputs_and_rawscan():
    raw = _raw(entities=[], schema="IFC4X3", elapsed_seconds=1.5)
    result = aggregate_scan(raw, path="some/model.ifc", file_size=999)
    assert result.path == "some/model.ifc"
    assert result.file_size == 999
    assert result.schema == "IFC4X3"
    assert result.total_entities == raw.total_records
    assert result.scan_seconds == 1.5


# --- 13. 空スキャンのスモークテスト ---


def test_empty_scan_produces_empty_result_without_crashing():
    raw = _raw(entities=[], face_ids=[], weighted=[])
    result = aggregate_scan(raw, path="empty.ifc", file_size=0)
    assert result.stats == []
    assert result.proxy_names == []
    assert result.elements == {}
    assert result.total_entities == 0


# --- 14. scan_file: 実ファイル経由の薄いラッパー統合チェック ---


def test_scan_file_end_to_end_on_tiny_synthetic_file(tmp_path):
    content = (
        b"ISO-10303-21;\n"
        b"HEADER;\n"
        b"FILE_DESCRIPTION((''),'2;1');\n"
        b"FILE_NAME('','',(''),(''),'','','');\n"
        b"FILE_SCHEMA(('IFC4'));\n"
        b"ENDSEC;\n"
        b"DATA;\n"
        b"#2=IFCPRODUCTDEFINITIONSHAPE('','',(#3));\n"
        + f"#1=IFCWALL('{_GUID}',#9,'Wall-01',$,$,$,#2,$,$);\n".encode()
        + b"#3=IFCSHAPEREPRESENTATION(#8,'Body','Brep',(#4));\n"
        + b"#4=IFCFACE((#5));\n"
        + b"ENDSEC;\n"
    )
    path = tmp_path / "tiny.ifc"
    path.write_bytes(content)

    result = scan_file(path)

    assert result.schema == "IFC4"
    assert result.file_size == path.stat().st_size
    assert result.scan_seconds >= 0.0
    stats = _stats_by_class(result)["IFCWALL"]
    assert stats.element_count == 1
    assert stats.est_faces_expanded == 1
    assert result.elements == {"IFCWALL": [_GUID]}


# --- 15. 前段修正: raw_id → フルインデックス解決のベクトル化(dict排除) ---
#
# 旧 `_Graph.raw_id_to_full_index`(dict[int, int])はモジュールdocstring §1が
# 明示する「id→index の Python dict は一切構築しない」という設計規則に反しており、
# large.ifc で実測21.8MB消費していた(docs/plans/2026-07-24-cui-phase1.md Task 4 前段修正)。
# np.searchsorted一括解決(`_resolve_full_indices`)に置換する。
#
# 集計結果そのもの(数値の正しさ)は上記の全テストで既に担保されているため
# (置換は純リファクタで挙動を変えない)、ここでは公開API(aggregate_scan)経由では
# 再現できない内部契約だけを直接テストする: 製品は常に `raw.entities` から同定
# されるため(`_identify_products`)、正常経路では「製品のraw_idがグラフのid空間
# に存在しない」という状態は起こり得ない。この不変条件が破れた場合(将来の
# バグ)に、黒魔術的に無視せず旧dict実装と同じ `KeyError` で即座に失敗することを
# 固定する。


def test_resolving_unknown_product_raw_id_fails_loud():
    """不変条件(製品のraw_idは常にグラフのid空間に存在する)が破れた場合、
    旧dict実装のKeyError fail-loud挙動をベクトル化後も保つ。"""
    from ifc_occam.scan.aggregate import _build_graph, _resolve_full_indices

    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(1, "IFCWALL", refs=(2,), global_id=_GUID),
        ],
    )
    graph = _build_graph(raw)
    with pytest.raises(KeyError):
        _resolve_full_indices(np.array([9999], dtype=np.int64), graph)


def test_resolve_full_indices_resolves_known_raw_ids_to_their_own_position():
    """既知のentity_idについては、`graph.ids`上で実際にそのidが見つかる位置を
    返す(旧dict実装 `raw_id_to_full_index[raw_id]` と同じ意味の解決結果になる
    ことの回帰ガード。順序をシャッフルしても各要素が独立に正しく解決されること
    も確認する)。"""
    from ifc_occam.scan.aggregate import _build_graph, _resolve_full_indices

    raw = _raw(
        entities=[
            _entity(2, "IFCPRODUCTDEFINITIONSHAPE"),
            _entity(20, "IFCWALL", refs=(2,), global_id="B" * 22),
            _entity(10, "IFCDOOR", refs=(2,), global_id="A" * 22),
        ],
    )
    graph = _build_graph(raw)
    idx = _resolve_full_indices(np.array([20, 10, 2], dtype=np.int64), graph)
    assert graph.ids[idx[0]] == 20
    assert graph.ids[idx[1]] == 10
    assert graph.ids[idx[2]] == 2


# --- 統合テスト: small.ifc / large.ifc と GUI版 diagnose の順位相関 ---
#
# cui-design.md §4-4(検証手段)・docs/plans/2026-07-24-cui-phase1.md Task 3(Spearman rho >= 0.6 目安、
# 実測値を報告)。real側(ifcopenshellフルオープン+診断)は small.ifc で約21秒・
# large.ifcで約109秒かかる(事前実測)。ブリーフが明示的に許可する「session
# 単位でキャッシュ、または根拠付きで定数化」のうち、本実装はどちらも取らず
# 「1ファイルにつき1テスト関数内で1回だけ計算する」という最も単純な形で
# 重複計算を避けた(そもそも各ファイルにつき統合テストは1つしか無いため、
# フィクスチャ化してもキャッシュの恩恵はなく、複雑さを増すだけと判断)。
# 両ファイルとも通常スイート(@pytest.mark.slow 無し)に置く(ブリーフの明示指定)。


def _spearman_top5(real_stats, scan_result: ScanResult) -> tuple[float, list[str], list[int], list[int]]:
    """real_stats(diagnose.aggregate_by_classの出力, total_triangles降順)の上位5クラスに
    絞り、その5クラスの (real total_triangles) と (このスキャンのest_faces_expanded) の
    Spearman順位相関を返す。クラス名の突合は常にupper()で行う(大文字クラス名(スキャン層)
    と ifcopenshell のクラス名(混在ケース)の突合の規約、docs/plans/2026-07-24-cui-phase1.md
    Global Constraints)。scanに存在しないクラスは0扱い(スキャンの製品同定が見逃した場合、
    相関を悪化させる方向に働くため、テストの健全性を損なわない)。
    """
    from scipy.stats import spearmanr

    top5 = real_stats[:5]
    real_values = [s.total_triangles for s in top5]
    scan_by_class = {s.ifc_class: s.est_faces_expanded for s in scan_result.stats}
    classes = [s.ifc_class.upper() for s in top5]
    scan_values = [scan_by_class.get(cls, 0) for cls in classes]

    rho, _pvalue = spearmanr(real_values, scan_values)
    return float(rho), classes, real_values, scan_values


def test_small_ifc_expanded_ranking_correlates_with_real_triangle_ranking(small_ifc_path):
    model, _warnings = extract_model(small_ifc_path)
    real_stats = aggregate_by_class(model)

    scan_result = scan_file(small_ifc_path)

    rho, classes, real_values, scan_values = _spearman_top5(real_stats, scan_result)
    print(
        f"\n[test_small_ifc_expanded_ranking_correlates_with_real_triangle_ranking] "
        f"top5={classes} real_triangles={real_values} est_faces_expanded={scan_values} "
        f"rho={rho:.3f} scan_seconds={scan_result.scan_seconds:.2f}"
    )

    assert rho >= 0.6, f"Spearman rho={rho:.3f} below 0.6 threshold; detail: {classes} {real_values} vs {scan_values}"


def test_large_ifc_expanded_ranking_correlates_with_real_triangle_ranking(large_ifc_path):
    model, _warnings = extract_model(large_ifc_path)
    real_stats = aggregate_by_class(model)

    scan_result = scan_file(large_ifc_path)

    rho, classes, real_values, scan_values = _spearman_top5(real_stats, scan_result)
    print(
        f"\n[test_large_ifc_expanded_ranking_correlates_with_real_triangle_ranking] "
        f"top5={classes} real_triangles={real_values} est_faces_expanded={scan_values} "
        f"rho={rho:.3f} scan_seconds={scan_result.scan_seconds:.2f}"
    )

    assert rho >= 0.6, f"Spearman rho={rho:.3f} below 0.6 threshold; detail: {classes} {real_values} vs {scan_values}"


def test_scan_records_and_aggregate_scan_agree_with_scan_file_wrapper(small_ifc_path):
    """scan_file(path) が scan_records+aggregate_scan を素朴に組み合わせた場合と
    同じ結果(scan_seconds以外)になることを確認する(ラッパーが薄いことの回帰ガード)。"""
    raw = scan_records(small_ifc_path)
    direct = aggregate_scan(raw, path=str(small_ifc_path), file_size=small_ifc_path.stat().st_size)
    wrapped = scan_file(small_ifc_path)

    assert wrapped.stats == direct.stats
    assert wrapped.proxy_names == direct.proxy_names
    assert wrapped.elements == direct.elements
    assert wrapped.est_fullopen_bytes == direct.est_fullopen_bytes
    assert wrapped.total_entities == direct.total_entities
