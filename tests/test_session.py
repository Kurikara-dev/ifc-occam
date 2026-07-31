"""cui/session.py(対話セッション・純粋ロジック層)のTDD (cui-design.md §6、docs/plans/2026-07-24-cui-phase1.md Task 4)。

`CuiSession` は軽量スキャン結果(`ScanResult`)を受け取り、対話コマンド文字列を
解釈してクラス単位の操作意図(`Intent`)を管理する。stdin/stdout・ファイルI/O・
ifcopenshell へのアクセスは一切行わない(それらは repl.py / core/ の責務)。

以下を担保する:
  - 各コマンドの解釈: delete/bbox/hull/decimate/keep/undo/list/rank。
  - 不明クラス → 前方一致候補の提示(エラーにはしても例外は投げない)。
  - クラス名は大文字小文字非区別(内部は常に upper() で突合)。
  - decimate の ratio 検証(0.05-0.95の範囲外・非数値を拒否)。
  - 同一クラスへの再指定は上書き(後勝ち)。keep は既存の操作指定の解除
    (=明示的な保持マーカーとして記録)として機能する。
  - to_operations(): Intent → core.ops.Operation。targets は scan.elements
    (GlobalId列)由来、bbox/hull/decimate の scope は intent.scope(既定
    "shared")、delete/keep は従来どおり "element"
    (docs/plans/2026-07-31-cui-shared-scope.md)。
  - render_intents()/render_ranking(): 主要な数値(要素数・推定Face数等)を含む。
"""

from __future__ import annotations

import pytest

from ifc_occam.core.ops import Operation
from ifc_occam.cui.session import CuiSession, Intent
from ifc_occam.scan.aggregate import ClassScanStats, ScanResult

# --- テスト用ヘルパー ---


def _stats(
    ifc_class: str, element_count: int, expanded: int = 0, unique: int = 0, parametric: int = 0
) -> ClassScanStats:
    return ClassScanStats(
        ifc_class=ifc_class,
        element_count=element_count,
        est_faces_expanded=expanded,
        est_faces_unique=unique,
        parametric_count=parametric,
    )


def _scan(stats, elements=None, **overrides) -> ScanResult:
    defaults = dict(
        path="model.ifc",
        file_size=1000,
        schema="IFC4",
        stats=list(stats),
        proxy_names=[],
        elements=elements if elements is not None else {},
        total_entities=100,
        scan_seconds=0.5,
        est_fullopen_bytes=7000,
    )
    defaults.update(overrides)
    return ScanResult(**defaults)


def _basic_scan() -> ScanResult:
    """delete/bbox/hull/decimate/keep/undo の一連の操作を試すための標準スキャン。
    IFCWALL と IFCWALLSTANDARDCASE は前方一致候補提示テスト用にあえて似た名前。"""
    return _scan(
        stats=[
            _stats("IFCWALL", element_count=12, expanded=120, unique=100, parametric=1),
            _stats("IFCWALLSTANDARDCASE", element_count=3, expanded=30, unique=30, parametric=0),
            _stats("IFCPLATE", element_count=8, expanded=80, unique=80, parametric=0),
            _stats("IFCMEMBER", element_count=5, expanded=50, unique=40, parametric=2),
        ],
        elements={
            "IFCWALL": [f"W{i}" for i in range(12)],
            "IFCWALLSTANDARDCASE": [f"WSC{i}" for i in range(3)],
            "IFCPLATE": [f"P{i}" for i in range(8)],
            "IFCMEMBER": [f"M{i}" for i in range(5)],
        },
    )


# --- 1. delete/bbox/hull: 基本コマンド ---


def test_delete_known_class_adds_intent_and_returns_confirmation_with_count():
    session = CuiSession(_basic_scan())
    msg = session.command("delete IFCWALL")
    assert "IFCWALL" in msg
    assert "12" in msg
    assert session.intents() == [Intent(op="delete", ifc_class="IFCWALL")]


def test_bbox_known_class_adds_intent():
    session = CuiSession(_basic_scan())
    msg = session.command("bbox IFCPLATE")
    assert "IFCPLATE" in msg
    assert "8" in msg
    assert session.intents() == [Intent(op="bbox", ifc_class="IFCPLATE")]


def test_hull_known_class_adds_intent():
    session = CuiSession(_basic_scan())
    msg = session.command("hull IFCMEMBER")
    assert "IFCMEMBER" in msg
    assert "5" in msg
    assert session.intents() == [Intent(op="hull", ifc_class="IFCMEMBER")]


# --- 2. decimate: ratio検証 ---


def test_decimate_valid_ratio_adds_intent_with_ratio():
    session = CuiSession(_basic_scan())
    msg = session.command("decimate IFCMEMBER 0.3")
    assert "IFCMEMBER" in msg
    assert "30" in msg  # 残30%相当の表示を期待(cui-design.md §5の例に合わせる)
    assert session.intents() == [Intent(op="decimate", ifc_class="IFCMEMBER", ratio=0.3)]


def test_decimate_ratio_at_lower_boundary_is_valid():
    session = CuiSession(_basic_scan())
    session.command("decimate IFCMEMBER 0.05")
    assert session.intents() == [Intent(op="decimate", ifc_class="IFCMEMBER", ratio=0.05)]


def test_decimate_ratio_at_upper_boundary_is_valid():
    session = CuiSession(_basic_scan())
    session.command("decimate IFCMEMBER 0.95")
    assert session.intents() == [Intent(op="decimate", ifc_class="IFCMEMBER", ratio=0.95)]


def test_decimate_ratio_below_lower_boundary_is_rejected():
    session = CuiSession(_basic_scan())
    msg = session.command("decimate IFCMEMBER 0.04")
    assert "0.04" in msg
    assert session.intents() == []


def test_decimate_ratio_above_upper_boundary_is_rejected():
    session = CuiSession(_basic_scan())
    msg = session.command("decimate IFCMEMBER 0.96")
    assert "0.96" in msg
    assert session.intents() == []


def test_decimate_non_numeric_ratio_is_rejected():
    session = CuiSession(_basic_scan())
    msg = session.command("decimate IFCMEMBER abc")
    assert "abc" in msg
    assert session.intents() == []


def test_decimate_rejected_ratio_does_not_overwrite_existing_intent():
    """既にdelete設定済みのクラスに無効なratioでdecimateを試みても、
    既存の意図は上書きされない(検証失敗時は状態を変えない)。"""
    session = CuiSession(_basic_scan())
    session.command("delete IFCMEMBER")
    session.command("decimate IFCMEMBER 999")
    assert session.intents() == [Intent(op="delete", ifc_class="IFCMEMBER")]


# --- 3. keep: 既存操作の解除 ---


def test_keep_overwrites_prior_intent_for_same_class():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    msg = session.command("keep IFCWALL")
    assert "IFCWALL" in msg
    assert session.intents() == [Intent(op="keep", ifc_class="IFCWALL")]


def test_keep_on_untouched_class_still_records_explicit_keep_intent():
    """keep は「意図の削除」ではなく明示的な保持マーカーとして記録される
    (to_operationsでOperation(op="keep")に変換されるため、intents()に
    残る必要がある。cui-design.md §6の Intent.op に "keep" が含まれる)。"""
    session = CuiSession(_basic_scan())
    session.command("keep IFCPLATE")
    assert session.intents() == [Intent(op="keep", ifc_class="IFCPLATE")]


# --- 4. 不明クラス: 前方一致候補提示・大文字小文字非区別 ---


def test_unknown_class_returns_error_with_prefix_matching_candidates():
    session = CuiSession(_basic_scan())
    msg = session.command("delete IFCWAL")
    assert "IFCWALL" in msg
    assert "IFCWALLSTANDARDCASE" in msg
    assert session.intents() == []


def test_unknown_class_with_no_candidates_returns_error_without_crashing():
    session = CuiSession(_basic_scan())
    msg = session.command("delete ZZZZZ")
    assert "ZZZZZ" in msg
    assert session.intents() == []


def test_class_matching_is_case_insensitive():
    session = CuiSession(_basic_scan())
    session.command("delete ifcwall")
    assert session.intents() == [Intent(op="delete", ifc_class="IFCWALL")]


def test_class_matching_is_case_insensitive_for_mixed_case_too():
    session = CuiSession(_basic_scan())
    session.command("bbox IfcPlate")
    assert session.intents() == [Intent(op="bbox", ifc_class="IFCPLATE")]


# --- 4b. 不明クラス: 前方一致候補が_CANDIDATE_LIMIT件を超える場合の切断表示
# (持ち越しMinor #1 / 最終レビューM2、docs/plans/2026-07-25-cui-phase2.md Task 3 同梱要件) ---


def test_unknown_class_error_appends_overflow_notice_when_candidates_exceed_limit():
    """前方一致候補が_CANDIDATE_LIMIT(10)件を超える場合、表示は先頭10件に
    切断されたうえで末尾に `...他N件`(N=超過件数)を付し、切断されている
    ことをユーザーに明示する。"""
    stats = [_stats(f"IFCMANYCANDIDATE{i:02d}", element_count=1) for i in range(12)]
    session = CuiSession(_scan(stats=stats))
    msg = session.command("delete IFCMANYCANDIDATE")
    assert session.intents() == []
    assert "...他2件" in msg
    # 表示される候補はソート済み先頭10件(00〜09)のみ
    for i in range(10):
        assert f"IFCMANYCANDIDATE{i:02d}" in msg
    assert "IFCMANYCANDIDATE10" not in msg
    assert "IFCMANYCANDIDATE11" not in msg


def test_unknown_class_error_shows_overflow_of_one_when_candidates_are_exactly_limit_plus_one():
    """境界値(レビューア指摘): 候補が_CANDIDATE_LIMIT(10)+1=11件のとき、
    overflowはちょうど1件(`...他1件`)になる。10件(無し)と12件(2件)の
    テストの間を埋める、ちょうど1件超過の境界。"""
    stats = [_stats(f"IFCMANYCANDIDATE{i:02d}", element_count=1) for i in range(11)]
    session = CuiSession(_scan(stats=stats))
    msg = session.command("delete IFCMANYCANDIDATE")
    assert session.intents() == []
    assert "...他1件" in msg
    # 表示される候補はソート済み先頭10件(00〜09)のみ
    for i in range(10):
        assert f"IFCMANYCANDIDATE{i:02d}" in msg
    assert "IFCMANYCANDIDATE10" not in msg


def test_unknown_class_error_omits_overflow_notice_when_candidates_at_or_below_limit():
    """候補が_CANDIDATE_LIMIT件以下(境界値ちょうど含む)なら `...他` は付かない
    (既存挙動の回帰ガード)。"""
    stats = [_stats(f"IFCMANYCANDIDATE{i:02d}", element_count=1) for i in range(10)]
    session = CuiSession(_scan(stats=stats))
    msg = session.command("delete IFCMANYCANDIDATE")
    assert "...他" not in msg
    for i in range(10):
        assert f"IFCMANYCANDIDATE{i:02d}" in msg


# --- 5. 同一クラス再指定は上書き(後勝ち) ---


def test_re_specifying_same_class_with_different_op_overwrites_last_wins():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    session.command("bbox IFCWALL")
    assert session.intents() == [Intent(op="bbox", ifc_class="IFCWALL")]


def test_re_specifying_same_class_keeps_its_original_list_position():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    session.command("bbox IFCPLATE")
    session.command("hull IFCWALL")  # IFCWALL再指定。先頭の位置を保つ想定。
    assert session.intents() == [
        Intent(op="hull", ifc_class="IFCWALL"),
        Intent(op="bbox", ifc_class="IFCPLATE"),
    ]


# --- 6. undo ---


def test_undo_with_number_removes_that_row():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    session.command("bbox IFCPLATE")
    session.command("hull IFCMEMBER")
    msg = session.command("undo 2")
    assert "2" in msg
    assert session.intents() == [
        Intent(op="delete", ifc_class="IFCWALL"),
        Intent(op="hull", ifc_class="IFCMEMBER"),
    ]


def test_undo_without_number_removes_last_added_row():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    session.command("bbox IFCPLATE")
    session.command("undo")
    assert session.intents() == [Intent(op="delete", ifc_class="IFCWALL")]


def test_undo_without_number_removes_last_inserted_even_if_another_class_was_modified_more_recently():
    """undo(番号省略)は『最後に新規追加された行』を取り消す。既存クラスの
    再指定はその元の挿入位置を保つため『最後に更新された行』とは限らない
    (このセッションの明示的な仕様: session.py 実装コメント参照)。"""
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")  # 挿入順1番目
    session.command("bbox IFCPLATE")  # 挿入順2番目(最後に挿入された行)
    session.command("hull IFCWALL")  # 既存(1番目)の更新。挿入順は変わらない。
    session.command("undo")
    assert session.intents() == [Intent(op="hull", ifc_class="IFCWALL")]


def test_undo_out_of_range_number_returns_error_and_does_not_mutate():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    msg = session.command("undo 5")
    assert "5" in msg
    assert session.intents() == [Intent(op="delete", ifc_class="IFCWALL")]


def test_undo_on_empty_intents_returns_error_and_does_not_crash():
    session = CuiSession(_basic_scan())
    msg = session.command("undo")
    assert isinstance(msg, str) and msg != ""
    assert session.intents() == []


def test_undo_non_numeric_argument_returns_error():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    msg = session.command("undo abc")
    assert "abc" in msg
    assert session.intents() == [Intent(op="delete", ifc_class="IFCWALL")]


# --- 7. list / render_intents ---


def test_render_intents_lists_all_current_intents_with_class_and_count():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    session.command("decimate IFCMEMBER 0.3")
    rendered = session.render_intents()
    assert "IFCWALL" in rendered
    assert "12" in rendered
    assert "IFCMEMBER" in rendered
    assert "5" in rendered


def test_render_intents_on_empty_session_says_nothing_to_show():
    session = CuiSession(_basic_scan())
    rendered = session.render_intents()
    assert rendered != ""
    assert "IFCWALL" not in rendered


def test_list_command_returns_same_string_as_render_intents():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    assert session.command("list") == session.render_intents()


# --- 7b. list の操作ラベルは日本語(監督者確定要件2、docs/plans/2026-07-24-cui-phase1.md Task 6) ---
#
# _op_label は要件定義§5のモック(「削除」「間引き 0.3」等)に合わせて日本語ラベルを
# 返す。bbox/hull は _SET_OP_LABELS の既存語彙(bbox軽量化/凸包化)と揃える
# (要件定義モックの素の "bbox" 表記ではなく、session.py内で既に確定している
# 語彙に合わせる、という監督者指定)。


def test_render_intents_label_for_delete_is_japanese():
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    rendered = session.render_intents()
    assert "削除" in rendered
    assert "delete" not in rendered


def test_render_intents_label_for_bbox_matches_existing_vocabulary():
    session = CuiSession(_basic_scan())
    session.command("bbox IFCPLATE")
    rendered = session.render_intents()
    assert "bbox軽量化" in rendered


def test_render_intents_label_for_hull_is_japanese():
    session = CuiSession(_basic_scan())
    session.command("hull IFCMEMBER")
    rendered = session.render_intents()
    assert "凸包化" in rendered


def test_render_intents_label_for_decimate_shows_japanese_word_and_ratio():
    """要件定義§5のlistモック例「間引き 0.3」に合わせる(残%表示ではなくratio値)。"""
    session = CuiSession(_basic_scan())
    session.command("decimate IFCMEMBER 0.3")
    rendered = session.render_intents()
    assert "間引き" in rendered
    assert "0.3" in rendered
    assert "decimate" not in rendered


def test_render_intents_label_for_keep_is_japanese():
    session = CuiSession(_basic_scan())
    session.command("keep IFCPLATE")
    rendered = session.render_intents()
    assert "保持" in rendered
    assert "keep" not in rendered


# --- 8. rank / render_ranking ---


def test_render_ranking_contains_each_class_name_and_its_expanded_count():
    session = CuiSession(_basic_scan())
    rendered = session.render_ranking()
    for s in _basic_scan().stats:
        assert s.ifc_class in rendered
        assert str(s.est_faces_expanded) in rendered
        assert str(s.element_count) in rendered


def test_rank_command_returns_same_string_as_render_ranking():
    session = CuiSession(_basic_scan())
    assert session.command("rank") == session.render_ranking()


def test_render_ranking_handles_empty_scan_without_crashing():
    session = CuiSession(_scan(stats=[], elements={}))
    rendered = session.render_ranking()
    assert isinstance(rendered, str)


# --- 8b. rank: proxy 名称内訳セクション (docs/plans/2026-07-25-cui-phase2.md Task 3、docs/cui-measurements.md
# 「Task 8」章の実測に基づく機能追加) ---


def test_render_ranking_appends_proxy_name_breakdown_when_present():
    """proxy_namesが非空なら、末尾に見出しと各キー・件数の行が追記される。"""
    scan = _scan(
        stats=[_stats("IFCBUILDINGELEMENTPROXY", element_count=3, expanded=30, unique=30)],
        proxy_names=[("【手摺】", 2), ("Bolt", 1)],
    )
    session = CuiSession(scan)
    rendered = session.render_ranking()
    assert "IfcBuildingElementProxy 名称内訳 (上位5)" in rendered
    assert "【手摺】" in rendered
    assert "Bolt" in rendered
    # 見出し以降に各キーとその件数が同じ行に現れる
    lines = rendered.splitlines()
    tag_line = next(line for line in lines if "【手摺】" in line)
    assert "2" in tag_line
    bolt_line = next(line for line in lines if "Bolt" in line)
    assert "1" in bolt_line


def test_render_ranking_shows_overflow_count_when_more_than_five_proxy_names():
    """6件目以降は個別表示せず `...他N種`(N=総数-5)の1行にまとめる。"""
    proxy_names = [(f"Tag{i}", 10 - i) for i in range(8)]  # 8種、上位5+overflow3
    scan = _scan(
        stats=[_stats("IFCBUILDINGELEMENTPROXY", element_count=8)],
        proxy_names=proxy_names,
    )
    session = CuiSession(scan)
    rendered = session.render_ranking()
    assert "...他3種" in rendered
    for key, _ in proxy_names[:5]:
        assert key in rendered
    for key, _ in proxy_names[5:]:
        assert key not in rendered


def test_render_ranking_with_exactly_five_proxy_names_has_no_overflow_line():
    """境界値: ちょうど5件なら `...他` 行は付かない。"""
    proxy_names = [(f"Tag{i}", 5 - i) for i in range(5)]
    scan = _scan(
        stats=[_stats("IFCBUILDINGELEMENTPROXY", element_count=5)],
        proxy_names=proxy_names,
    )
    session = CuiSession(scan)
    rendered = session.render_ranking()
    assert "...他" not in rendered
    for key, _ in proxy_names:
        assert key in rendered


def test_render_ranking_with_empty_proxy_names_matches_prior_output_unchanged():
    """空なら従来出力と完全一致(後方互換) — 名称内訳セクション自体が現れない。"""
    session = CuiSession(_basic_scan())  # _scan()既定でproxy_names=[]
    rendered = session.render_ranking()
    assert "名称内訳" not in rendered


def test_render_proxy_name_breakdown_of_empty_list_directly_returns_empty_list():
    """(レビューア指摘) `_render_proxy_name_breakdown([])`単体の直接呼び出しでも
    空リストを返すことを固定する。上の
    test_render_ranking_with_empty_proxy_names_matches_prior_output_unchanged は
    render_ranking()経由・部分一致(`"名称内訳" not in rendered`)の間接テストだが、
    本テストはproxy内訳追加の起点そのもの(空入力→空出力)を直接的な単体テストで
    補完する。"""
    assert CuiSession._render_proxy_name_breakdown([]) == []


def test_render_ranking_full_output_matches_fixed_expected_string_for_basic_scan():
    """(レビューア指摘) proxy_names導入前とのrender_ranking()出力の後方互換を
    「完全一致」で固定する。部分文字列一致ではなく、_basic_scan()の実際の出力
    全文を、変更前の実装で得られていたのと同じ固定済み期待文字列(手動実行で
    実際の出力から採取し、以後は本テストが正とする)と丸ごと比較する。この
    比較なら列幅・順序・空行・文言のどんな些細な変更も検知できる。"""
    session = CuiSession(_basic_scan())
    rendered = session.render_ranking()
    expected = "\n".join(
        [
            "ファイル: model.ifc (1000 bytes)",
            "スキーマ: IFC4",
            "総エンティティ行数: 100",
            "スキャン時間: 0.50秒",
            "推定フルオープンメモリ: 7000 bytes",
            "",
            "=== クラス別ランキング (推定Face数[展開]降順) ===",
            "#     クラス名                                     要素数         推定Face数(展開)         推定Face数(共有統合)     パラメトリック件数       寄与率",
            "1     IFCWALL                                   12                 120                   100             1     42.9%",
            "2     IFCWALLSTANDARDCASE                        3                  30                    30             0     10.7%",
            "3     IFCPLATE                                   8                  80                    80             0     28.6%",
            "4     IFCMEMBER                                  5                  50                    40             2     17.9%",
        ]
    )
    assert rendered == expected


# --- 9. to_operations ---


def test_to_operations_delete_maps_to_delete_op_with_scan_elements_as_targets():
    scan = _basic_scan()
    session = CuiSession(scan)
    session.command("delete IFCWALL")
    ops = session.to_operations()
    assert ops == [Operation(op="delete", targets=scan.elements["IFCWALL"], scope="element")]


def test_to_operations_bbox_maps_to_simplify_method_bbox():
    """bbox は末尾にscopeキーワードを指定しなければ既定"shared"になる
    (docs/plans/2026-07-31-cui-shared-scope.md)。"""
    scan = _basic_scan()
    session = CuiSession(scan)
    session.command("bbox IFCPLATE")
    ops = session.to_operations()
    assert ops == [
        Operation(
            op="simplify",
            targets=scan.elements["IFCPLATE"],
            scope="shared",
            params={"method": "bbox"},
        )
    ]


def test_to_operations_hull_maps_to_simplify_method_convex_hull():
    """hull も既定"shared"(docs/plans/2026-07-31-cui-shared-scope.md)。"""
    scan = _basic_scan()
    session = CuiSession(scan)
    session.command("hull IFCMEMBER")
    ops = session.to_operations()
    assert ops == [
        Operation(
            op="simplify",
            targets=scan.elements["IFCMEMBER"],
            scope="shared",
            params={"method": "convex_hull"},
        )
    ]


def test_to_operations_decimate_maps_to_simplify_method_decimate_with_ratio_param():
    """decimate も既定"shared"(docs/plans/2026-07-31-cui-shared-scope.md)。"""
    scan = _basic_scan()
    session = CuiSession(scan)
    session.command("decimate IFCMEMBER 0.3")
    ops = session.to_operations()
    assert ops == [
        Operation(
            op="simplify",
            targets=scan.elements["IFCMEMBER"],
            scope="shared",
            params={"method": "decimate", "ratio": 0.3},
        )
    ]


def test_to_operations_keep_maps_to_keep_op():
    scan = _basic_scan()
    session = CuiSession(scan)
    session.command("keep IFCWALL")
    ops = session.to_operations()
    assert ops == [Operation(op="keep", targets=scan.elements["IFCWALL"], scope="element")]


def test_to_operations_scope_defaults_to_shared_for_simplify_and_element_for_delete_keep():
    """delete/keep の Operation.scope は従来どおり"element"(意味を持たない)。
    bbox/decimate(scopeキーワード省略)は既定"shared"になる
    (docs/plans/2026-07-31-cui-shared-scope.md、旧
    test_to_operations_scope_is_always_element を新仕様に更新)。"""
    session = CuiSession(_basic_scan())
    session.command("delete IFCWALL")
    session.command("bbox IFCPLATE")
    session.command("decimate IFCMEMBER 0.3")
    session.command("keep IFCWALLSTANDARDCASE")
    ops = session.to_operations()
    assert [op.scope for op in ops] == ["element", "shared", "shared", "element"]


def test_to_operations_order_follows_intents_order():
    scan = _basic_scan()
    session = CuiSession(scan)
    session.command("bbox IFCPLATE")
    session.command("delete IFCWALL")
    ops = session.to_operations()
    assert [op.op for op in ops] == ["simplify", "delete"]
    assert [op.targets for op in ops] == [scan.elements["IFCPLATE"], scan.elements["IFCWALL"]]


def test_to_operations_defaults_targets_to_empty_list_when_class_missing_from_scan_elements():
    """既知クラス(statsにある)がelementsに存在しない防御的なケース
    (global_idが無い製品しかいない等、aggregate.py側の稀な経路)でも
    to_operationsはKeyErrorを出さず空targetsで済ませる。"""
    scan = _scan(stats=[_stats("IFCWEIRD", element_count=5)], elements={})
    session = CuiSession(scan)
    session.command("delete IFCWEIRD")
    ops = session.to_operations()
    assert ops == [Operation(op="delete", targets=[], scope="element")]


# --- 10. コマンド解釈全般の頑健性 ---


def test_unknown_command_verb_returns_error():
    session = CuiSession(_basic_scan())
    msg = session.command("frobnicate IFCWALL")
    assert "frobnicate" in msg
    assert session.intents() == []


def test_blank_line_returns_empty_string():
    session = CuiSession(_basic_scan())
    assert session.command("") == ""
    assert session.command("   ") == ""


def test_command_verb_is_case_insensitive():
    session = CuiSession(_basic_scan())
    session.command("DELETE IFCWALL")
    assert session.intents() == [Intent(op="delete", ifc_class="IFCWALL")]


def test_wrong_arg_count_for_delete_returns_usage_error_and_does_not_crash():
    session = CuiSession(_basic_scan())
    msg = session.command("delete")
    assert isinstance(msg, str) and msg != ""
    assert session.intents() == []


def test_wrong_arg_count_for_decimate_missing_ratio_returns_usage_error():
    session = CuiSession(_basic_scan())
    msg = session.command("decimate IFCWALL")
    assert isinstance(msg, str) and msg != ""
    assert session.intents() == []


def test_wrong_arg_count_for_decimate_extra_argument_returns_usage_error():
    session = CuiSession(_basic_scan())
    msg = session.command("decimate IFCWALL 0.3 extra")
    assert isinstance(msg, str) and msg != ""
    assert session.intents() == []


# --- 共有波及(scope)対応(docs/plans/2026-07-31-cui-shared-scope.md) ---


@pytest.fixture
def session() -> CuiSession:
    """scope対応テスト用の標準スキャン。IFCWALLは2要素、IFCDOORも別途持つ
    (test_intent_list_shows_scopeでbbox対象に使う)。"""
    return CuiSession(
        _scan(
            stats=[
                _stats("IFCWALL", element_count=2, expanded=20, unique=20, parametric=0),
                _stats("IFCDOOR", element_count=4, expanded=40, unique=40, parametric=0),
            ],
            elements={
                "IFCWALL": ["W0", "W1"],
                "IFCDOOR": ["D0", "D1", "D2", "D3"],
            },
        )
    )


def test_simplify_commands_default_to_shared_scope(session):
    """bbox/hull/decimate は既定で共有波及になり、メッセージにも明示される。"""
    msg = session.command("bbox IfcWall")
    assert msg == "IFCWALL 2要素をbbox軽量化対象に追加しました(共有波及)。"
    ops = session.to_operations()
    assert ops[0].scope == "shared"


def test_element_keyword_opts_out_to_per_element(session):
    """末尾の element キーワードで従来の個別化に切り替わる。"""
    msg = session.command("decimate IfcWall 0.3 element")
    assert msg == "IFCWALL 2要素を間引き(残30%)対象に追加しました(個別)。"
    ops = session.to_operations()
    assert ops[0].scope == "element"


def test_element_keyword_is_case_insensitive(session):
    """docstringが明記する大文字小文字不問を固定する(フェーズ最終レビューM-4、
    レビュー時は手動確認のみだった)。`decimate <クラス> 0.1 ELEMENT` の
    ような全大文字のscope指定も受理されること。"""
    msg = session.command("decimate IfcWall 0.1 ELEMENT")
    assert msg == "IFCWALL 2要素を間引き(残10%)対象に追加しました(個別)。"
    ops = session.to_operations()
    assert ops[0].scope == "element"


def test_shared_keyword_is_accepted_as_explicit_default(session):
    msg = session.command("hull IfcWall shared")
    assert msg == "IFCWALL 2要素を凸包化対象に追加しました(共有波及)。"
    assert session.to_operations()[0].scope == "shared"


def test_unknown_scope_keyword_is_rejected(session):
    msg = session.command("bbox IfcWall banana")
    assert msg == "不明な指定です: banana(使い方: bbox <クラス名> [element|shared])"
    assert session.intents() == []


def test_delete_still_takes_exactly_one_argument(session):
    """delete に scope の概念はない(連鎖は閉包計算の領域)。"""
    assert session.command("delete IfcWall element") == "使い方: delete <クラス名>"


def test_intent_list_shows_scope(session):
    session.command("decimate IfcWall 0.1")
    session.command("bbox IfcDoor element")
    rendered = session.render_intents()
    assert "間引き 0.1(共有波及)" in rendered
    assert "bbox軽量化(個別)" in rendered


def test_delete_and_keep_operations_keep_element_scope(session):
    """delete/keep の Operation.scope は従来どおり "element" のまま(実質未使用)。"""
    session.command("delete IfcWall")
    assert session.to_operations()[0].scope == "element"
