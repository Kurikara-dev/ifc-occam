"""参照グラフ集計(バルクスキャン結果 → クラス別の推定面数)(cui-design.md §4)。

`ifc_occam.scan.pipeline.scan_records` が返す `RawScan`(entities/face_ids/
weighted の3バケツ。ブロッククラスは意図的に不在)を受け取り、以下を計算する:

1. **numpy の id テーブル + CSR**: `entities`(中間クラスのみ)・`face_ids`
   (単純frontier、weight常に1)・`weighted`(テッセレーション/パラメトリック
   frontier)の3つの id を1本の昇順ソート済み `ids`(int64)にまとめ、
   `own_weight`/`is_parametric` を同じ並びで持つ。中間クラスの refs だけが
   子を持つため、それを「エンティティローカル」な小さいCSR
   (`entity_indptr`/`entity_refs_resolved`、`entities`の件数だけの行数)として
   別持ちし、各要素の各 ref は `np.searchsorted(ids, ...)` で
   `ids`(全idの空間)上のインデックスに解決する(解決できない場合は `-1`。
   ブロッククラスは`RawScan`に存在しないため、そこへの参照は必ず解決不能=
   重み0として扱う、という設計上の意図的な挙動)。id→index の Python dict は
   一切構築しない(cui-design.md §4 手順1、docs/plans/2026-07-24-cui-phase1.md Task 3 の明示指定)。
   製品同定・proxy_names 等、`entities`(全体の一部)だけを対象にする小さい
   Python set/dict はこの「idテーブル全体のdict化禁止」とは別物として許容する
   (`entities` は大きいファイルでも全体の一部、docs/plans/2026-07-24-cui-phase1.md
   Task 2 の実測では large.ifcで約11.3%)。

2. **製品の同定**: `IFCPRODUCTDEFINITIONSHAPE` の entity_id 集合を作り、
   `entities` のうち refs がその集合と交差するものを「製品」とする
   (cui-design.md §4 手順2。スキーマ表を持たない設計)。

3. **重み伝播 — 設計書の式からの意図的な逸脱(要監督者確認)**:
   cui-design.md §4 手順3 は `w(e) = weight(e) + Σ w(refs)` を「メモ化DFSで
   計算、DAGなので線形時間」と書いているが、これを字面通り「1エンティティに
   つき1個のグローバルなメモ(subtree_weight)」として実装すると、**ダイヤモンド
   構造(1つの製品の到達集合の中で複数の経路が同じ子孫に合流するケース)を
   二重に加算してしまう**(例: 製品→{A,B}→共有フェース、という構造で
   w(製品)=w(A)+w(B) を単純合算すると共有フェースの重みが2回加算される。
   これは「共有形状を2製品が参照→expandedは2倍」という設計の要求とは
   別物で、docs/plans/2026-07-24-cui-phase1.md Task 3 が明示的に要求する
   「ダイヤモンド参照で二重加算しない」と真正面から矛盾する)。

   このモジュールは矛盾を、**「製品ごとに独立した到達集合ベースの走査」**で
   解決する: 各製品について明示スタックで到達可能な全ノードを走査し、
   「今回の走査で最初に訪れたノードの own_weight」だけを合算する
   (`_traverse`、訪問済みsetは呼び出し単位)。この訪問済みsetは
   (a) 1つの製品の到達集合内の合流(ダイヤモンド)を自然に1回だけ数え、
   (b) 循環参照でも「訪問済みならスキップ」で安全に停止する(3状態
   (未訪問/訪問中/完了)を分けるメモ化は不要。個々のノードの重みを
   「何回加算したか」だけが問題であり、経路がbackedgeかcross-edgeかは
   区別する必要がないため)。
   一方、**製品をまたぐ共有**(cui-design.md の例そのもの: 2つの製品が
   同じ共有形状を参照)は各製品が「自分自身の」訪問済みsetを新規に使うため
   正しく2回(製品ごとに1回)数えられる — 設計書の例(2倍)と矛盾しない。
   結果として本実装は「1製品内の合流は1回、製品間の共有は製品数分」という
   一貫した規則になる。

   トレードオフ(性能・監督者への報告事項): この「製品ごとに独立した走査」は
   複数の製品が同じ巨大な部分木を共有する場合、その部分木を製品数分だけ
   再走査する(グローバルなsubtree_weightメモの再利用をしない)。small.ifc/
   large.ifcの実データ調査(scripts/investigate_shape_sharing.py)では
   RepresentationMapの最大参照数は48件程度であり、この規模なら実害はないと
   判断した(統合テストの実測scan_secondsで裏付ける)。1.2GB/6.5GB級
   (Task 8)で共有度が桁違いに大きい場合は要再検討(正確性を犠牲にせず
   高速化するには、合流のない安全な部分木だけを検出してグローバルに
   メモ化する、といった追加の工夫が必要になる)。

   `refs` タプル自体の重複(parserは重複除去しない設計、
   docs/plans/2026-07-24-cui-phase1.md Task 2 参照)は、CSR構築時に各エンティティの refs を
   `sorted(set(...))` してから解決する(同じ理由: 1エンティティの直接refs
   内の重複がそのまま二重加算にならないようにする、上記ダイヤモンド対応の
   一部)。

4. **est_faces_expanded / est_faces_unique**:
   - expanded(class) = Σ_{製品 p, class(p)==class} (pの到達集合の own_weight
     合計、`_traverse` に毎回新しい visited set を渡す)。
   - unique は「初回訪問時のみ数える」2回目のパス(cui-design.md §4 手順3)
     として、製品を entity_id 昇順の決定的な順序で処理し、**1本の
     visited set を全製品で共有**する(`_traverse` に同じ set を渡し続ける)。
     ある製品の走査で新たに訪問したノードだけがその製品(→そのクラス)に
     加算され、以後の製品はそのノードをスキップする。すなわち共有部分木の
     帰属は「entity_id が小さい製品が勝つ」(先着順)。どちらの製品に
     帰属させるべきかという「正しい」答えは無く、テストでは決定的であることの
     方を固定する。

5. **parametric_count**: 「製品の到達集合(expandedと同じ走査)が
   is_parametric=True のノードを1つ以上含むか」で判定する(brief記載の
   もう一方の解釈「パラメトリックエンティティの総数」ではなく、
   「見積りが名目値を含む製品の件数」という解釈を採用: 「件数を別掲する」の
   目的は「この見積りは何件が近似か」をユーザーに伝えることなので、
   製品単位の方が実用上意味を持つと判断した)。unique(製品間で重複しない
   走査)ではなく expanded 相当の製品ごとの走査で判定する(製品間で共有される
   パラメトリック部分木がある場合、両方の製品の見積りが実際に名目値の
   影響を受けているため、両方をカウントするのが「見積りへの影響」を正しく
   反映する)。

6. **proxy_names**: `IFCBUILDINGELEMENTPROXY` の製品(同定済み)の集計キー
   (Name が正規表現 `^【([^】]*)】` に一致する場合は「【カテゴリ】」部分
   (接頭辞全体、括弧含む)、一致しない場合は Name 全体)の頻度上位20件
   (`Counter.most_common`)。Name が空文字列/None の製品は数えない
   (docs/plans/2026-07-25-cui-phase2.md Task 3)。
   集計キーをタグ接頭辞方式にした根拠(Task 8実測、docs/cui-measurements.md
   「Task 8」章「5. 鉄骨ファブ系の所見」): 実データのproxy Nameは連番付き
   (例「【曲折円柱】曲折円柱 (1903)」)で、素朴なName完全一致の頻度集計では
   上位20件が全てcount=1になり判断材料として無力だったが、Name先頭の
   「【カテゴリ】」タグ接頭辞だけで再集計すると同一カテゴリの個体が
   100%(mini: 456/456、small: 936/936)束なることを実証した。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ifc_occam.scan.parser import ScanEntity
from ifc_occam.scan.pipeline import RawScan, scan_records

__all__ = [
    "ClassScanStats",
    "ScanResult",
    "FULLOPEN_BYTES_MULTIPLIER",
    "aggregate_scan",
    "scan_file",
]

#: フルオープン推定メモリ = ファイルサイズ × この係数(cui-design.md §7)。
#: Task 8 実測により 7→14 に校正(docs/cui-measurements.md「Task 8」章)。
#: 根拠: ctypes+psapi(PeakWorkingSetSize)によるフルオープン単体(削除等の
#: 操作なし、ifcopenshell.open直後にプロセス終了直前で読む方式)の実測比
#: (ピークワーキングセット / ファイルサイズ):
#:   test-donuts_mini.ifc  (305,788,056 bytes) → 13.71倍
#:   test-donuts_small.ifc (1,224,450,442 bytes) → 12.94倍
#: 2点の実測が13〜14倍の範囲に収まって一貫していたため、安全側(過小評価より
#: 過大評価の方が「警告を出し過ぎる」で済み実害が小さい)に倍の14へ切り上げて
#: 校正した(単純平均13.32倍ではなく、実測の上限側13.71倍にさらに切り上げ)。
#: なお本定数は cui/repl.py の _FULLOPEN_WARN_BYTES の比較式では約分されて
#: 打ち消し合うため、警告が発火するファイルサイズの閾値そのもの(2GiB)には
#: 影響しない — 変わるのは「推定フルオープンメモリ」として表示される数値の
#: 精度のみ(cui/repl.pyのコメントも参照)。
FULLOPEN_BYTES_MULTIPLIER = 14

_PDS_CLASS = "IFCPRODUCTDEFINITIONSHAPE"
_PROXY_CLASS = "IFCBUILDINGELEMENTPROXY"
_PROXY_NAME_TOP_N = 20

#: proxy Name先頭の「【カテゴリ】」タグ接頭辞を検出する正規表現(docs/plans/2026-07-25-cui-phase2.md Task 3)。
#: 実データ(docs/cui-measurements.md「Task 8」章)では個体ごとに連番("ST-001"等)が
#: 付き素朴なName完全一致集計は無力だが、この接頭辞(括弧含む全体、group(0))での
#: 集計は同一カテゴリの個体を束ねる。
_PROXY_TAG_PREFIX_RE = re.compile(r"^【([^】]*)】")


@dataclass
class ClassScanStats:
    """クラス別の推定面数集計(cui-design.md §4)。"""

    ifc_class: str
    element_count: int
    est_faces_expanded: int  # Σ 要素ごとの推定面数(共有は参照回数分)
    est_faces_unique: int  # 共有を1回で数えた推定面数
    parametric_count: int  # パラメトリック名目重みを到達集合に含む要素数


@dataclass
class ScanResult:
    """スキャン(診断)結果一式(cui-design.md §4)。"""

    path: str
    file_size: int
    schema: str
    stats: list[ClassScanStats]  # est_faces_expanded 降順
    proxy_names: list[tuple[str, int]]  # 集計キー(Nameまたはタグ接頭辞)頻度 上位20(§6)
    elements: dict[str, list[str]]  # ifc_class → [GlobalId,...] (apply用)
    total_entities: int
    scan_seconds: float
    est_fullopen_bytes: int  # フルオープン推定メモリ(§7)


@dataclass(slots=True)
class _Product:
    raw_id: int
    ifc_class: str
    global_id: str | None
    name: str | None


@dataclass(slots=True)
class _Graph:
    """重み伝播用の numpy グラフ表現(モジュールdocstring §1参照)。"""

    own_weight: np.ndarray  # (n,) int64、`ids`(全id空間、暗黙のソート順)に整列
    is_parametric: np.ndarray  # (n,) bool、同上
    entity_local_index: np.ndarray  # (n,) int64。-1=葉(子を持たない)。それ以外は entity_indptr の行番号
    entity_indptr: np.ndarray  # (n_entities+1,) int64
    entity_refs_resolved: np.ndarray  # (total_refs,) int64。`ids`上のindex。-1=参照先が存在しない(解決不能)
    ids: np.ndarray  # (n,) int64昇順。全id空間そのもの(暗黙のソート順)。
    # 製品のraw_id→フルインデックス解決は `_resolve_full_indices` がこれを
    # searchsortedする(旧 raw_id_to_full_index の dict[int,int] は
    # large.ifcで実測21.8MB消費し、モジュールdocstring §1の
    # 「id→indexのPython dictは一切構築しない」規則に反していたため撤去。
    # docs/plans/2026-07-24-cui-phase1.md Task 4 前段修正)。


@dataclass(slots=True)
class _ClassAcc:
    element_count: int = 0
    est_faces_expanded: int = 0
    est_faces_unique: int = 0
    parametric_count: int = 0
    global_ids: list[str] = field(default_factory=list)


def _build_graph(raw: RawScan) -> _Graph:
    entity_ids = np.fromiter(
        (e.entity_id for e in raw.entities), dtype=np.int64, count=len(raw.entities)
    )
    n_entities = len(entity_ids)
    face_ids_arr = np.array(raw.face_ids, dtype=np.int64)
    n_faces = len(face_ids_arr)
    weighted_ids = np.fromiter(
        (w[0] for w in raw.weighted), dtype=np.int64, count=len(raw.weighted)
    )
    weighted_w = np.fromiter(
        (w[1] for w in raw.weighted), dtype=np.int64, count=len(raw.weighted)
    )
    weighted_p = np.fromiter(
        (w[2] for w in raw.weighted), dtype=bool, count=len(raw.weighted)
    )

    all_ids = np.concatenate([entity_ids, face_ids_arr, weighted_ids])
    all_weight = np.concatenate(
        [np.zeros(n_entities, dtype=np.int64), np.ones(n_faces, dtype=np.int64), weighted_w]
    )
    all_param = np.concatenate(
        [np.zeros(n_entities, dtype=bool), np.zeros(n_faces, dtype=bool), weighted_p]
    )

    order = np.argsort(all_ids, kind="stable")
    ids = all_ids[order]
    own_weight = all_weight[order]
    is_parametric = all_param[order]
    n = len(ids)

    entity_sorted_pos = np.searchsorted(ids, entity_ids) if n_entities else np.zeros(0, dtype=np.int64)

    entity_local_index = np.full(n, -1, dtype=np.int64)
    if n_entities:
        entity_local_index[entity_sorted_pos] = np.arange(n_entities)

    # 各エンティティの直接refsは重複除去してから使う(モジュールdocstring §3:
    # ダイヤモンド対応の一部。parserは意図的に重複除去しない設計のため)。
    deduped_refs: list[tuple[int, ...]] = [tuple(sorted(set(e.refs))) for e in raw.entities]
    ref_lengths = (
        np.fromiter((len(r) for r in deduped_refs), dtype=np.int64, count=n_entities)
        if n_entities
        else np.zeros(0, dtype=np.int64)
    )
    entity_indptr = np.zeros(n_entities + 1, dtype=np.int64)
    if n_entities:
        entity_indptr[1:] = np.cumsum(ref_lengths)

    total_refs = int(entity_indptr[-1]) if n_entities else 0
    if total_refs:
        flat_refs_raw = np.fromiter(
            (r for refs in deduped_refs for r in refs), dtype=np.int64, count=total_refs
        )
        resolved = np.searchsorted(ids, flat_refs_raw)
        resolved_clamped = np.minimum(resolved, n - 1)
        valid = (resolved < n) & (ids[resolved_clamped] == flat_refs_raw)
        entity_refs_resolved = np.where(valid, resolved_clamped, -1).astype(np.int64)
    else:
        entity_refs_resolved = np.zeros(0, dtype=np.int64)

    return _Graph(
        own_weight=own_weight,
        is_parametric=is_parametric,
        entity_local_index=entity_local_index,
        entity_indptr=entity_indptr,
        entity_refs_resolved=entity_refs_resolved,
        ids=ids,
    )


def _resolve_full_indices(raw_ids: np.ndarray, graph: _Graph) -> np.ndarray:
    """製品の raw_id 配列を `graph.ids`(全id空間、昇順)上のフルインデックス配列に
    一括解決する(docs/plans/2026-07-24-cui-phase1.md Task 4 前段修正: 旧 `raw_id_to_full_index` dict の
    置換。`_build_graph` 内の参照解決(L238-241相当)と同じ
    searchsorted+clamp+等値チェックのパターンを使う)。

    製品は常に `raw.entities` から同定される(`_identify_products`)ため、
    `graph.ids` は同じ `raw.entities` を含んで構築されており、正常経路では
    全ての製品raw_idが必ず解決できるという不変条件がある。この関数はその
    不変条件が破れた場合(バグ)に、参照解決のように `-1` で静かに無視するのでは
    なく、`KeyError` で即座に失敗する(旧dict実装 `dict[raw_id]` の
    KeyError fail-loud挙動を保つ。挙動を静かに変えないことが前段修正の
    明示要件)。
    """
    if len(raw_ids) == 0:
        return np.zeros(0, dtype=np.int64)

    n = len(graph.ids)
    pos = np.searchsorted(graph.ids, raw_ids)
    pos_clamped = np.minimum(pos, n - 1)
    valid = (pos < n) & (graph.ids[pos_clamped] == raw_ids)
    if not np.all(valid):
        missing = sorted(raw_ids[~valid].tolist())
        raise KeyError(f"未知の product raw_id です(グラフに存在しません): {missing}")
    return pos_clamped.astype(np.int64)


def _traverse(start_idx: int, graph: _Graph, visited: set[int]) -> tuple[int, bool]:
    """start_idx(`ids`上のindex)から明示スタックで到達可能な全ノードを走査する。

    `visited` に既に含まれるノードは(このノード自身がこの呼び出しで初めて
    渡された新規setであれ、呼び出し間で共有され続けているsetであれ)
    「既に数えた」ものとしてスキップする。これにより:
      - 1回の呼び出し内での合流(ダイヤモンド)や循環参照は、`visited` が
        呼び出しの最初から最後まで一貫して更新され続けることで、安全に
        1回だけ数えられる(3状態のメモ化は不要。モジュールdocstring §3)。
      - `visited` を呼び出し間で共有すれば(unique集計)、「初回訪問した
        呼び出しだけがそのノードの重みを得る」という帰属になる。
      - `visited` を呼び出しごとに新規のsetにすれば(expanded集計)、
        各呼び出しは独立して同じノードを数えられる(製品間の共有は
        製品数分カウントされる)。

    戻り値: (このノードから新たに訪問したノードのown_weight合計,
             新たに訪問したノードの中にis_parametric=Trueが1つ以上あるか)。
    """
    stack = [int(start_idx)]
    total = 0
    saw_parametric = False
    while stack:
        idx = stack.pop()
        if idx in visited:
            continue
        visited.add(idx)
        total += int(graph.own_weight[idx])
        if graph.is_parametric[idx]:
            saw_parametric = True
        local_e = int(graph.entity_local_index[idx])
        if local_e != -1:
            start_off = int(graph.entity_indptr[local_e])
            end_off = int(graph.entity_indptr[local_e + 1])
            for child in graph.entity_refs_resolved[start_off:end_off]:
                child_idx = int(child)
                if child_idx != -1 and child_idx not in visited:
                    stack.append(child_idx)
    return total, saw_parametric


def _identify_products(entities: list[ScanEntity]) -> list[_Product]:
    """PDSを参照するエンティティ=製品(cui-design.md §4 手順2)。
    entity_id 昇順(unique集計の決定的な処理順の基礎)で返す。
    """
    pds_ids = {e.entity_id for e in entities if e.ifc_class == _PDS_CLASS}
    products = [
        _Product(raw_id=e.entity_id, ifc_class=e.ifc_class, global_id=e.global_id, name=e.name)
        for e in entities
        if not pds_ids.isdisjoint(e.refs)
    ]
    products.sort(key=lambda p: p.raw_id)
    return products


def _proxy_name_key(name: str) -> str:
    """proxy Name の集計キーを決定する(docs/plans/2026-07-25-cui-phase2.md Task 3)。

    先頭が `【カテゴリ】` 形式のタグ接頭辞なら接頭辞全体(括弧含む、
    `_PROXY_TAG_PREFIX_RE` の group(0))をキーにする。それ以外は Name 全体を
    そのままキーにする(接頭辞なしのNameに対する既存挙動を保つ)。
    """
    m = _PROXY_TAG_PREFIX_RE.match(name)
    return m.group(0) if m else name


def _compute_proxy_names(products: list[_Product]) -> list[tuple[str, int]]:
    counter = Counter(
        _proxy_name_key(p.name)
        for p in products
        if p.ifc_class == _PROXY_CLASS and p.name
    )
    return counter.most_common(_PROXY_NAME_TOP_N)


def aggregate_scan(raw: RawScan, *, path: str | Path, file_size: int) -> ScanResult:
    """`RawScan` から `ScanResult` を計算する純粋関数(I/O・時間計測を行わない)。

    `scan_seconds` は `raw.elapsed_seconds`(scan_records自体の実測秒数)を
    そのまま使う。ファイル全体の経過時間(scan_records + このaggregate自体)を
    計測したい場合は `scan_file` を使うこと。
    """
    graph = _build_graph(raw)
    products = _identify_products(raw.entities)

    # 両パスで使う製品→フルインデックスの解決を、パス開始前に一括で行う
    # (docs/plans/2026-07-24-cui-phase1.md Task 4 前段修正: 製品ごとのdictルックアップをベクトル化し、
    # 2パス分をまとめて1回のsearchsortedで済ませる)。
    product_raw_ids = np.fromiter(
        (p.raw_id for p in products), dtype=np.int64, count=len(products)
    )
    full_indices = _resolve_full_indices(product_raw_ids, graph)

    class_acc: dict[str, _ClassAcc] = {}

    # 1回目のパス: expanded(製品ごとに独立したvisited set) + parametric_count。
    for prod, full_idx in zip(products, full_indices):
        expanded_weight, saw_parametric = _traverse(int(full_idx), graph, visited=set())

        acc = class_acc.setdefault(prod.ifc_class, _ClassAcc())
        acc.element_count += 1
        acc.est_faces_expanded += expanded_weight
        if saw_parametric:
            acc.parametric_count += 1
        if prod.global_id is not None:
            acc.global_ids.append(prod.global_id)

    # 2回目のパス: unique(全製品で1本のvisited setを共有。entity_id昇順=
    # 決定的な「先着順」処理でモジュールdocstring §4の帰属規則を実現)。
    global_visited: set[int] = set()
    for prod, full_idx in zip(products, full_indices):
        unique_weight, _saw_parametric = _traverse(int(full_idx), graph, visited=global_visited)
        class_acc[prod.ifc_class].est_faces_unique += unique_weight

    stats = [
        ClassScanStats(
            ifc_class=cls,
            element_count=acc.element_count,
            est_faces_expanded=acc.est_faces_expanded,
            est_faces_unique=acc.est_faces_unique,
            parametric_count=acc.parametric_count,
        )
        for cls, acc in class_acc.items()
    ]
    stats.sort(key=lambda s: (-s.est_faces_expanded, s.ifc_class))

    elements = {cls: acc.global_ids for cls, acc in class_acc.items()}
    proxy_names = _compute_proxy_names(products)

    return ScanResult(
        path=str(path),
        file_size=file_size,
        schema=raw.schema,
        stats=stats,
        proxy_names=proxy_names,
        elements=elements,
        total_entities=raw.total_records,
        scan_seconds=raw.elapsed_seconds,
        est_fullopen_bytes=file_size * FULLOPEN_BYTES_MULTIPLIER,
    )


def scan_file(path: str | Path, chunk_size: int = 8 * 2**20) -> ScanResult:
    """`scan_records` + `aggregate_scan` を通した、ファイル1本分の薄い便利関数。

    `scan_seconds` は scan_records の読み取り時間だけでなく本関数の呼び出し
    全体(集計含む)の実測秒数で上書きする — CUIの「診断」フェーズ全体で
    ユーザーが実際に待つ時間に対応させるため(cui-design.md §0 段階A)。
    """
    p = Path(path)
    start = time.perf_counter()
    raw = scan_records(p, chunk_size=chunk_size)
    result = aggregate_scan(raw, path=str(p), file_size=p.stat().st_size)
    result.scan_seconds = time.perf_counter() - start
    return result
