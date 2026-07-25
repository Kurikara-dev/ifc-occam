"""操作リスト (design.md §3, Phase 3 契約)。純粋・ifcopenshell 非依存。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

_VALID_OPS = {"delete", "simplify", "keep"}
_VALID_SCOPES = {"element", "shared"}
_VALID_SIMPLIFY_METHODS = {"bbox", "convex_hull", "decimate"}


@dataclass
class Operation:
    """1回の操作。op="simplify" の params 例: {"method": "bbox", "ratio": 0.3}。"""

    op: str  # "delete" | "simplify" | "keep"
    targets: list[str]  # GlobalId のリスト
    scope: str = "element"  # "element" | "shared"
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "targets": list(self.targets),
            "scope": self.scope,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Operation":
        return cls(
            op=d["op"],
            targets=list(d["targets"]),
            scope=d.get("scope", "element"),
            params=dict(d.get("params", {})),
        )


def ops_to_json(operations: list[Operation]) -> str:
    """操作リスト全体を JSON 文字列化。順序・params を保持。"""
    return json.dumps([op.to_dict() for op in operations], ensure_ascii=False)


def ops_from_json(payload: str) -> list[Operation]:
    """JSON 文字列から操作リストを復元。順序・params を保持。"""
    raw = json.loads(payload)
    return [Operation.from_dict(d) for d in raw]


def resolve_effective(operations: list[Operation]) -> dict[str, Operation]:
    """gid ごとの有効操作を返す。リストの後方が勝つ(last-wins)。

    keep は「対象外に確定」を表すマーカーであり、それ自体が結果に op="keep" として
    含まれる(それ以前の delete/simplify を打ち消す)。
    """
    effective: dict[str, Operation] = {}
    for op in operations:
        for gid in op.targets:
            effective[gid] = op
    return effective


def validate_operations(operations: list[Operation], known_gids: set[str]) -> list[str]:
    """操作リストを検証し、警告文字列のリストを返す。例外は投げない。正常系は []。"""
    warnings: list[str] = []

    for op in operations:
        if op.op not in _VALID_OPS:
            warnings.append(f"不正な op です: {op.op!r}")

        for gid in op.targets:
            if gid not in known_gids:
                warnings.append(f"未知の GlobalId です: {gid!r}")

        if op.scope not in _VALID_SCOPES:
            warnings.append(f"不正な scope です: {op.scope!r}")

        if op.op == "simplify":
            method = op.params.get("method")
            if method not in _VALID_SIMPLIFY_METHODS:
                warnings.append(f"不正な simplify method です: {method!r}")
            elif method == "decimate":
                ratio = op.params.get("ratio")
                if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
                    warnings.append(
                        f"decimate には数値の ratio (0<ratio<1) が必要です: {ratio!r}"
                    )
                elif not (0 < ratio < 1):
                    warnings.append(
                        f"decimate の ratio は 0<ratio<1 である必要があります: {ratio!r}"
                    )

    return warnings
