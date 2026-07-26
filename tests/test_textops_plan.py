"""textops/plan.py(削除計画: カスケード + 専有サブグラフ回収)のTDD
(docs/plans/2026-07-25-cui-phase3.md Task 2)。

`compute_text_delete_plan(graph, delete_classes) -> TextDeletePlan` は、Task 1
の `FullGraph`(全レコード参照グラフ、CSR/numpy)だけを入力に、テキストグラフ
上で完結する削除計画を作る(ifcopenshell オブジェクトも `core/cascade.py` も
importしない)。

検証する契約(タスクブリーフ Step 1 (a)-(g)):
  (a) 単純専有チェーン回収: A(seed)->B->Cのチェーンが全滅する。
  (b) 共有資源(2製品参照)は片方削除で生存: 2箇所から参照される資源は
      片方の参照元を消しても生存する。
  (c) ダイヤモンド: A->B, A->C, B->D, C->Dの形でAを消すとDはB・C両方が
      死んだ後にだけ回収される(worklistが複数経路からの減算を正しく
      集約する)。
  (d) aggregates再帰カスケード: IFCRELAGGREGATESの連鎖(Building->Storey->
      Wall)が不動点反復で多段カスケードする。この連鎖はOwnerHistoryへの
      参照(実際のsmall.ifcの `#380500 = IFCRELAGGREGATES(guid, #209, ...,
      #380499, (...));` と同型 — 属性1=OwnerHistoryが解決可能な参照として
      先頭に来る)を含む形で構成し、RelatingObjectの位置解釈が
      OwnerHistory参照を誤認しないことを固定する。
  (e) voids→fills連鎖: 壁(seed)の削除がIFCRELVOIDSELEMENTを介して開口へ、
      開口の死がIFCRELFILLSELEMENTを介して充填要素(窓)へ、不動点反復で
      連鎖する(cascade.pyのGUI版と同一意味論)。
  (f) in_degree==0レコードの生存: 何にも参照されないレコード(IFCPROJECT
      等のトップレベル)はseed/カスケード対象でない限りsweepで回収されない
      (alive_ref_countが最初から0であることそのものは回収条件にならない)。
  (g) dead循環の残存(無限ループしない): 相互参照のみで外部から到達不能に
      なった2レコードは、参照カウント方式の構造的限界により回収されず
      残る(既知の限界としてdocstringに明記される事項の実証)。無限ループ
      せず終了することも確認する。

加えて、TextDeletePlanのもう一方の契約(patch_rel_ids/statsの各キー)、
自己参照の生存(監督者裁定の不変条件)、delete_classesのupper()突合、
空グラフでのクラッシュ耐性を個別に固定する。

レビューア指摘による被覆ロック(Fix round)として、以下も固定する:
  - OwnerHistory省略ケース(IFC4の`$`相当、参照列が[Relating, Related...]
    のみ)でもIFCRELAGGREGATES/IFCRELVOIDSELEMENTの属性位置解釈が崩れない
    こと。
  - 多重辺(1レコードが同一対象を複数回参照する)のper-occurrence減算が、
    加算(in_degree)側と一致していること(過少/過剰の両方向をロック)。
"""

from __future__ import annotations

import numpy as np

from ifc_occam.scan.fullgraph import FullGraph
from ifc_occam.textops.plan import TextDeletePlan, compute_text_delete_plan


# --- テスト用ヘルパー: 合成 FullGraph を numpy で直組みする ---
#
# fullgraph.py の参照解決(searchsorted + clamp + 等値チェックのミスガード
# パターン)と同型の手順を、記述しやすい (id, class, raw_ref_ids) のリストから
# 組み立てる。records は id昇順で渡すこと(FullGraph自身の契約と同じ)。


def _make_graph(records: list[tuple[int, str, list[int]]]) -> FullGraph:
    n = len(records)
    ids = np.array([r[0] for r in records], dtype=np.int64)
    assert ids.tolist() == sorted(ids.tolist()), "records は id 昇順で渡すこと"

    class_table: list[str] = []
    class_index: dict[str, int] = {}
    class_codes = np.zeros(n, dtype=np.int32)
    for i, (_rid, cls, _refs) in enumerate(records):
        cls_upper = cls.upper()
        code = class_index.get(cls_upper)
        if code is None:
            code = len(class_table)
            class_table.append(cls_upper)
            class_index[cls_upper] = code
        class_codes[i] = code

    ref_lengths = np.array([len(r[2]) for r in records], dtype=np.int64)
    ref_indptr = np.zeros(n + 1, dtype=np.int64)
    ref_indptr[1:] = np.cumsum(ref_lengths)

    flat_raw = np.array(
        [x for r in records for x in r[2]], dtype=np.int64
    ) if ref_lengths.sum() else np.empty(0, dtype=np.int64)

    if flat_raw.size and n:
        resolved = np.searchsorted(ids, flat_raw)
        resolved_clamped = np.minimum(resolved, n - 1)
        valid = (resolved < n) & (ids[resolved_clamped] == flat_raw)
        ref_targets = np.where(valid, resolved_clamped, -1).astype(np.int64)
    else:
        ref_targets = np.empty(0, dtype=np.int64)

    in_degree = np.bincount(
        ref_targets[ref_targets >= 0], minlength=n
    ).astype(np.int64)

    return FullGraph(
        ids=ids,
        class_codes=class_codes,
        class_table=class_table,
        ref_indptr=ref_indptr,
        ref_targets=ref_targets,
        in_degree=in_degree,
        record_count=n,
    )


# --- (a) 単純専有チェーン回収 ---


def test_simple_exclusive_chain_is_swept():
    """A(seed) -> B -> C。Bも Cも他から参照されないので、Aの削除がsweepで
    Cまで連鎖的に回収される。"""
    graph = _make_graph([
        (1, "IFCWALL", [2]),  # A: seed、Bを専有参照
        (2, "IFCMATERIAL", [3]),  # B: Aからのみ参照される
        (3, "IFCCOLOUR", []),  # C: Bからのみ参照される
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1, 2, 3]
    assert plan.stats["seeds"] == 1
    assert plan.stats["cascade"] == 0
    assert plan.stats["swept"] == 2


# --- (b) 共有資源は片方削除で生存 ---


def test_shared_resource_survives_when_only_one_referrer_is_deleted():
    """材料(id=3)はWall(seed)とSlab(seedでない)の両方から参照される。
    Wallだけを消しても材料はSlabに専有されたままなので生存する。"""
    graph = _make_graph([
        (1, "IFCWALL", [3]),  # seed。共有資源を参照
        (2, "IFCSLAB", [3]),  # seedでない。共有資源を参照
        (3, "IFCMATERIAL", []),  # 共有資源、in_degree=2
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1]
    assert plan.stats["swept"] == 0


# --- (c) ダイヤモンド ---


def test_diamond_shared_descendant_is_swept_only_after_both_paths_die():
    """A(seed) -> B, A -> C, B -> D, C -> D。Dはin_degree=2(B, Cから)。
    BとCがどちらもAの死で専有回収された後、初めてDも回収される
    (worklistが複数経路からの減算を正しく積算することの検証)。"""
    graph = _make_graph([
        (1, "IFCWALL", [2, 3]),  # A: seed
        (2, "IFCMATERIAL", [4]),  # B: Aからのみ参照される
        (3, "IFCCOLOUR", [4]),  # C: Aからのみ参照される
        (4, "IFCUNIT", []),  # D: BとC両方から参照される(in_degree=2)
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1, 2, 3, 4]
    assert plan.stats["swept"] == 3


# --- 多重辺(multi-edge) per-occurrence 減算ロック ---
#
# モジュールdocstring「専有サブグラフ回収(カウントダウン)」: 参照は
# CSR上の**出現1回ごとに**減算する(重複除去しない——多重辺は加算/減算で
# 正確に相殺される設計。Task 1裁定)。以下の2件は、この加算(in_degree=
# 出現回数)と減算(sweepでの出現回数分の引き算)が一致していることを
# 両方向からロックする: 過少減算ならdying(in_degree=2)テストがdeadを
# 見逃して失敗し、過剰減算ならsurviving(in_degree=3)テストが生存を
# 見逃して失敗する。


def test_multi_edge_target_referenced_twice_by_dying_record_is_swept():
    """1レコード(seed)が同一対象(id=2)を2回参照し(多重辺)、その対象が
    当該レコードのみから参照される(専有だがin_degree=2、参照が2本)。
    sweepは出現1回ごとに減算するので、2回分の減算で0に到達し回収される。"""
    graph = _make_graph([
        (1, "IFCWALL", [2, 2]),  # seed。id=2を2回参照(多重辺)
        (2, "IFCMATERIAL", []),  # id=1からのみ参照。多重辺でin_degree=2
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1, 2]
    assert plan.stats["swept"] == 1


def test_multi_edge_target_survives_when_additional_referrer_stays_alive():
    """id=2をseed(id=1)が2回参照し(多重辺)、さらに別のレコード(id=3、
    seedでなく死なない)からも1回参照される(in_degree=3)。seedの死で
    出現2回分だけ減算されるが1残るため0に到達せず生存する。"""
    graph = _make_graph([
        (1, "IFCWALL", [2, 2]),  # seed。id=2を2回参照(多重辺)
        (2, "IFCMATERIAL", []),  # id=1から2回+id=3から1回、in_degree=3
        (3, "IFCSLAB", [2]),  # seedでない。id=2を1回参照し続ける
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1]
    assert plan.stats["swept"] == 0
    assert 2 not in plan.drop_ids.tolist()
    assert 3 not in plan.drop_ids.tolist()


# --- (d) aggregates再帰カスケード(OwnerHistory参照の位置解釈を含む) ---


def test_aggregates_relating_dead_kills_rel_and_all_related_objects():
    """単段のIFCRELAGGREGATES: OwnerHistory参照(#209のような解決可能な参照)
    がRelatingObjectの直前に来る実データ(small.ifcの実レコード)と同型の
    レイアウトで、RelatingObjectが正しく識別されRelatedObjects全員が
    連鎖することを固定する。OwnerHistory(id=1)はIFCPROJECT(id=2)からも
    独立に参照されており、rel(6)が死んでも生存する
    (誤ってRelatingObjectと解釈されていないことの裏付け——誤認していれば
    このOwnerHistory参照自体がカスケードの起点として扱われてしまう)。"""
    graph = _make_graph([
        (1, "IFCOWNERHISTORY", []),
        (2, "IFCPROJECT", [1]),  # OwnerHistoryを独立に参照(生存の裏付け)
        (3, "IFCBUILDING", []),  # seed (= RelatingObject)
        (4, "IFCBUILDINGSTOREY", []),
        (5, "IFCBUILDINGSTOREY", []),
        # OwnerHistory=1, RelatingObject=3, RelatedObjects=(4,5)
        (6, "IFCRELAGGREGATES", [1, 3, 4, 5]),
    ])
    plan = compute_text_delete_plan(graph, {"IFCBUILDING"})

    assert plan.drop_ids.tolist() == [3, 4, 5, 6]
    # OwnerHistory自体は誤ってRelatingObjectと解釈されず、生存する
    assert 1 not in plan.drop_ids.tolist()
    assert 2 not in plan.drop_ids.tolist()  # IFCPROJECT(in_degree==0)も生存


def test_aggregates_cascades_recursively_through_nested_containers():
    """IFCRELAGGREGATESの連鎖: rel1(relating=Building, related=[Storey])、
    rel2(relating=Storey, related=[Wall])。Buildingの削除は不動点反復
    (1回の走査ではrel2はまだ発火しない)でStorey経由Wallまで到達する。
    OwnerHistory(id=1)はIFCPROJECT(id=2、in_degree==0でそもそも死なない
    が参照も保持)から独立して参照されており、両rel死後も生存する。"""
    graph = _make_graph([
        (1, "IFCOWNERHISTORY", []),
        (2, "IFCPROJECT", [1]),  # OwnerHistoryを別途参照(生存の裏付け)
        (3, "IFCBUILDING", []),  # seed
        (4, "IFCBUILDINGSTOREY", []),
        (5, "IFCWALL", []),
        (6, "IFCRELAGGREGATES", [1, 3, 4]),  # OwnerHistory=1,Relating=3,Related=[4]
        (7, "IFCRELAGGREGATES", [1, 4, 5]),  # OwnerHistory=1,Relating=4,Related=[5]
    ])
    plan = compute_text_delete_plan(graph, {"IFCBUILDING"})

    assert plan.drop_ids.tolist() == [3, 4, 5, 6, 7]
    assert 1 not in plan.drop_ids.tolist()  # OwnerHistoryはIFCPROJECTの参照で生存
    assert 2 not in plan.drop_ids.tolist()  # IFCPROJECTはin_degree==0で生存
    assert plan.stats["cascade"] == 4  # storey, wall, rel6, rel7


# --- (e) voids→fills連鎖 ---


def test_voids_relating_dead_kills_rel_and_opening():
    """単段のIFCRELVOIDSELEMENT: 壁(seed)の削除でrelと開口が死ぬ。
    OwnerHistory(id=1)はIFCPROJECT(id=2)からも独立に参照されており、
    rel(5)が死んでも生存する(先頭のOwnerHistory参照をRelatingBuilding
    Elementと誤認していないことの裏付け)。"""
    graph = _make_graph([
        (1, "IFCOWNERHISTORY", []),
        (2, "IFCPROJECT", [1]),  # OwnerHistoryを独立に参照(生存の裏付け)
        (3, "IFCWALL", []),  # seed (= RelatingBuildingElement)
        (4, "IFCOPENINGELEMENT", []),  # RelatedOpeningElement
        (5, "IFCRELVOIDSELEMENT", [1, 3, 4]),
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [3, 4, 5]
    assert 1 not in plan.drop_ids.tolist()


def test_voids_then_fills_cascade_chains_across_iterations():
    """壁(seed)->(IFCRELVOIDSELEMENT)->開口->(IFCRELFILLSELEMENT)->窓、と
    異なる関係クラスをまたいで連鎖する(不動点反復が正しく連鎖することの
    検証。GUIのcompute_delete_closureと同一意味論)。OwnerHistory(id=1)は
    IFCPROJECT(id=2)からも参照され、両relの死後も生存する。"""
    graph = _make_graph([
        (1, "IFCOWNERHISTORY", []),
        (2, "IFCPROJECT", [1]),
        (3, "IFCWALL", []),  # seed
        (4, "IFCOPENINGELEMENT", []),
        (5, "IFCWINDOW", []),
        (6, "IFCRELVOIDSELEMENT", [1, 3, 4]),  # Relating=3(wall), Related=4(opening)
        (7, "IFCRELFILLSELEMENT", [1, 4, 5]),  # Relating=4(opening), Related=5(window)
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [3, 4, 5, 6, 7]
    assert plan.stats["cascade"] == 4  # opening, window, rel(voids), rel(fills)
    assert 1 not in plan.drop_ids.tolist()


# --- OwnerHistory省略ケース(IFC4の$相当) ---
#
# IFC4スキーマではOwnerHistoryは任意attrで`$`により省略できる。省略時、
# 参照列は[RelatingObject, RelatedObjects...]のみ(OwnerHistory分の参照が
# 最初から存在しない)。`_relating_and_related`のスキップ判定は「先頭参照
# のクラスがIFCOWNERHISTORYに解決するか」だけを見るため、省略時は先頭が
# そのままRelatingObjectとして正しく解釈されるはず——以下はその固定。
# OwnerHistory(id=1)はグラフ内に存在するが、当該relの参照列からは参照
# されない構成にして、per-row判別(グラフ全体にOwnerHistoryクラスが
# 存在すること自体では誤スキップが起きないこと)を厳密に検証する。


def test_aggregates_relating_dead_kills_related_when_owner_history_is_omitted():
    """IFCRELAGGREGATESでOwnerHistory省略(参照列=[Relating, Related...]
    のみ)の場合でも、relating(seed)の死がrel自身とrelated全員に正しく
    連鎖する。"""
    graph = _make_graph([
        (1, "IFCOWNERHISTORY", []),  # グラフ内に存在するが、このrelは参照しない
        (2, "IFCBUILDING", []),  # seed (= RelatingObject)
        (3, "IFCBUILDINGSTOREY", []),
        (4, "IFCBUILDINGSTOREY", []),
        # OwnerHistory省略(IFC4の$): 参照列 = [Relating=2, Related=(3,4)]
        (5, "IFCRELAGGREGATES", [2, 3, 4]),
    ])
    plan = compute_text_delete_plan(graph, {"IFCBUILDING"})

    assert plan.drop_ids.tolist() == [2, 3, 4, 5]
    assert 1 not in plan.drop_ids.tolist()  # OwnerHistoryは誰にも参照されないが生存(in_degree==0)


def test_voids_relating_dead_kills_opening_when_owner_history_is_omitted():
    """IFCRELVOIDSELEMENTでOwnerHistory省略(参照列=[RelatingBuilding
    Element, RelatedOpeningElement]のみ)の場合でも、壁(seed)の死がrelと
    開口に正しく連鎖する。"""
    graph = _make_graph([
        (1, "IFCOWNERHISTORY", []),  # グラフ内に存在するが、このrelは参照しない
        (2, "IFCWALL", []),  # seed (= RelatingBuildingElement)
        (3, "IFCOPENINGELEMENT", []),  # RelatedOpeningElement
        # OwnerHistory省略(IFC4の$): 参照列 = [Relating=2, Related=3]
        (4, "IFCRELVOIDSELEMENT", [2, 3]),
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [2, 3, 4]
    assert 1 not in plan.drop_ids.tolist()


# --- (f) in_degree==0レコードの生存 ---


def test_in_degree_zero_record_survives_unconditionally():
    """IFCPROJECT(in_degree==0、何からも参照されない)は、無関係な削除
    (壁とその専有材料の回収)が起きても手を付けられない
    (alive_ref_countが最初から0であることは回収条件にならない —
    元のin_degree>0のガードが必要)。"""
    graph = _make_graph([
        (1, "IFCPROJECT", []),  # in_degree=0、誰にも参照されない
        (2, "IFCWALL", [3]),  # seed
        (3, "IFCMATERIAL", []),  # Wall専有、sweepで回収される想定
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [2, 3]
    assert 1 not in plan.drop_ids.tolist()


# --- (g) dead循環の残存(無限ループしない) ---


def test_mutually_referencing_cycle_survives_once_unreachable_no_infinite_loop():
    """A(seed) -> X、X <-> Y(相互参照のみ)。Aの死でXのalive_ref_countは
    2(A,Yから)から1(Yからのみ)に減るが0には到達しない。Yのalive_ref_count
    も1(Xから)のままで0に到達しない——互いが互いを「生かして」しまう
    参照カウント方式の構造的限界(既知の限界としてdocstring明記対象)。
    テスト自体が完了すること(pytestがハングしないこと)が無限ループしない
    ことの実証でもある。"""
    graph = _make_graph([
        (1, "IFCWALL", [2]),  # A: seed、Xを参照
        (2, "IFCTESTNODE", [3]),  # X: Aと Yから参照される(in_degree=2)
        (3, "IFCTESTNODE", [2]),  # Y: Xからのみ参照される(in_degree=1)
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1]
    assert plan.stats["swept"] == 0


# --- 監督者裁定: 自己参照は1要素の循環、seedでない限り生存 ---


def test_self_reference_alone_survives_when_not_reachable_from_seeds():
    """自己参照のみで保たれるレコード(id=3)は、無関係な削除が起きても
    無限ループせずに生存する(自己参照はin_degreeに1回カウントされるが、
    自分自身が死ぬまで自分の参照は減算されない——1要素の循環として
    (g)と同じ構造的限界に該当し、特別扱い不要で自然に生存する)。"""
    graph = _make_graph([
        (1, "IFCWALL", [2]),  # seed
        (2, "IFCMATERIAL", []),  # Wall専有、回収される
        (3, "IFCTESTNODE", [3]),  # 自己参照のみ。in_degree=1(自分自身から)
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1, 2]
    assert 3 not in plan.drop_ids.tolist()


# --- delete_classes は upper() 突合 ---


def test_delete_classes_matching_is_case_insensitive():
    graph = _make_graph([(1, "IFCWALL", [])])
    plan = compute_text_delete_plan(graph, {"ifcwall"})

    assert plan.drop_ids.tolist() == [1]
    assert plan.stats["seeds"] == 1


# --- 汎用 IFCREL* 規則: kept かつ dead 参照ありは patch 候補 ---


def test_generic_ifcrel_referencing_dead_id_becomes_patch_candidate():
    """3特殊クラス以外のIFCREL*(ここではIFCRELASSOCIATESMATERIAL)は
    位置解釈をせず、dead idを参照しているかどうかだけで候補判定される。
    このrel自体はカスケード対象ではないのでdead化はしない(kept)。"""
    graph = _make_graph([
        (1, "IFCWALL", []),  # seed
        (2, "IFCMATERIAL", []),
        (3, "IFCRELASSOCIATESMATERIAL", [1, 2]),  # dead(1)とalive(2)を参照
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == [1]
    assert plan.patch_rel_ids.tolist() == [3]
    assert plan.stats["rels_patched"] == 1


def test_generic_ifcrel_not_referencing_any_dead_id_is_not_a_candidate():
    graph = _make_graph([
        (1, "IFCWALL", []),
        (2, "IFCMATERIAL", []),
        (3, "IFCRELASSOCIATESMATERIAL", [1, 2]),
    ])
    plan = compute_text_delete_plan(graph, set())  # 何も削除しない

    assert plan.drop_ids.tolist() == []
    assert plan.patch_rel_ids.tolist() == []
    assert plan.stats["rels_patched"] == 0


# --- stats の全キーが seeds/cascade/swept/rels_dropped/rels_patched を
#     一貫して分解すること ---


def test_stats_breakdown_across_seeds_cascade_sweep_and_rel_candidates():
    graph = _make_graph([
        (1, "IFCOWNERHISTORY", []),
        (2, "IFCPROJECT", [1]),  # OwnerHistoryを独立に参照(生存の裏付け)
        (3, "IFCWALL", [5]),  # seed。材料を専有参照
        (4, "IFCOPENINGELEMENT", []),
        (5, "IFCMATERIAL", []),  # Wall専有 -> sweepで回収
        (6, "IFCRELVOIDSELEMENT", [1, 3, 4]),  # relating=3(dead)-> cascade死
        (7, "IFCRELASSOCIATESMATERIAL", [3, 8]),  # dead(3)参照。kept、patch候補
        (8, "IFCCOLOUR", []),  # rel7からのみ参照。rel7がkeptなので生存
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.stats["seeds"] == 1
    assert plan.stats["cascade"] == 2  # rel(6) + opening(4)
    assert plan.stats["swept"] == 1  # material(5)
    assert set(plan.drop_ids.tolist()) == {3, 4, 5, 6}
    assert plan.patch_rel_ids.tolist() == [7]
    assert plan.stats["rels_patched"] == 1
    assert plan.stats["rels_dropped"] == 1  # dead側でIFCREL*なのはrel(6)のみ
    assert 8 not in plan.drop_ids.tolist()  # rel7がkeptのままなので生存
    assert 1 not in plan.drop_ids.tolist()  # OwnerHistoryはIFCPROJECTの参照で生存
    assert 2 not in plan.drop_ids.tolist()  # IFCPROJECTはin_degree==0で生存


# --- 空グラフ ---


def test_empty_graph_produces_empty_plan_without_crashing():
    graph = _make_graph([])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.tolist() == []
    assert plan.patch_rel_ids.tolist() == []
    assert plan.stats == {
        "seeds": 0,
        "cascade": 0,
        "swept": 0,
        "rels_dropped": 0,
        "rels_patched": 0,
    }


# --- 契約: dtype ---


def test_drop_ids_and_patch_rel_ids_are_sorted_ascending_int64():
    graph = _make_graph([
        (1, "IFCWALL", []),  # seed
        (2, "IFCWALL", []),  # seed
        (3, "IFCRELASSOCIATESMATERIAL", [1, 2, 4]),
        (4, "IFCMATERIAL", []),
    ])
    plan = compute_text_delete_plan(graph, {"IFCWALL"})

    assert plan.drop_ids.dtype == np.int64
    assert plan.patch_rel_ids.dtype == np.int64
    assert plan.drop_ids.tolist() == sorted(plan.drop_ids.tolist())
    assert plan.patch_rel_ids.tolist() == sorted(plan.patch_rel_ids.tolist())
