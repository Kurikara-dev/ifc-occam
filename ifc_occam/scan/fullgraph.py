"""全レコード参照グラフの軽量スキャン (cui-design.md §8予告、
docs/plans/2026-07-25-cui-phase3.md Task 1)。

Phase 3(テキストレベル削除)の土台。`scan_records`(pipeline.py)/
`aggregate_scan`(aggregate.py)は「block/単純frontierのrefsを捨てる」構造的
高速化を前提にした**部分的な**グラフ(中間クラスのみrefsを持つ)だが、本
モジュールはテキストレベル削除(cui-design.md §8: 専有サブツリー回収 +
IFCREL* 参照リスト修正)のために**全レコード**(block/frontier含む)の参照
関係を必要とするため、別モジュールとして独立させた(既存の
scan_records/aggregate_scanの契約・性能特性を変えないため)。

## 抽出する内容(意図的に軽い)

reader.py の `iter_records` が yield する各レコードから、parser.py の
`_match_header`(id/クラス名の抽出。壊れたレコードの判定基準そのものを
parse_record/scan_recordsと共有)と `_extract_refs`(文字列リテラルを
空白化してから `#\\d+` を出現順に全て拾う。重複除去はしない)をそのまま
再利用し、「クラス名」と「参照先idの列」だけを取り出す。GUID/Name/
CoordIndex等の属性の意味解釈は一切行わない(parser.pyのフル解釈より軽い
——scan_full_graphの唯一の関心は参照関係であり、Name等はテキスト削除の
判断に使わないため)。

refsはbody(レコードの外側の丸括弧の中身。ヘッダの `#id=CLASS(` 自体は
含まない)から抽出する。レコード全体(ヘッダ含む)に対して同じ正規表現を
かけると、レコード自身の `#id=` の数字を「自己参照」と誤認する(全レコード
が無条件に自己参照を持つことになってしまう)ため、これは誤り。自己参照
(`#10=...(#10)`のようにbody内に自分のidが再度現れる場合)は特別扱いせず
そのまま保持する——後段(Task 2のカウントダウン方式による専有サブグラフ
回収)がこれを自然に処理できるため、ここで特殊処理を入れる必要はない。

## refsは重複除去しない・出現回数がそのままin_degreeになる

`_extract_refs` は同じ参照が1レコード内に複数回現れても除去しない(集計側
の責務と分離、というparser.py自身の設計)。本モジュールもこれを継承し、
`in_degree` は「解決済み参照の**出現回数**」として計算する(ユニークな
参照元レコード数ではない)。これは意図的な選択であり、Task 2 の専有サブ
グラフ回収(カウントダウン方式)が `alive_ref_count = in_degree.copy()` から
出発し、dead と判定されたレコードの各参照**出現**を1つずつ減算していく
設計を前提にしているため——出現回数と正確に一致していなければ、カウント
ダウンが0に到達するタイミングがずれてしまう。

## レコード規模のPython dict/object listを作らない

`ids`/`class_codes`/`ref_indptr`/`ref_targets`/`in_degree` はすべて
numpy配列(またはCSR)であり、レコード数・参照数に比例するPythonオブジェクト
(list[ScanEntity]のような)は保持しない。ストリーミング中の中間バッファは
`array.array("q", ...)`(pipeline.pyの`face_ids`と同型。C配列であり
Pythonオブジェクトを1件ずつ持たない)を使う。`class_table`/`class_index`
(クラス名→コード)はクラス**種類数**のみに比例する(IFCスキーマの語彙数は
有界。実データで数百程度)ため、record規模の禁止規律の例外として許容される
(aggregate.pyのraw_id_to_full_index dict撤去、docs/plans/2026-07-25-cui-phase3.md
Task 1 の明示指定と同じ理由づけ)。

## ids整列への並べ替え(ファイル記述順 → id昇順)とCSRの再構成

reader.iter_recordsはファイル中の記述順でレコードを返すが、`ids`(および
`class_codes`/`ref_indptr`/`in_degree`)の契約は「id昇順」である。ストリーム
走査中はファイル記述順のまま `array.array` に詰め、走査完了後に
`np.argsort` で1回だけ並べ替える。可変長の行(各レコードのrefs)を並べ替える
際、Pythonループで行ごとにリストを組み替えるとレコード規模のPythonオブジェクト
操作になってしまうため、`np.repeat` で「新しい行番号→元の開始位置」を
ベクトル化して求め、`flat_refs_raw[src_positions]` の1回のfancy indexingで
CSR全体を並べ替える(要素ごとのPythonループを避ける)。

参照解決(生のid→`ids`上のindex)は aggregate.py `_build_graph`
(L266-269付近。タスクブリーフは「L250-253付近」と引用しているが、これは
`_build_graph`内でrefsを重複除去する箇所であり、実際にsearchsorted+clamp+
等値チェックを行っている箇所はL266-269——おそらく引用後にコメント追加で
行番号がずれた。内容面では本docstringが指す箇所で相違ない)と同じ
searchsorted + clamp + 等値チェックのミスガードパターンを使う:
`searchsorted` で挿入位置を求め、`ids`の範囲外にclampしてから実際にその
位置の値が一致するかを確認する(一致しなければ解決不能=-1)。
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ifc_occam.scan.parser import _extract_refs, _match_header
from ifc_occam.scan.reader import iter_records

__all__ = ["FullGraph", "scan_full_graph"]


@dataclass(slots=True)
class FullGraph:
    """全レコード参照グラフ(cui-phase3 Task 1 契約。フィールド名/型は
    docs/plans/2026-07-25-cui-phase3.md Task 1 からverbatim)。

    ids/class_codes/ref_indptr(の行)/in_degree は同じ並び(id昇順)に
    整列している。ref_targets は `ids` 上のindexであり、record idそのもの
    ではない(record idに戻すには `ids[target]` を引く)。
    """

    ids: np.ndarray  # (n,) int64 昇順ソート済み record id
    class_codes: np.ndarray  # (n,) int32、ids に整列。class_table のインデックス
    class_table: list[str]  # 大文字クラス名のインターン表(クラス種類数のみ=有界)
    ref_indptr: np.ndarray  # (n+1,) int64  CSR
    ref_targets: np.ndarray  # (m,) int64  ids 上の index。解決不能参照は -1
    in_degree: np.ndarray  # (n,) int64  被参照数(解決済み参照のみ、出現回数)
    record_count: int


def scan_full_graph(path: str | Path) -> FullGraph:
    """path のDATAセクション全レコードを1回走査し、参照グラフを構築する。

    壊れたレコード(`_match_header` がNoneを返す——id/クラス名/括弧の対応が
    取れないもの)は静かに読み飛ばす(scan_records/parse_recordと同じ規約)。
    `record_count` は解釈できたレコード数のみを数える(壊れたレコードは
    含まない)。
    """
    ids_buf: array = array("q")  # 各レコードのid(ファイル記述順)
    class_code_buf: array = array("q")  # 各レコードのクラスコード(ファイル記述順)
    ref_len_buf: array = array("q")  # 各レコードのrefs件数(ファイル記述順)
    flat_refs_buf: array = array("q")  # 全レコードのrefsを連結した生id列(ファイル記述順)

    class_table: list[str] = []
    class_index: dict[str, int] = {}  # クラス名→コード。クラス種類数のみに比例(有界)

    for record in iter_records(path):
        matched = _match_header(record)
        if matched is None:
            continue  # 壊れたレコード。scan_records/parse_recordと同じ扱いで無視
        m, stripped = matched

        ifc_class = m.group(2).decode("ascii").upper()
        code = class_index.get(ifc_class)
        if code is None:
            code = len(class_table)
            class_table.append(ifc_class)
            class_index[ifc_class] = code

        body = stripped[m.end():-1]  # 外側の丸括弧の中身のみ(ヘッダの#idは含まない)
        refs = _extract_refs(body)  # 文字列ブランク済み、出現順、重複除去なし

        ids_buf.append(int(m.group(1)))
        class_code_buf.append(code)
        ref_len_buf.append(len(refs))
        flat_refs_buf.extend(refs)

    n = len(ids_buf)

    ids_unsorted = np.array(ids_buf, dtype=np.int64)
    class_codes_unsorted = np.array(class_code_buf, dtype=np.int32)
    ref_lengths_unsorted = np.array(ref_len_buf, dtype=np.int64)
    flat_refs_raw = np.array(flat_refs_buf, dtype=np.int64)

    order = np.argsort(ids_unsorted, kind="stable")
    ids = ids_unsorted[order]
    class_codes = class_codes_unsorted[order]
    ref_lengths = ref_lengths_unsorted[order]

    # ファイル記述順でのCSR開始位置(元の各行がflat_refs_raw中のどこから
    # 始まるか)。並べ替え後の行の並びから、この配列を order で引くことで
    # 「新しい行番号 → 元の開始位置」が求まる。
    orig_indptr = np.zeros(n + 1, dtype=np.int64)
    orig_indptr[1:] = np.cumsum(ref_lengths_unsorted)

    ref_indptr = np.zeros(n + 1, dtype=np.int64)
    ref_indptr[1:] = np.cumsum(ref_lengths)
    total_refs = int(ref_indptr[-1])

    # 可変長の行をPythonループ無しで並べ替える(モジュールdocstring参照):
    # 新フラット配列の各位置がどの新行に属すかを row_of_pos で求め、行内での
    # 相対位置(within_row)と元の開始位置(old_row_starts)を足すことで、
    # 元のflat_refs_raw上の絶対位置(src_positions)を1回のベクトル演算で得る。
    row_of_pos = np.repeat(np.arange(n, dtype=np.int64), ref_lengths)
    within_row = np.arange(total_refs, dtype=np.int64) - ref_indptr[row_of_pos]
    old_row_starts = orig_indptr[order]
    src_positions = old_row_starts[row_of_pos] + within_row
    flat_targets_raw = flat_refs_raw[src_positions]

    # 参照解決: searchsorted + clamp + 等値チェックのミスガードパターン
    # (aggregate.py _build_graph と同型)。
    resolved = np.searchsorted(ids, flat_targets_raw)
    resolved_clamped = np.minimum(resolved, n - 1)
    valid = (resolved < n) & (ids[resolved_clamped] == flat_targets_raw)
    ref_targets = np.where(valid, resolved_clamped, -1).astype(np.int64)

    in_degree = np.bincount(ref_targets[ref_targets >= 0], minlength=n).astype(np.int64)

    return FullGraph(
        ids=ids,
        class_codes=class_codes,
        class_table=class_table,
        ref_indptr=ref_indptr,
        ref_targets=ref_targets,
        in_degree=in_degree,
        record_count=n,
    )
