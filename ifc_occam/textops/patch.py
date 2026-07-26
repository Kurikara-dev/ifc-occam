"""IFCREL* レコードの参照リストをバイト列レベルでパッチする
(cui-design.md §8、docs/plans/2026-07-25-cui-phase3.md Task 3)。

`patch_rel_record(record, dead_ids) -> bytes | None` は、Task 2
(`ifc_occam/textops/plan.py`)の `compute_text_delete_plan` が
`patch_rel_ids` に列挙した候補 IFCREL* レコード1件ごとに、Task 4
(ストリーム書き換え)が呼ぶ純粋関数。record 自身の生バイト列(reader.py の
`iter_records` が返す `#id=CLASS(...);` 形。先頭の空白・コメントは除去済み、
末尾に `;` が付き、レコード内部の改行・空白は保持される)と、削除確定済みの
record id 集合(dead_ids。Task 2 `TextDeletePlan.drop_ids` を想定)だけを
入力に完結する——`FullGraph` も `TextDeletePlan` も ifcopenshell も import
しない。record 自身のクラス名(`IFCREL` で始まるか)も一切見ない: 呼び出し
元(Task 4)が `patch_rel_ids` で既に候補を絞り込んでいる前提であり、本関数は
「与えられたバイト列」だけを純粋に構造解析する。

## 契約(brief 規則1-5)

1. 文字列リテラル(`'...'`、`''` エスケープ)内は絶対に触らない。
2. 属性トップレベルの括弧リスト内の dead `#id` トークンを除去し、カンマを
   正規化する(`(#1,#2,#3)` で #2 dead → `(#1,#3)`; 全滅 → 空リスト)。
3. 除去の結果、参照リストが空 `()` になった → None(レコードごとdrop)。
4. リスト外(単独属性)の dead 参照が残る → None(レコードごとdrop)。
5. dead 参照が無い場合は入力をそのまま返す(同一オブジェクト、コピーしない)。

## 監督者裁定の実装

- **dead_ids のメンバシップ判定**: `dead_ids` が `np.ndarray` の場合、
  「昇順ソート済み」(`TextDeletePlan.drop_ids` の契約)を前提に
  searchsorted + クランプ + 等値ガードで判定する(aggregate.py/
  fullgraph.py/plan.py と同型のミスガードパターン)。drop_ids はレコード
  規模(数百万件)になり得るうえ、本関数は patch 候補 rel 1件ごとに呼ばれる
  ホットパスであるため、ndarray を Python の set/dict へ変換することは
  意図的に避ける——1回の呼び出しコストは「そのレコード内の参照数」に比例
  させ、`len(dead_ids)` には比例させない。`frozenset`(またはそれに類する
  集合型)の場合は素の `in` を使う(O(1)想定)。
- **スキャン範囲**: 参照の探索は必ず body(`stripped[m.end():-1]`、ヘッダの
  自分自身の `#id=` を含まない)に限定する。ヘッダを含めてスキャンすると
  自己idを参照と誤認し、自己drop(=ファイル破損)を引き起こす。
- **出力形**: `stripped[:m.end()] + 新body + b");"`。除去部分以外の改行・
  空白は verbatim に保つ。レコード先頭/末尾の空白が落ちるのは
  Global Constraints(レコード間区切りの改行1つへの正規化)で許容される。
- **壊れたレコード**: 入力をそのまま返す(同一オブジェクト。壊さない・
  落とさない側に倒す)。2種類あり、到達可能性が異なる:
    - 外形が壊れている(`_match_header` が None = id/クラス名/外側括弧の
      対応が取れない): Task 1 の `parse_record` も同じ `_match_header` を
      使うため、このレコードはグラフに載らず `patch_rel_ids` にも入らない。
      通常到達しない。
    - body 内部の括弧が対応していない(`(` が閉じないまま `)` が余る等):
      **こちらは通常到達しうる**。Task 1 の `parse_record`/`_extract_refs`
      は body 内部の括弧バランスを検証しない(正規表現で `#\\d+` を拾うだけ)
      ため、内部不整合レコードは「パース成功」としてグラフに載り、
      `patch_rel_ids` の候補にもなり得る(Task 3 レビューで実測確認済み)。
      この分岐は理論上の保険ではなく実在する安全網であり、1件の壊れた
      レコードで Task 4 のストリーム書き換え全体を例外で止めないための
      ものである。**残存する副作用**: このレコードは verbatim で出力される
      ため dead 参照が残り、出力に dangling 参照が生じ得る。入力が既に
      スキーマ違反であるケースに限られるので「入力より悪化させない」側を
      選んでいる(drop すると被参照側を孤立させ得るため)。
- **`dead_ids` の昇順ソート前提が破れた場合**: `np.searchsorted` は前提が
  崩れても例外を投げず**無音で誤判定**する(dead を alive と誤り、出力に
  dangling 参照が残る)。本関数は裁定1(1回の呼び出しコストを「そのレコード
  内の参照数」に比例させる)の帰結として per-call のソート検証を行わない。
  現状 `TextDeletePlan.drop_ids` は `plan.py` の `np.sort` で保証している
  が、この前提はストリーム開始前に**1回だけ**(O(k))検証する責務を呼び出し元
  (Task 4 `rewrite_without`)が持つ。
- **ネスト括弧(属性内タプル)**: 除去は深さ非依存——dead トークンは、それを
  直接囲んでいる括弧リストから除去し、カンマ正規化もそのリスト内で行う。
  一方、規則3/4は**属性トップレベル位置**でのみ判定する:
    - 規則3 = トップレベル属性であるリスト(body 直下、深さ1の括弧)が除去の
      結果 空 `()` になった → None。
    - 規則4 = dead 参照がどの括弧にも入らず(深さ0)、トップレベル属性として
      直接置かれている → None。
  深さ2以上のネストしたリストが除去の結果空 `()` になった場合、その `()`
  は親リストから取り除かず、そのまま残す(親が空になったと判定してNoneには
  ならない)。実データの IFCREL* にネストした参照リストは現れないため、
  これは **文書化済みの限界(YAGNI)** とする——深さ2以上のリストが丸ごと
  空になり、かつそれが唯一の内容であるような親リストがある場合、親リスト
  自身は(空の `()` を要素として持つため)空とは判定されず、規則3は発火
  しない。
- **カンマ正規化の具体規則**: dead トークンの span と「直後のカンマ+その
  カンマ直後の空白」を一緒に除去する。直後にカンマが無い(リスト末尾)場合は
  「直前のカンマ+トークン直前の空白」を除去する。複数の除去スパンが隣接・
  重複する場合(例: `(#1,#2,#3,#4)` で #2,#3 が同時に dead)でも、除去スパン
  を開始位置でソートしてから1回でスプライスするため、リスト内に先頭カンマ・
  末尾カンマ・`,,` が現れない不変条件を保つ。
- **規則5の同一性**: dead 参照が無い場合(`dead_ids` が空の場合を含む)は、
  同じオブジェクト(`record` そのもの)を返す。コピーしない——`dead_ids` が
  空の場合は body の走査すら行わない早期returnの高速路にしている。
"""

from __future__ import annotations

import re

import numpy as np

from ifc_occam.scan.parser import _blank_strings, _match_header

__all__ = ["patch_rel_record"]

# `(` / `)` と `#\d+`(参照トークン、ブランク済みbody上でのみ意味を持つ)を
# 1つの正規表現で走査する。parser.py の `_extract_refs` は参照の「値」だけを
# 出現順に返すが、本モジュールは「どの括弧に直接囲まれているか(depth/group)」
# も必要なため、`_extract_refs` をそのまま使うのではなく、括弧の深さ追跡を
# 兼ねた専用の正規表現を新たに定義する(parser.py の重複実装ではない——
# `_extract_refs` が提供しない情報(位置・深さ)を要求するため)。
_STRUCT_RE = re.compile(rb"([()])|#(\d+)")


def _is_dead_mask(values: np.ndarray, dead_ids: "np.ndarray | frozenset[int]") -> np.ndarray:
    """values(このレコード内で出現した参照idの列、int64)それぞれについて
    dead_ids に含まれるかを bool 配列で返す。

    dead_ids が np.ndarray の場合は「昇順ソート済み」(TextDeletePlan.drop_ids
    の契約)を前提に searchsorted + クランプ + 等値ガードで判定する(record
    規模になり得る dead_ids を Python の set/dict へ変換しない——モジュール
    docstring 参照)。frozenset(または他の集合型)の場合は素の `in` で判定
    する。
    """
    if isinstance(dead_ids, np.ndarray):
        if dead_ids.size == 0:
            return np.zeros(values.shape, dtype=bool)
        pos = np.searchsorted(dead_ids, values)
        pos_clamped = np.minimum(pos, dead_ids.size - 1)
        return (pos < dead_ids.size) & (dead_ids[pos_clamped] == values)
    return np.array([int(v) in dead_ids for v in values], dtype=bool)


def _removal_span(body: bytes, start: int, end: int, g_start: int, g_end: int) -> tuple[int, int]:
    """1つの dead `#id` トークン(body上の [start,end))を、それが直接属する
    括弧リストの範囲 [g_start,g_end)(開き括弧の直後〜閉じ括弧の直前まで、
    閉じ括弧自身は含まない)内で除去する際の除去スパンを返す(監督者裁定の
    カンマ正規化)。

    直後のカンマ(+その直後の空白)を優先して一緒に除去する。直後にカンマが
    無い(リスト末尾)場合は直前のカンマ(+トークン直前の空白)を一緒に除去
    する。どちらも無い(このトークンがリスト内唯一の要素)場合はトークン
    自身の span だけを返す。
    """
    pos = end
    while pos < g_end and body[pos:pos + 1].isspace():
        pos += 1
    if pos < g_end and body[pos:pos + 1] == b",":
        pos += 1
        while pos < g_end and body[pos:pos + 1].isspace():
            pos += 1
        return start, pos

    pos = start
    while pos > g_start and body[pos - 1:pos].isspace():
        pos -= 1
    if pos > g_start and body[pos - 1:pos] == b",":
        return pos - 1, end

    return start, end


def _splice(data: bytes, start: int, end: int, deletions: list[tuple[int, int]]) -> bytes:
    """data[start:end] から、この範囲に完全に収まる deletions を取り除いた
    bytes を返す(除去スパン以外は verbatim)。deletions は開始位置で昇順
    ソート済みの前提(隣接・重複するスパンがあっても、cursor を
    `max(cursor, d_end)` で単調に進めることで正しく1回のスプライスに
    まとめる)。
    """
    parts: list[bytes] = []
    cursor = start
    for d_start, d_end in deletions:
        if d_start < start or d_end > end:
            continue  # この範囲(グループ)に属さない除去スパンは無視する
        parts.append(data[cursor:d_start])
        cursor = max(cursor, d_end)
    parts.append(data[cursor:end])
    return b"".join(parts)


def patch_rel_record(record: bytes, dead_ids: "np.ndarray | frozenset[int]") -> bytes | None:
    """record(1件のIFCREL*候補レコード、`#id=CLASS(...);` 形のbytes)から、
    dead_ids に含まれる参照を除去する。

    戻り値: パッチ済みレコード(変更不要なら入力と同一オブジェクト) /
    None(レコードごとdrop)。モジュールdocstring参照。
    """
    if len(dead_ids) == 0:
        return record  # 規則5(裁定7): dead_idsが空。bodyの走査すら行わない

    matched = _match_header(record)
    if matched is None:
        return record  # 壊れたレコード。壊さない・落とさない側に倒す

    m, stripped = matched
    body = stripped[m.end():-1]
    blanked = _blank_strings(body)  # 文字列リテラルを同じ長さの空白に置換

    # --- 単一パスで (a) 括弧の入れ子構造(groups) と (b) 各参照トークンの
    #     位置・値・直接の属するgroup・深さ(refs) を同時に組み立てる ---
    groups: dict[int, tuple[int, int]] = {}  # gid -> (content_start, content_end)
    refs: list[tuple[int, int, int, "int | None", int]] = []  # (value, start, end, gid, depth)
    stack: list[tuple[int, int]] = []  # (gid, content_start)
    next_gid = 0

    for tm in _STRUCT_RE.finditer(blanked):
        paren = tm.group(1)
        if paren == b"(":
            stack.append((next_gid, tm.end()))
            next_gid += 1
        elif paren == b")":
            if not stack:
                return record  # 対応しない閉じ括弧。壊れた入力、防御的に諦める
            gid, g_start = stack.pop()
            groups[gid] = (g_start, tm.start())
        else:
            value = int(tm.group(2))
            gid = stack[-1][0] if stack else None
            depth = len(stack)
            refs.append((value, tm.start(), tm.end(), gid, depth))

    if not refs:
        return record  # 規則5: 参照が無い

    values = np.array([r[0] for r in refs], dtype=np.int64)
    dead_mask = _is_dead_mask(values, dead_ids)

    if not dead_mask.any():
        return record  # 規則5: dead参照が無い

    deletions: list[tuple[int, int]] = []
    empty_check_groups: set[int] = set()

    for (_value, start, end, gid, depth), is_dead in zip(refs, dead_mask.tolist()):
        if not is_dead:
            continue
        if depth == 0:
            return None  # 規則4: リスト外(単独属性)のdead参照
        g_start, g_end = groups[gid]
        deletions.append(_removal_span(body, start, end, g_start, g_end))
        if depth == 1:
            empty_check_groups.add(gid)

    deletions.sort()

    # 規則3: 属性トップレベル(depth==1)のリストが除去の結果 空 () になった
    # かを確認する。depth>=2 のネストしたリストは対象外(裁定5: 文書化済み
    # の既知の限界)。
    for gid in empty_check_groups:
        g_start, g_end = groups[gid]
        remaining = _splice(body, g_start, g_end, deletions)
        if remaining.strip() == b"":
            return None

    new_body = _splice(body, 0, len(body), deletions)
    return stripped[:m.end()] + new_body + b");"
