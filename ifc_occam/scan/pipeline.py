"""レコード単位の `parse_record` を束ね、ファイル全体を一括で走査する
バルクパス(監督者指示による性能最適化。docs/plans/2026-07-24-cui-phase1.md
Task 2 参照)。

## なぜ parser.py と別モジュールにしたか

`parser.py` は「レコード1件の中身を読む」契約(cui-design.md §3)に専念して
おり、既存の85テストがその契約に対して書かれている。本モジュールは
「ファイル全体をどう走査するか」という別の関心事(reader.iter_records の
呼び出し・カテゴリ別の集計・HEADER の schema 抽出・タイミング計測)を担う。
`parser.py` の分類テーブルと補助関数(`_match_header` / `_classify` /
`_frontier_weight` / `_extract_refs` / `_extract_guid_and_name`)は import
して再利用する(分類ロジックの単一の真実源を保つ。二重定義すると片方だけ
改修され乖離するリスクがあるため)。`_match_header` は「どこまでが壊れた
レコードか」の判定基準そのものを parse_record と共有するために
parser.py 側に切り出した(docs/plans/2026-07-24-cui-phase1.md Task 2)。
reader.py の内部実装は(parser.py と同じ理由で)import しない —
`iter_records` という公開契約のみに依存する。

## 構造的な高速化の要点

cProfile 診断(large.ifc)によれば、レコードの約85%は「点・方向・配置・
ループ・境界・エッジ・頂点・スタイル・単位・OwnerHistory・プロパティ/
数量セット類(block)」または「単純frontier(IFCFACE/IFCFACESURFACE/
IFCADVANCEDFACE、weight常に1)」のいずれかであり、この2カテゴリは
クラス別カウント(+単純frontierはID記録)以外の作業を一切必要としない。
従来の `parse_record` はカテゴリに関わらず必ず (a) body のスライス,
(b) GUID/Name抽出の正規表現呼び出し, (c) ScanEntity データクラスの
構築、を行っていた。本モジュールはヘッダ(id/クラス名)を1回だけ安価に
読み取った直後にカテゴリ分岐し、block/単純frontierではそれ以上何もしない
(body すら切り出さない)。パラメトリック/テッセレーション系frontierと
中間クラスのみ、従来と同じ重み計算・refs抽出を行う。

この結果、RawScan は block/単純frontier について ScanEntity を一切
生成しない(GUID/Name も抽出しない)。これは `parse_record` 単体の契約
(全カテゴリで ScanEntity を返す)とは意図的に異なる、バルクパス専用の
軽量な契約である。GlobalId/Name は実データ上そもそも frontier/block
クラス(点・面・配置等、IfcRootを継承しないジオメトリ表現アイテム)には
現れないため、この省略による実害はない。
"""

from __future__ import annotations

import re
import time
from array import array
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ifc_occam.scan.parser import (
    ScanEntity,
    _FRONTIER_FACES,
    _classify,
    _extract_guid_and_name,
    _extract_refs,
    _frontier_weight,
    _match_header,
)
from ifc_occam.scan.reader import iter_records

__all__ = ["RawScan", "scan_records"]


@dataclass(slots=True)
class RawScan:
    """`scan_records` の戻り値(docs/plans/2026-07-24-cui-phase1.md Task 2)。

    class_counts: 全クラス(block含む)のレコード件数。
    face_ids: 単純frontier(IFCFACE/IFCFACESURFACE/IFCADVANCEDFACE、
        weightは常に1)の entity_id 列。集計側は「このIDに1票」として
        weightを解決できる(id→weight-1 resolvable)。
    weighted: パラメトリック/テッセレーション系frontier
        (IFCTRIANGULATEDFACESET / IFCPOLYGONALFACESET / パラメトリック
        立体6種)の (entity_id, weight, is_parametric) 列。
    entities: 中間クラスのみの ScanEntity 列(refs/GUID/Name保持)。
        block/frontierはここに現れない(ScanEntity自体を生成しない)。
    schema: HEADER の FILE_SCHEMA から取り出した schema識別子
        (例 "IFC4")。見つからなければ空文字列。
    total_records: 解釈できた(=class_countsに数えた)レコード総数。
        壊れたレコード(id/クラス名/括弧の対応が取れない)は含まない
        (parse_record が None を返すのと同じ扱い)。
    elapsed_seconds: scan_records 呼び出し全体(schema抽出含む)の実測秒数。
    """

    class_counts: dict[str, int]
    face_ids: array
    weighted: list[tuple[int, int, bool]]
    entities: list[ScanEntity]
    schema: str
    total_records: int
    elapsed_seconds: float


_SCHEMA_PEEK_BYTES = 8192
_SCHEMA_RE = re.compile(rb"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'")


def _extract_schema(path: str | Path) -> str:
    """HEADER の `FILE_SCHEMA(('IFC4'));` から schema識別子を軽量に取り出す。

    reader.py は HEADER を読まない設計(cui-design.md §2)なので、ここで
    独立に先頭 `_SCHEMA_PEEK_BYTES` バイトだけを読む(「安いつまみ読み」。
    実データでは HEADER はファイル先頭のごく近くにあるため、DATA
    セクション全体を読み進める必要はない)。文字列/コメント認識は行わない
    単純な正規表現探索(HEADER内でこの前提が崩れるのは非現実的なため、
    reader.py のような quote-aware な走査は不要と判断)。見つからなければ
    空文字列を返す(例外を投げない。schemaはベストエフォートの付随情報)。
    """
    with open(path, "rb") as f:
        head = f.read(_SCHEMA_PEEK_BYTES)
    m = _SCHEMA_RE.search(head)
    if not m:
        return ""
    return m.group(1).decode("ascii", errors="replace")


def scan_records(path: str | Path, chunk_size: int = 8 * 2**20) -> RawScan:
    """ファイル全体を1回走査し、カテゴリ別に振り分けた `RawScan` を返す。

    reader.iter_records(path, chunk_size) が yield する各レコードに対し、
    parse_record と同じヘッダ(id/クラス名)の抽出を1回だけ行い、
    `_classify` の3分類で分岐する:
      - block: class_counts のみ加算(bodyすら切り出さない)。
      - frontier かつ単純frontier(IFCFACE系): class_counts加算 +
        face_ids に entity_id を追記(weightは常に1なので集計側で解決)。
      - frontier かつパラメトリック/テッセレーション系: bodyを切り出し
        `_frontier_weight` で重みを計算し、weighted に
        (entity_id, weight, is_parametric) を追記。
      - intermediate: bodyを切り出し `_extract_refs` / `_extract_guid_and_name`
        で従来通り ScanEntity を構築し entities に追記。

    id/クラス名/括弧の対応が取れない壊れたレコードは(parse_record が
    None を返すのと同じ基準で)静かに読み飛ばす。どのカテゴリにも
    数えないため、class_counts の値の合計は常に total_records に一致する。
    """
    start = time.perf_counter()
    schema = _extract_schema(path)

    class_counts: defaultdict[str, int] = defaultdict(int)
    face_ids: array = array("q")
    weighted: list[tuple[int, int, bool]] = []
    entities: list[ScanEntity] = []
    total_records = 0

    for record in iter_records(path, chunk_size=chunk_size):
        matched = _match_header(record)
        if matched is None:
            continue  # 壊れたレコード。parse_record が None を返す場合と同じ扱いで無視
        m, stripped = matched

        ifc_class = m.group(2).decode("ascii").upper()
        category = _classify(ifc_class)

        class_counts[ifc_class] += 1
        total_records += 1

        if category == "block":
            continue

        entity_id = int(m.group(1))

        if category == "frontier":
            if ifc_class in _FRONTIER_FACES:
                face_ids.append(entity_id)
            else:
                body = stripped[m.end():-1]
                weight, is_parametric = _frontier_weight(ifc_class, body)
                weighted.append((entity_id, weight, is_parametric))
            continue

        # intermediate: parse_record と同じ内容の ScanEntity を構築する
        body = stripped[m.end():-1]
        refs = _extract_refs(body)
        global_id, name = _extract_guid_and_name(body)
        entities.append(
            ScanEntity(
                entity_id=entity_id,
                ifc_class=ifc_class,
                refs=refs,
                weight=0,
                is_parametric=False,
                global_id=global_id,
                name=name,
            )
        )

    elapsed = time.perf_counter() - start
    return RawScan(
        class_counts=dict(class_counts),
        face_ids=face_ids,
        weighted=weighted,
        entities=entities,
        schema=schema,
        total_records=total_records,
        elapsed_seconds=elapsed,
    )
