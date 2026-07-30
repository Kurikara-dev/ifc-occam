"""書き出し時ゴミ回収(GC)。simplify が孤児化させた旧形状レコードを、
書き出したファイルの参照グラフに対する mark-and-sweep で一括除去する。

背景(2026-07-30 プローブ実測、docs/plans/2026-07-30-simplify-cleanup-speedup.md):
remove_deep2 を要素ごとに呼ぶ掃除は donuts 族データで47秒/要素(456要素で
約6時間)。掃除をやめて「捨てたルートidの記録」だけ行い、書き出したファイルへ
本モジュールの GC を1回かけると、要素ループは3.3秒/要素・GCは約80秒の固定費
(要素数非依存)になり、クラス別レコード数は remove_deep2 で正しく消した出力と
完全一致した。

## ルートの決め方(クラス推定をしない)

mark のルートは「in_degree 0 の全レコード − simplify が捨てたと記録した
ルートid(doomed_root_ids)」。孤児サブツリーの内部ノードはサブツリー内から
参照されている(in_degree>0)ため、ルートから除外すべきはトップだけであり、
そのトップは simplify 自身が正確に知っている。クラス名でルート適格性を推定
すると、列挙漏れ(未知のトップレベル幾何クラス)が黙って肥大に化ける。

## 安全側の設計

- 生き残り(keep)のどれかが drop 対象を参照していたら GC を**中止**し、
  fat(ゴミ込み)をそのまま最終出力に採用する(dangling を作るくらいなら
  太い方がまし。原理上は起きない——doomed root は in_degree 0 なので外から
  参照されず、その内部は mark に到達しない——が、保険として毎回検査する)。
- doomed root が mark で生き残った(=何かがまだ参照している)場合は削除せず、
  参照元クラスつきの警告情報として報告する(core/simplify._cleanup_items の
  残置警告と同じ検出線。consolidate の同型欠陥はこの線が自白させた)。

## 副作用として落ちるもの(フェーズ最終レビューM7)

mark-and-sweep は「ルートから到達可能か」だけを見るため、原本に元々
どのルートからも到達できない参照サイクル(死んだサイクル)が含まれていた
場合、GC経路はそれも一緒に落とす(inline 掃除は個別の旧アイテムしか
辿らないため、そのようなサイクルは残す)。IFCの実データでは実質起きない
想定だが、起きた場合は出力がその分小さくなるだけであり、これは仕様である
(バグではない)。

このモジュールは textops の規律に従い、ifcopenshell を import しない。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ifc_occam.scan.fullgraph import FullGraph, scan_full_graph
from ifc_occam.textops.plan import TextDeletePlan
from ifc_occam.textops.rewrite import rewrite_without

__all__ = ["GcReport", "gc_rewrite"]


@dataclass(slots=True)
class GcReport:
    """GC 1回の結果。aborted=True のときは fat をそのまま出力に採用済み
    (records_dropped=0、出力は正しいが旧形状が残っている)。"""

    records_dropped: int
    #: (record id, 自身のクラス名, 参照元クラス名のカンマ結合)
    doomed_survivors: list[tuple[int, str, str]]
    aborted: bool


def _mark_reachable(graph: FullGraph, doomed_root_ids) -> np.ndarray:
    """roots(in_degree 0 − doomed_root_ids)から到達可能なレコードの
    bool マスク(ids に整列)を返す。フロンティアBFS(全体ベクトル演算、
    レコード規模のPythonループなし)。"""
    n = graph.record_count
    keep = np.zeros(n, dtype=bool)
    if n == 0:
        return keep

    doomed = np.asarray(sorted(set(int(i) for i in doomed_root_ids)), dtype=np.int64)
    pos = np.searchsorted(graph.ids, doomed)
    pos_clamped = np.clip(pos, 0, n - 1)
    doomed_rows = pos_clamped[(pos < n) & (graph.ids[pos_clamped] == doomed)]

    is_root = graph.in_degree == 0
    is_root[doomed_rows] = False

    indptr = graph.ref_indptr
    targets = graph.ref_targets
    frontier = np.nonzero(is_root)[0]
    keep[frontier] = True
    while frontier.size:
        starts = indptr[frontier]
        counts = indptr[frontier + 1] - starts
        total = int(counts.sum())
        if total == 0:
            break
        base = np.repeat(starts, counts)
        within = np.arange(total, dtype=np.int64) - np.repeat(
            np.cumsum(counts) - counts, counts
        )
        t = targets[base + within]
        t = t[t >= 0]
        t = t[~keep[t]]
        frontier = np.unique(t)
        keep[frontier] = True
    return keep


def _kept_to_dropped_violations(graph: FullGraph, keep: np.ndarray) -> int:
    """生き残りレコードから drop 対象への参照の本数(0でなければGC中止)。

    原理的には常に0になる(doomed root は in_degree 0 のためkeep側から
    到達できず、内部もmarkに含まれない)。この関数は「doomed rootが外から
    参照されている場合の保険」ではなく、mark(BFSの閉包計算)の実装そのものが
    壊れていないかを検査する自己検査であり、正しい実装では発火しない
    (フェーズ最終レビューM4)。"""
    edge_src_kept = np.repeat(keep, np.diff(graph.ref_indptr))
    t = graph.ref_targets[edge_src_kept]
    t = t[t >= 0]
    return int((~keep[t]).sum())


def _doomed_survivor_details(
    graph: FullGraph, keep: np.ndarray, doomed_root_ids
) -> list[tuple[int, str, str]]:
    """mark を生き残った doomed root の (record id, 自身のクラス名, 参照元クラス名)
    を返す。

    生き残った=何かがまだ参照している、なので参照元クラスを逆引きして
    警告に添える(何が掴んでいるかが分からない警告は調査に使えない)。
    自身のクラス名も添えるのは、core/simplify._cleanup_items の残置警告
    (「旧形状 {クラス名} #{id} が…」)と同じ情報量に揃えるため
    (フェーズ最終レビューM1: GCの警告はこれが抜けていた)。
    """
    n = graph.record_count
    doomed = np.asarray(sorted(set(int(i) for i in doomed_root_ids)), dtype=np.int64)
    pos = np.searchsorted(graph.ids, doomed)
    pos_clamped = np.clip(pos, 0, n - 1)
    doomed_rows = pos_clamped[(pos < n) & (graph.ids[pos_clamped] == doomed)]
    survivor_rows = doomed_rows[keep[doomed_rows]]
    if survivor_rows.size == 0:
        return []

    src_rows = np.repeat(np.arange(n, dtype=np.int64), np.diff(graph.ref_indptr))
    details: list[tuple[int, str, str]] = []
    for row in survivor_rows:
        referrer_rows = np.unique(src_rows[graph.ref_targets == row])
        names = sorted(
            {graph.class_table[int(graph.class_codes[r])] for r in referrer_rows}
        )
        own_class = graph.class_table[int(graph.class_codes[row])]
        details.append((int(graph.ids[row]), own_class, ", ".join(names) or "不明"))
    return details


def gc_rewrite(
    fat_path: str | Path,
    out_path: str | Path,
    doomed_root_ids,
    source_name: str,
) -> GcReport:
    """fat_path(ゴミ込みで書き出された出力)から到達不能レコードを除去した
    ファイルを out_path に書く。安全検査に違反した場合は fat をそのまま
    out_path へ移して中止する(aborted=True)。fat_path は成功時には
    呼び出し側が削除してよい(中止時は移動済みで存在しない)。"""
    fat = Path(fat_path)
    out = Path(out_path)

    graph = scan_full_graph(fat)
    keep = _mark_reachable(graph, doomed_root_ids)

    if _kept_to_dropped_violations(graph, keep) > 0:
        shutil.move(str(fat), str(out))
        return GcReport(records_dropped=0, doomed_survivors=[], aborted=True)

    survivors = _doomed_survivor_details(graph, keep, doomed_root_ids)
    drop_ids = graph.ids[~keep]
    plan = TextDeletePlan(
        drop_ids=drop_ids,
        patch_rel_ids=np.empty(0, dtype=np.int64),
        stats={
            "seeds": 0,
            "cascade": 0,
            "swept": int(drop_ids.size),
            "rels_dropped": 0,
            "rels_patched": 0,
        },
    )
    rewrite_without(fat, out, plan, graph, source_name, stamp_header_lines=False)
    return GcReport(
        records_dropped=int(drop_ids.size), doomed_survivors=survivors, aborted=False
    )
