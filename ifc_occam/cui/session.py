"""cui/session.py — 対話セッション(純粋ロジック層)(cui-design.md §6)。

`ScanResult`(軽量スキャンの集計結果、scan/aggregate.py)を受け取り、対話コマンド
文字列を解釈してクラス単位の操作意図(`Intent`)を管理する。stdin/stdout・
ファイルI/O・ifcopenshell へのアクセスは一切行わない(それらは repl.py /
core/export.py の責務)。

対話コマンド一覧(ifc_occam_cui_requirements.md §5、cui-design.md §6):
    delete <クラス名>                            クラス全要素を削除対象に
    bbox <クラス名> [element|shared]             クラス全要素をbbox化対象に
    obb <クラス名> [element|shared]              クラス全要素をOBB軽量化対象に
                                                  (向き付きbbox)
    hull <クラス名> [element|shared]             クラス全要素を凸包化対象に
    decimate <クラス名> <ratio> [element|shared] クラス全要素を間引き対象に
                                                  (ratio: 0.05-0.95)。bbox/hull/
                                                  decimateの末尾scopeキーワードは
                                                  省略時 既定"shared"(共有波及。
                                                  docs/plans/2026-07-31-cui-shared-scope.md)
    keep <クラス名>                              既存の操作指定を解除する
                                                  (明示的な保持マーカー)
    undo [番号]                   操作リストから1件除去(番号省略時は直前に
                                   新規追加した1件。cui-design.md には
                                   専用メソッドが無いため command() 内で解釈する)
    list                         現在の操作リスト表示(render_intents と同一)
    rank                         診断ランキング再表示(render_ranking と同一)

Global Constraint: クラス名の突合は常に upper() で行う(スキャン層のクラス名は
常に大文字。ユーザー入力は大文字小文字非区別)。

同一クラスへの再指定は上書き(後勝ち)。挿入順(=最初にそのクラスへコマンドを
発行した順)は再指定によって変わらない — Python dict の「既存キーへの再代入は
位置を変えない」という素の挙動をそのまま使う。undo(番号省略)はその挿入順で
「最後に新規追加されたクラス」を取り消す(=直前に再指定しただけの既存クラスは
対象にならない)。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from ifc_occam.core.advisor import advise_simplify
from ifc_occam.core.ops import Operation
from ifc_occam.scan.aggregate import ScanResult

__all__ = ["Intent", "CuiSession"]

#: decimate の ratio 許容範囲(cui-design.md §6)。core.ops.validate_operations の
#: 0<ratio<1 より狭い、CUI入力時点での実用的な安全域(桁違いの誤指定を防ぐ)。
_RATIO_MIN = 0.05
_RATIO_MAX = 0.95

#: 不明クラスのエラーメッセージに提示する前方一致候補の最大件数
#: (IFCクラスは全て"IFC"始まりのため、短い誤入力では候補が膨大になり得る)。
#: 超過時は `_unknown_class_error` が末尾に `...他N件` を付し、切断されている
#: ことをユーザーに明示する(持ち越しMinor #1 / 最終レビューM2)。
_CANDIDATE_LIMIT = 10

#: Intent.op(simplify系) → core.ops.Operation の params["method"] 変換表
#: (cui-design.md §6: "bbox/hull/decimate → simplify(method=..., scope=intent.scope)"、
#: 既定 "shared"。docs/plans/2026-07-31-cui-shared-scope.md)。
_SIMPLIFY_METHOD_BY_OP = {
    "bbox": "bbox", "hull": "convex_hull", "decimate": "decimate", "obb": "obb",
}

_SET_OP_LABELS = {"delete": "削除", "bbox": "bbox軽量化", "hull": "凸包化", "obb": "OBB軽量化"}

#: simplify系コマンドの scope 表示ラベル(GUIの操作リストと同じ語彙)。
_SCOPE_LABELS = {"shared": "共有波及", "element": "個別"}

#: Intent.op(simplify系) → core.advisor.advise_simplify の method 変換表
#: (delete/keepは対象外 = 判定文を出さない、task-4-brief.md Step4)。
_ADVISOR_METHOD_BY_OP = {"bbox": "bbox", "hull": "convex_hull", "obb": "obb", "decimate": "decimate"}

#: CUIの注意行の接頭辞(task-4-brief.md Step4)。
_ADVICE_PREFIX = "注意: "


def _display_width(text: str) -> int:
    """端末表示セル数。East Asian Width が F(Fullwidth)/W(Wide) の文字を
    2セル、それ以外を1セルと数える。Python の format 幅指定はコードポイント数
    でパディングするため、全角混じりの操作ラベル(「間引き 0.1(共有波及)」等)
    と短いラベル(「保持」)が並ぶと列がズレる(フェーズ最終レビューM-6)。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text
    )


def _pad_display(text: str, width: int) -> str:
    """_display_width 基準で width セルまで右側に空白を詰める(左寄せ)。
    超過時は切り詰めずそのまま返す(列境界は明示2スペース区切りが保証する —
    render_intents/render_ranking 共通の既存方針)。"""
    return text + " " * max(0, width - _display_width(text))


def _rjust_display(text: str, width: int) -> str:
    """_display_width 基準の右寄せ(ヘッダの数値列見出し用)。"""
    return " " * max(0, width - _display_width(text)) + text


@dataclass
class Intent:
    """1クラスに対する操作意図(cui-design.md §6)。

    scope は bbox/hull/decimate(simplify系)でのみ意味を持つ。既定 "shared"
    (共有波及): 同じ共有形状(IfcRepresentationMap)を使う要素すべてに1回の
    書き換えで波及する(GUIの共有波及と同じ。2026-07-31 実測: large.ifc で
    element 比 サイズ-5.5MB/簡略化1.8倍速/警告708→0件)。"element"(個別)は
    要素ごとに形状を個別化する(コマンド末尾の element キーワード)。
    delete/keep では無視される。
    """

    op: str  # "delete" | "bbox" | "hull" | "decimate" | "obb" | "keep"
    ifc_class: str  # スキャン時の大文字クラス名
    ratio: float | None = None
    scope: str = "shared"


class CuiSession:
    """スキャン結果 + 操作意図を保持する純粋ロジック層(cui-design.md §6)。"""

    def __init__(self, scan: ScanResult) -> None:
        self._scan = scan
        #: クラス名(大文字)→ 要素数。既知クラス集合の唯一の正(command検証・
        #: 確認メッセージ・render_intentsの要素数表示すべてここを参照する)。
        self._element_counts: dict[str, int] = {s.ifc_class: s.element_count for s in scan.stats}
        #: クラス名(大文字)→ Intent。挿入順を保持する(list/undoの番号付けに使う)。
        self._intents: dict[str, Intent] = {}

    # ------------------------------------------------------------------
    # コマンド解釈
    # ------------------------------------------------------------------

    def command(self, line: str) -> str:
        """1コマンドを解釈し、表示用文字列を返す(印字はしない、cui-design.md §6)。"""
        tokens = line.split()
        if not tokens:
            return ""

        verb = tokens[0].lower()
        args = tokens[1:]

        if verb == "delete":
            return self._command_set(args, op="delete")
        if verb == "bbox":
            return self._command_set(args, op="bbox")
        if verb == "hull":
            return self._command_set(args, op="hull")
        if verb == "obb":
            return self._command_set(args, op="obb")
        if verb == "decimate":
            return self._command_decimate(args)
        if verb == "keep":
            return self._command_keep(args)
        if verb == "undo":
            return self._command_undo(args)
        if verb == "list":
            return self.render_intents()
        if verb == "rank":
            return self.render_ranking()
        return f"不明なコマンドです: {tokens[0]}"

    def intents(self) -> list[Intent]:
        """現在有効な操作意図の一覧(挿入順)。"""
        return list(self._intents.values())

    def to_operations(self) -> list[Operation]:
        """Intent → core.ops.Operation 変換(cui-design.md §6)。

        targets は scan.elements(GlobalId列)から取る。op の対応:
        bbox/hull/decimate → simplify(method=..., scope=intent.scope)、
        delete → delete、keep → keep。simplify は intent.scope(既定 shared)、
        delete/keep は従来どおり "element"(意味を持たない)。
        """
        operations: list[Operation] = []
        for intent in self.intents():
            targets = list(self._scan.elements.get(intent.ifc_class, []))
            if intent.op in _SIMPLIFY_METHOD_BY_OP:
                params = {"method": _SIMPLIFY_METHOD_BY_OP[intent.op]}
                if intent.op == "decimate":
                    params["ratio"] = intent.ratio
                operations.append(
                    Operation(op="simplify", targets=targets, scope=intent.scope, params=params)
                )
            else:  # "delete" | "keep"
                operations.append(Operation(op=intent.op, targets=targets, scope="element"))
        return operations

    # ------------------------------------------------------------------
    # 表示用レンダリング
    # ------------------------------------------------------------------

    def render_ranking(self) -> str:
        """rank コマンド用。診断ランキングを整形する(cli.format_report 流儀、
        cui-design.md §6)。

        末尾に proxy 名称内訳(`_render_proxy_name_breakdown`)を追記する
        (docs/plans/2026-07-25-cui-phase2.md Task 3)。`scan.proxy_names` が空の場合は何も追記せず、
        従来出力と完全一致する(後方互換)。
        """
        scan = self._scan
        total_expanded = sum(s.est_faces_expanded for s in scan.stats)

        lines: list[str] = []
        lines.append(f"ファイル: {scan.path} ({scan.file_size} bytes)")
        lines.append(f"スキーマ: {scan.schema}")
        lines.append(f"総エンティティ行数: {scan.total_entities}")
        lines.append(f"スキャン時間: {scan.scan_seconds:.2f}秒")
        lines.append(f"推定フルオープンメモリ: {scan.est_fullopen_bytes} bytes")
        lines.append("")
        lines.append("=== クラス別ランキング (推定Face数[展開]降順) ===")

        if not scan.stats:
            lines.append("(該当クラスなし)")
        else:
            # 各列の間に明示的な2スペース区切りを入れる — クラス名や
            # decimateラベル(ratio埋め込みで可変長)が想定の列幅を超えても、
            # 幅指定のpadding(overflow時は無視される)だけに頼らず次の列との
            # 境界が必ず視認できるようにする。
            # ヘッダは表示セル幅(_display_width、全角=2)基準でパディングする
            # (render_intentsと同じ理由 — formatの幅指定はコードポイント数で
            # 詰めるため全角見出しがASCIIのデータ列と最大31セルズレていた。
            # CF-A最終レビューM-2)。見出しがデータ列幅を超える2列は列ごと
            # 拡幅した(推定Face数(共有統合)=22セル、パラメトリック件数=18セル)。
            lines.append(
                f"{'#':<4}  {_pad_display('クラス名', 32)}  {_rjust_display('要素数', 10)}"
                f"  {_rjust_display('推定Face数(展開)', 18)}"
                f"  {_rjust_display('推定Face数(共有統合)', 22)}"
                f"  {_rjust_display('パラメトリック件数', 18)}  {_rjust_display('寄与率', 8)}"
            )
            for i, s in enumerate(scan.stats, start=1):
                share = (s.est_faces_expanded / total_expanded * 100) if total_expanded else 0.0
                lines.append(
                    f"{i:<4}  {s.ifc_class:<32}  {s.element_count:>10}  {s.est_faces_expanded:>18}"
                    f"  {s.est_faces_unique:>22}  {s.parametric_count:>18}  {share:>7.1f}%"
                )

        lines.extend(self._render_proxy_name_breakdown(scan.proxy_names))
        return "\n".join(lines)

    @staticmethod
    def _render_proxy_name_breakdown(proxy_names: list[tuple[str, int]]) -> list[str]:
        """rank末尾に追記する proxy 名称内訳セクションの行群(docs/plans/2026-07-25-cui-phase2.md Task 3)。

        `proxy_names` が空なら空リストを返す(呼び出し側の出力を一切変えない
        = 従来出力との完全一致を保つ)。非空なら見出し1行 +
        上位5件(各行 `  <キー>  <件数>`) + 6件目以降があれば
        `  ...他N種`(N = len(proxy_names) - 5)の1行を返す。

        キーはNameそのもの、またはNameのタグ接頭辞「【カテゴリ】」
        (`aggregate._compute_proxy_names` 参照。Task 8実測の知見: 連番付き
        Nameの素朴頻度集計は無力だが、タグ接頭辞での集計は同一カテゴリの
        個体を束ねる。docs/cui-measurements.md「Task 8」章)。
        """
        if not proxy_names:
            return []
        lines = ["", "IfcBuildingElementProxy 名称内訳 (上位5)"]
        for key, count in proxy_names[:5]:
            lines.append(f"  {key}  {count}")
        overflow = len(proxy_names) - 5
        if overflow > 0:
            lines.append(f"  ...他{overflow}種")
        return lines

    def render_intents(self) -> str:
        """list コマンド用。現在の操作意図一覧を整形する(cui-design.md §6)。"""
        if not self._intents:
            return "操作はまだありません。"

        # render_rankingと同じ理由(decimateラベルがratio埋め込みで可変長)で
        # 列の間に明示的な2スペース区切りを入れる。パディングは表示セル幅
        # (_display_width、全角=2)基準 — format の :<20 はコードポイント数で
        # 詰めるため全角混じりのラベルで列がズレる(フェーズ最終レビューM-6)。
        # 操作列は22セル: 最長の通常ラベル「間引き 0.NN(共有波及)」が21セル、
        # 3桁小数(例: 0.150)でも22セルに収まる(それ以上の桁は超過時
        # 非切り詰めの既存方針に委ねる)。
        lines: list[str] = ["=== 操作リスト ==="]
        lines.append(
            f"{'#':<4}  {_pad_display('操作', 22)}  {_pad_display('クラス', 32)}"
            f"  {_rjust_display('要素数', 10)}"
        )
        for i, intent in enumerate(self.intents(), start=1):
            count = self._element_counts.get(intent.ifc_class, 0)
            lines.append(
                f"{i:<4}  {_pad_display(self._op_label(intent), 22)}"
                f"  {_pad_display(intent.ifc_class, 32)}  {count:>10}"
            )
        return "\n".join(lines)

    @staticmethod
    def _op_label(intent: Intent) -> str:
        """list 表示用の操作ラベル(日本語)。simplify系は scope を併記する
        (GUIの操作リスト「decimate ratio=0.1 / 共有波及」と同じ情報量)。
        要件定義§5モックの「間引き 0.3」形からの逸脱は
        docs/plans/2026-07-31-cui-shared-scope.md で正式に上書き済み。
        """
        if intent.op == "decimate":
            return f"間引き {intent.ratio}({_SCOPE_LABELS[intent.scope]})"
        if intent.op == "keep":
            return "保持"
        label = _SET_OP_LABELS.get(intent.op, intent.op)
        if intent.op in _SIMPLIFY_METHOD_BY_OP:
            return f"{label}({_SCOPE_LABELS[intent.scope]})"
        return label

    # ------------------------------------------------------------------
    # 内部ヘルパー: クラス名解決
    # ------------------------------------------------------------------

    def _resolve_class(self, raw: str) -> str | None:
        """ユーザー入力を大文字化し、既知クラスと突合する(Global Constraint:
        クラス名の突合は常に upper() で行う)。見つからなければ None。"""
        cls = raw.upper()
        return cls if cls in self._element_counts else None

    @staticmethod
    def _parse_scope(token: str | None) -> str | None:
        """簡略化コマンド末尾の任意キーワードを scope に解決する。

        None(省略)= 既定の "shared"。"element"/"shared"(大文字小文字不問)は
        それぞれの scope。それ以外は None を返し、呼び出し側がエラーにする。
        """
        if token is None:
            return "shared"
        lowered = token.lower()
        if lowered in ("element", "shared"):
            return lowered
        return None

    def _unknown_class_error(self, raw: str) -> str:
        """不明クラスのエラーメッセージ。前方一致候補を最大 `_CANDIDATE_LIMIT`
        件まで提示する。候補がそれを超える場合は切断していることが分かるよう
        末尾に `...他N件`(N=超過件数)を付す(持ち越しMinor #1 / 最終レビュー
        M2、docs/plans/2026-07-25-cui-phase2.md Task 3 同梱要件)。"""
        cls = raw.upper()
        candidates = sorted(c for c in self._element_counts if c.startswith(cls))
        if candidates:
            shown = ", ".join(candidates[:_CANDIDATE_LIMIT])
            overflow = len(candidates) - _CANDIDATE_LIMIT
            if overflow > 0:
                shown += f", ...他{overflow}件"
            return f"不明なクラスです: {cls} (候補: {shown})"
        return f"不明なクラスです: {cls} (候補なし)"

    # ------------------------------------------------------------------
    # 内部ヘルパー: 適正判定(advisor.py連携、task-4-brief.md Step4)
    # ------------------------------------------------------------------

    def _avg_faces_per_element(self, ifc_class: str) -> float | None:
        """そのクラスの軽量スキャン統計(render_rankingが使っているのと同じ行データ)
        から 推定Face数[展開] / 要素数 を返す。データが無ければ(クラスがscan.stats
        に無い、または要素数0で除算不能)None を返す。"""
        for s in self._scan.stats:
            if s.ifc_class == ifc_class:
                if s.element_count == 0:
                    return None
                return s.est_faces_expanded / s.element_count
        return None

    def _append_advice(self, message: str, ifc_class: str, op: str) -> str:
        """確認メッセージの末尾に、advise_simplifyが返す注意文を「注意: 」接頭辞付きの
        行として連結する。CUIはサンプル実測(hull_triangle_ratio/obb_volume_ratio)を
        持たないため、triangle_source=None・avg_triangles_per_shapeのみ渡す
        (発火し得るのはdecimateの低密度警告のみ)。delete/keepはop対象外
        (_ADVISOR_METHOD_BY_OPに無い)なので何も付かない。"""
        method = _ADVISOR_METHOD_BY_OP.get(op)
        if method is None:
            return message
        avg = self._avg_faces_per_element(ifc_class)
        advice_messages = advise_simplify(method, avg_triangles_per_shape=avg)
        if not advice_messages:
            return message
        return "\n".join([message, *(f"{_ADVICE_PREFIX}{m}" for m in advice_messages)])

    # ------------------------------------------------------------------
    # 内部ヘルパー: 各コマンドの実装
    # ------------------------------------------------------------------

    def _command_set(self, args: list[str], *, op: str) -> str:
        """delete/bbox/hull 共通。bbox/hull は末尾に任意の scope キーワード
        (element=個別化 / shared=既定の共有波及の明示)を取れる。delete は
        取らない(削除に波及の概念はなく、連鎖はapply側の閉包計算が担う)。"""
        takes_scope = op in _SIMPLIFY_METHOD_BY_OP
        usage = f"使い方: {op} <クラス名>" + (" [element|shared]" if takes_scope else "")
        max_args = 2 if takes_scope else 1
        if not (1 <= len(args) <= max_args):
            return usage
        cls = self._resolve_class(args[0])
        if cls is None:
            return self._unknown_class_error(args[0])
        count = self._element_counts[cls]
        if takes_scope:
            scope = self._parse_scope(args[1] if len(args) == 2 else None)
            if scope is None:
                return f"不明な指定です: {args[1]}({usage})"
            self._intents[cls] = Intent(op=op, ifc_class=cls, scope=scope)
            message = (
                f"{cls} {count}要素を{_SET_OP_LABELS[op]}対象に追加しました"
                f"({_SCOPE_LABELS[scope]})。"
            )
            return self._append_advice(message, cls, op)
        self._intents[cls] = Intent(op=op, ifc_class=cls)
        message = f"{cls} {count}要素を{_SET_OP_LABELS[op]}対象に追加しました。"
        return self._append_advice(message, cls, op)

    def _command_decimate(self, args: list[str]) -> str:
        usage = "使い方: decimate <クラス名> <ratio> [element|shared]"
        if not (2 <= len(args) <= 3):
            return usage
        cls = self._resolve_class(args[0])
        if cls is None:
            return self._unknown_class_error(args[0])
        try:
            ratio = float(args[1])
        except ValueError:
            return f"decimate の ratio は数値で指定してください: {args[1]!r}"
        if not (_RATIO_MIN <= ratio <= _RATIO_MAX):
            return (
                f"decimate の ratio は {_RATIO_MIN}~{_RATIO_MAX} の範囲で"
                f"指定してください: {ratio}"
            )
        scope = self._parse_scope(args[2] if len(args) == 3 else None)
        if scope is None:
            return f"不明な指定です: {args[2]}({usage})"
        self._intents[cls] = Intent(op="decimate", ifc_class=cls, ratio=ratio, scope=scope)
        count = self._element_counts[cls]
        percent = ratio * 100
        message = (
            f"{cls} {count}要素を間引き(残{percent:.0f}%)対象に追加しました"
            f"({_SCOPE_LABELS[scope]})。"
        )
        return self._append_advice(message, cls, "decimate")

    def _command_keep(self, args: list[str]) -> str:
        if len(args) != 1:
            return "使い方: keep <クラス名>"
        cls = self._resolve_class(args[0])
        if cls is None:
            return self._unknown_class_error(args[0])
        self._intents[cls] = Intent(op="keep", ifc_class=cls)
        count = self._element_counts[cls]
        return f"{cls} {count}要素を保持し、既存の操作指定を解除しました。"

    def _command_undo(self, args: list[str]) -> str:
        if len(args) > 1:
            return "使い方: undo [番号]"
        if not self._intents:
            return "取り消す操作がありません。"

        if not args:
            # 番号省略時は挿入順で最後に新規追加されたクラスを取り消す。
            # 既存クラスの再指定は挿入順を変えない(モジュールdocstring参照)ため、
            # 「直近に更新されただけ」のクラスは対象にならない。
            cls = next(reversed(self._intents))
            del self._intents[cls]
            return f"{cls} の操作を取り消しました。"

        try:
            n = int(args[0])
        except ValueError:
            return f"undo の番号は整数で指定してください: {args[0]!r}"

        order = list(self._intents.keys())
        if not (1 <= n <= len(order)):
            return f"#{n} は範囲外です(現在{len(order)}件)。"

        cls = order[n - 1]
        del self._intents[cls]
        return f"#{n} ({cls}) を取り消しました。"
