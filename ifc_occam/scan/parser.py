"""STEPレコード(`#id=CLASS(...);` 形の bytes)を解釈し ScanEntity にする
パーサ (cui-design.md §3)。

reader.py の `iter_records` が yield する1件ずつのバイト列を受け取り、
クラス名を3分類(フロンティア/ブロック/中間)して重み・参照・GUID・Name を
抽出する。reader.py の内部実装(`_RecordScanner` 等)は一切importしない
(責務分離。reader は「レコード境界を見つける」、parser は「レコード1件の
中身を読む」)。

## クラス3分類(判定は本モジュールの定数、cui-design.md §3)

1. **フロンティア**(重みを持ち、refsは格納しない。全て厳密一致で判定する。
   前方一致は使わない — `IFCFACE` を前方一致にすると `IFCFACEBOUND` /
   `IFCFACEOUTERBOUND` / `IFCFACETEDBREP` 等を誤って frontier化してしまう
   ため):
   - `IFCFACE`, `IFCFACESURFACE`, `IFCADVANCEDFACE` → weight 1
   - `IFCTRIANGULATEDFACESET` → CoordIndex 属性(0-indexで3番目。
     Coordinates, Normals, Closed, CoordIndex, PnIndex の順。ifcopenshell
     0.8.5 のスキーマ定義で確認済み。IFC4X3ではClosed/Normalsの宣言順が
     入れ替わるが CoordIndex の位置(index=3)は不変)の `),(` 出現数+1を
     三角形数として weight に(属性本文から直接カウント。パースではなく
     count)。Normals にも同形の入れ子タプルが乗ることがあるため、
     レコード全体を無差別にカウントせず CoordIndex 属性だけを対象にする。
   - `IFCPOLYGONALFACESET` → Faces 属性(0-indexで2番目。Coordinates,
     Closed, Faces, PnIndex の順)の要素数(`#ref` のリスト。
     IfcIndexedPolygonalFace は正式なENTITY型なのでインライン値ではなく
     必ず別レコードへの参照になる)を同様にカウント。
   - パラメトリック立体(`IFCEXTRUDEDAREASOLID`, `IFCREVOLVEDAREASOLID`,
     `IFCSWEPTDISKSOLID`, `IFCBOOLEANRESULT`, `IFCBOOLEANCLIPPINGRESULT`,
     `IFCCSGSOLID`) → weight = PARAMETRIC_NOMINAL_TRIS (=16),
     is_parametric=True。
2. **ブロック**(重みなし・refs格納なし。グラフを軽くする):
   点・方向・配置・ループ・境界・エッジ・頂点・スタイル・単位・
   OwnerHistory・プロパティ/数量セット類。定数リスト(`_BLOCK_EXACT`)に
   加え、前方一致の3ファミリー(`_BLOCK_PREFIXES`: `IFCCARTESIANPOINT*`
   `IFCPROPERTY*` `IFCQUANTIT*`)を併用する。`IFCCARTESIANPOINTLIST3D`
   (テッセレーション座標コンテナ)はこの前方一致で正しくブロックされる。
3. **中間**(refsを格納): 上記以外すべて。文字列リテラル内の `#123` は
   参照と誤認しない(文字列スパンを先に空白へ置換してから正規表現で
   `#\\d+` を拾う)。

## GUID/Name抽出

IfcRoot系(GlobalId, OwnerHistory, Name, Description, ... の属性順)は
第1属性が22文字の `[0-9A-Za-z_$]` 文字列になる。この形にマッチした
レコードだけ(cheap gate)、第3属性を Name として追加抽出する。
フロンティア/ブロック分類のクラス(点・ループ・面など)は構造上この形に
なりえないため、クラス種別に関わらずこのゲート判定だけで実質的に
「製品候補クラスのみ」を選別できる。

Name のデコード: 外側の `'...'` を外し `''` → `'` に戻した上で、
`\\X2\\<hex>\\X0\\` (UTF-16BE, 4桁hexの列)をデコードする。ifcopenshell
0.8.5 が実際に復号する値(small.ifc の実レコードで事前確認済み。
tests/test_parser.py 参照)と一致することを確認済み。`\\S\\` (shift) や
`\\PA\\` (alphabet切替)等の他のSTEPエスケープは意図的にデコードせず、
そのままの文字列として残す(仕様外・低頻度のためスコープ外。ドキュメント化
のみ)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class ScanEntity:
    entity_id: int
    ifc_class: str
    refs: tuple[int, ...]
    weight: int
    is_parametric: bool
    global_id: str | None
    name: str | None


PARAMETRIC_NOMINAL_TRIS = 16

# --- クラス3分類の定数テーブル ---

_FRONTIER_FACES = frozenset({
    "IFCFACE",
    "IFCFACESURFACE",
    "IFCADVANCEDFACE",
})
_FRONTIER_TRIANGULATEDFACESET = "IFCTRIANGULATEDFACESET"
_FRONTIER_POLYGONALFACESET = "IFCPOLYGONALFACESET"
_FRONTIER_PARAMETRIC = frozenset({
    "IFCEXTRUDEDAREASOLID",
    "IFCREVOLVEDAREASOLID",
    "IFCSWEPTDISKSOLID",
    "IFCBOOLEANRESULT",
    "IFCBOOLEANCLIPPINGRESULT",
    "IFCCSGSOLID",
})
_FRONTIER_ALL = (
    _FRONTIER_FACES
    | {_FRONTIER_TRIANGULATEDFACESET, _FRONTIER_POLYGONALFACESET}
    | _FRONTIER_PARAMETRIC
)

_BLOCK_EXACT = frozenset({
    # 方向
    "IFCDIRECTION",
    # 配置
    "IFCAXIS1PLACEMENT",
    "IFCAXIS2PLACEMENT2D",
    "IFCAXIS2PLACEMENT3D",
    "IFCLOCALPLACEMENT",
    "IFCGRIDPLACEMENT",
    # ループ
    "IFCPOLYLOOP",
    "IFCEDGELOOP",
    "IFCVERTEXLOOP",
    # 境界
    "IFCFACEBOUND",
    "IFCFACEOUTERBOUND",
    # エッジ
    "IFCEDGE",
    "IFCORIENTEDEDGE",
    "IFCEDGECURVE",
    # 頂点
    "IFCVERTEXPOINT",
    # スタイル
    "IFCSURFACESTYLE",
    "IFCSURFACESTYLERENDERING",
    "IFCSURFACESTYLESHADING",
    "IFCSURFACESTYLEWITHTEXTURES",
    "IFCCURVESTYLE",
    "IFCCURVESTYLEFONTANDSCALING",
    "IFCFILLAREASTYLE",
    "IFCFILLAREASTYLEHATCHING",
    "IFCFILLAREASTYLETILES",
    "IFCFILLAREASTYLETILESYMBOLWITHSTYLE",
    "IFCTEXTSTYLE",
    "IFCTEXTSTYLEFORDEFINEDFONT",
    "IFCTEXTSTYLETEXTMODEL",
    "IFCCOLOURRGB",
    "IFCCOLOURRGBLIST",
    "IFCINDEXEDCOLOURMAP",
    # 単位
    "IFCSIUNIT",
    "IFCCONVERSIONBASEDUNIT",
    "IFCCONVERSIONBASEDUNITWITHOFFSET",
    "IFCDERIVEDUNIT",
    "IFCDERIVEDUNITELEMENT",
    "IFCMONETARYUNIT",
    "IFCMEASUREWITHUNIT",
    "IFCUNITASSIGNMENT",
    # OwnerHistory
    "IFCOWNERHISTORY",
    # プロパティ/数量セット類(前方一致 IFCPROPERTY*/IFCQUANTIT* で
    # 拾えない例外分。IFCCOMPLEXPROPERTY は "IFCCOMPLEX" で始まり
    # "IFCPROPERTY" では始まらない、IFCELEMENTQUANTITY は "IFCELEMENT" で
    # 始まり "IFCQUANTIT" では始まらないため個別に列挙する)
    "IFCELEMENTQUANTITY",
    "IFCCOMPLEXPROPERTY",
    "IFCCOMPLEXPROPERTYTEMPLATE",
})
# 前方一致で判定するファミリー(cui-design.md §3 の例示そのまま)。
# 注意: IFCCARTESIANPOINTLIST3D(テッセレーション座標コンテナ)はこの
# IFCCARTESIANPOINT 前方一致でブロックされる(refsを元々持たないので実害は
# ないが、分類としてもブロックが正しい)。frontierは厳密一致のみで判定する
# ため、この前方一致とfrontier集合が衝突しないことは
# tests/test_parser.py::test_no_frontier_class_is_shadowed_by_a_block_prefix
# で固定している。
_BLOCK_PREFIXES = ("IFCCARTESIANPOINT", "IFCPROPERTY", "IFCQUANTIT")


def _classify(ifc_class: str) -> str:
    """ifc_class(大文字)を "frontier" / "block" / "intermediate" に分類する。"""
    if ifc_class in _FRONTIER_ALL:
        return "frontier"
    if ifc_class in _BLOCK_EXACT or ifc_class.startswith(_BLOCK_PREFIXES):
        return "block"
    return "intermediate"


# --- レコードヘッダ(id/クラス名)の抽出 ---

_HEADER_RE = re.compile(rb"^\s*#(\d+)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


# --- 文字列リテラルの扱い(STEPの '' 二重化エスケープ) ---
#
# `'(?:[^']|'')*'` は「'' か '以外の1文字' の繰り返しに挟まれた文字列」を
# 貪欲に一つのトークンとしてマッチする、'' エスケープ対応の定番パターン。
# reader.py の内部実装(_scan_string 等)はimportせず、ここで独立に定義する
# (責務分離。パーサはレコード全体が既に確定済みのbytesであることを前提に
# でき、reader側のチャンク読みの複雑さを引き継ぐ必要がない)。
_STRING_RE = re.compile(rb"'(?:[^']|'')*'")
_REF_RE = re.compile(rb"#(\d+)")


def _blank_strings(body: bytes) -> bytes:
    """文字列リテラルの中身(引用符含む)を同じ長さの空白に置き換える。

    これにより後段の `#\\d+` 正規表現が文字列内の "#123" のような
    見た目だけの並びを参照と誤認しなくなる(置換後は単なる空白なので
    マッチ対象が残らない)。位置・長さは変えないので、置換後も後続の
    属性分割(_split_top_level)等と併用しても座標がずれない。
    """
    return _STRING_RE.sub(lambda m: b" " * len(m.group(0)), body)


def _extract_refs(body: bytes) -> tuple[int, ...]:
    """中間クラスの refs を抽出する: 文字列を空白化した上で `#\\d+` を
    出現順に全て拾う(入れ子括弧の深さは無関係。正規表現は構造を見ないため
    自然に対応できる)。重複除去はしない(同じ参照が複数回現れた場合も
    そのまま記録する。集計側の責務と分離する)。
    """
    blanked = _blank_strings(body)
    return tuple(int(m.group(1)) for m in _REF_RE.finditer(blanked))


# --- 属性の位置アクセス(GUID/Name/CoordIndex/Facesで使う) ---


_SPLIT_TOKEN_RE = re.compile(rb"['(),]")


def _split_top_level(body: bytes) -> list[bytes]:
    """属性リストを最上位のカンマで分割する(文字列内・括弧の深さを考慮)。

    文字列(`''`二重化対応)の中のカンマ・括弧は無視する。括弧の深さが0の
    位置にあるカンマだけを区切りとして扱う。各要素は前後の空白を除去して
    返す。GUID/Name抽出(ゲートを通った少数のレコードのみ)と
    CoordIndex/Faces抽出(frontierの2クラスのみ)でのみ使う、比較的コストの
    高い操作なので、中間クラスの refs 抽出(大多数のレコード)には使わない。

    reader.py の `_next_record` と同じ考え方で、1バイトずつの Python ループ
    ではなく次の区切り(`'` `(` `)` `,` のいずれか)へジャンプする。最初の
    実装は1バイトずつの while ループで、次に `bytes.find` を4回(各記号ごと)
    呼んで最近傍を選ぶ版を試したが、large.ifc でプロファイルしたところ後者は
    呼び出し回数の割に find の反復コストがかさみ、却って総時間が伸びる
    ケースがあった。1回の正規表現検索(文字クラス `['(),]`)で4記号の最近傍を
    一度に見つける版に落ち着けたところ、実測で最も速かった(このモジュールの
    他のホットパスに比べ `_split_top_level` はGUID保持クラス+稀な2
    frontierクラスのみが通る少数派なので、致命的な影響ではないが、
    1バイトループの明確なアンチパターンは残さない)。
    """
    parts: list[bytes] = []
    depth = 0
    start = 0
    pos = 0
    n = len(body)
    while True:
        m = _SPLIT_TOKEN_RE.search(body, pos)
        if m is None:
            break
        idx = m.start()
        tok = m.group(0)
        if tok == b"'":
            j = idx + 1
            while True:
                close = body.find(b"'", j)
                if close == -1:
                    pos = n  # 未終端文字列。残りは無いものとして打ち切る
                    break
                if body[close + 1:close + 2] == b"'":
                    j = close + 2
                    continue
                pos = close + 1
                break
            continue
        if tok == b"(":
            depth += 1
        elif tok == b")":
            depth -= 1
        elif depth == 0:  # tok == b","
            parts.append(body[start:idx])
            start = idx + 1
        pos = idx + 1
    parts.append(body[start:])
    return [p.strip() for p in parts]


def _count_tuple_groups(attr: bytes) -> int:
    """`((a,b,c),(d,e,f),...)` 形の属性から、最上位のタプル要素数を数える
    (`),(` の出現数+1)。空リスト `()` や `$` は0。CoordIndexに使う。
    """
    inner = attr.strip()
    if not inner or inner in (b"$", b"*"):
        return 0
    if inner.startswith(b"(") and inner.endswith(b")"):
        core = inner[1:-1].strip()
    else:
        core = inner
    if not core:
        return 0
    return core.count(b"),(") + 1


def _count_flat_list(attr: bytes) -> int:
    """`(#3,#4,#5)` 形の属性(参照の単純リスト)から要素数を数える。
    空リスト `()` や `$` は0。Facesに使う。
    """
    inner = attr.strip()
    if not inner or inner in (b"$", b"*"):
        return 0
    if inner.startswith(b"(") and inner.endswith(b")"):
        core = inner[1:-1].strip()
    else:
        core = inner
    if not core:
        return 0
    return core.count(b",") + 1


_COORD_INDEX_ATTR_INDEX = 3  # Coordinates, Normals, Closed, CoordIndex, PnIndex
_FACES_ATTR_INDEX = 2  # Coordinates, Closed, Faces, PnIndex


def _frontier_weight(ifc_class: str, body: bytes) -> tuple[int, bool]:
    """frontierクラスの (weight, is_parametric) を計算する。"""
    if ifc_class in _FRONTIER_FACES:
        return 1, False
    if ifc_class == _FRONTIER_TRIANGULATEDFACESET:
        attrs = _split_top_level(body)
        coord_index = attrs[_COORD_INDEX_ATTR_INDEX] if len(attrs) > _COORD_INDEX_ATTR_INDEX else b""
        return _count_tuple_groups(coord_index), False
    if ifc_class == _FRONTIER_POLYGONALFACESET:
        attrs = _split_top_level(body)
        faces = attrs[_FACES_ATTR_INDEX] if len(attrs) > _FACES_ATTR_INDEX else b""
        return _count_flat_list(faces), False
    if ifc_class in _FRONTIER_PARAMETRIC:
        return PARAMETRIC_NOMINAL_TRIS, True
    return 0, False  # 到達しないはず(呼び出し元が category=="frontier" を保証)


# --- GUID/Name抽出 ---

_GUID_RE = re.compile(rb"^\s*'([0-9A-Za-z_$]{22})'")
_X2_RUN_RE = re.compile(rb"\\X2\\(.*?)\\X0\\", re.DOTALL)
_NAME_ATTR_INDEX = 2  # GlobalId, OwnerHistory, Name, Description, ...


def _decode_x2_runs(s: bytes) -> str:
    """`\\X2\\<hex>\\X0\\` (UTF-16BE, 4桁hexの列)をデコードする。他の
    バイト列は utf-8 として寛容にデコードする(想定はASCIIだが、実データが
    素のUTF-8を含む場合にも例外を出さない)。hexが不正な場合はエスケープを
    そのまま残す(Name抽出はベストエフォートであるべきで、ここで例外を
    投げて全体のスキャンを止めるべきではない)。
    """
    parts: list[str] = []
    last = 0
    for m in _X2_RUN_RE.finditer(s):
        parts.append(s[last:m.start()].decode("utf-8", errors="replace"))
        hexdigits = re.sub(rb"\s+", b"", m.group(1))
        try:
            parts.append(bytes.fromhex(hexdigits.decode("ascii")).decode("utf-16-be"))
        except (ValueError, UnicodeDecodeError):
            parts.append(m.group(0).decode("ascii", errors="replace"))
        last = m.end()
    parts.append(s[last:].decode("utf-8", errors="replace"))
    return "".join(parts)


def _decode_name_attr(raw: bytes) -> str | None:
    """Name属性(第3属性)の生バイト列を人間可読な文字列にデコードする。
    `$` (null) や空は None。外側の `'...'` を外し `''` → `'` に戻した上で
    \\X2\\ ランをデコードする。\\S\\ / \\PA\\ 等の他エスケープは意図的に
    デコードせずそのまま残す(モジュールdocstring参照)。
    """
    raw = raw.strip()
    if not raw or raw == b"$":
        return None
    if len(raw) >= 2 and raw[0:1] == b"'" and raw[-1:] == b"'":
        inner = raw[1:-1]
    else:
        inner = raw  # 想定外の形。あるものをそのままデコードする
    inner = inner.replace(b"''", b"'")
    return _decode_x2_runs(inner)


def _extract_guid_and_name(body: bytes) -> tuple[str | None, str | None]:
    """第1属性が22文字のGUID形文字列の場合のみ global_id/name を抽出する
    (cheap gate: マッチしなければ _split_top_level すら呼ばない)。
    """
    m = _GUID_RE.match(body)
    if not m:
        return None, None
    global_id = m.group(1).decode("ascii")
    attrs = _split_top_level(body)
    name = None
    if len(attrs) > _NAME_ATTR_INDEX:
        name = _decode_name_attr(attrs[_NAME_ATTR_INDEX])
    return global_id, name


# --- 公開API ---


def _match_header(record: bytes) -> tuple[re.Match[bytes], bytes] | None:
    """レコード先頭のヘッダ(id/クラス名)にマッチさせる、parse_recordと
    scan_records(pipeline.py)共有のヘッダ解釈(「壊れたレコード」の判定
    基準そのもの)。id/クラス名/括弧の対応が取れない壊れたレコードは
    None。戻り値は (マッチオブジェクト, strip済みのレコード全体) —
    呼び出し側は `m.group(1)` を entity_id、`m.group(2)` を ifc_class
    (大文字化前)として取り出し、必要なら
    `stripped[m.end():-1]` で body(末尾の `;` と外側の閉じ括弧を除いた
    属性列)を切り出せる。

    scan_records はレコードの85%程度(block/単純frontier)で body を
    一切切り出さない構造的高速化のため、この関数を parse_record と
    共有することで「どこまでが壊れたレコードか」の基準が2箇所で乖離する
    リスクを避ける(片方だけ修正されて食い違う、という事態を防ぐ)。
    """
    stripped = record.strip()
    if stripped.endswith(b";"):
        stripped = stripped[:-1].rstrip()
    m = _HEADER_RE.match(stripped)
    if not m or not stripped.endswith(b")"):
        return None
    return m, stripped


def parse_record(record: bytes) -> ScanEntity | None:
    """1レコード(reader.iter_recordsが返す `#id=CLASS(...);` 形のbytes)を
    解釈し ScanEntity を返す。id/クラス名/括弧の対応が取れない壊れた
    レコードは None(例外を投げない。スキャナ全体を1件の壊れたレコードで
    止めないため)。
    """
    matched = _match_header(record)
    if matched is None:
        return None
    m, stripped = matched

    entity_id = int(m.group(1))
    ifc_class = m.group(2).decode("ascii").upper()
    body = stripped[m.end():-1]

    category = _classify(ifc_class)

    refs: tuple[int, ...] = ()
    weight = 0
    is_parametric = False

    if category == "frontier":
        weight, is_parametric = _frontier_weight(ifc_class, body)
    elif category == "intermediate":
        refs = _extract_refs(body)
    # block: refs=(), weight=0, is_parametric=False (デフォルトのまま)

    global_id, name = _extract_guid_and_name(body)

    return ScanEntity(
        entity_id=entity_id,
        ifc_class=ifc_class,
        refs=refs,
        weight=weight,
        is_parametric=is_parametric,
        global_id=global_id,
        name=name,
    )
