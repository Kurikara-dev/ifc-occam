"""プリセット (design.md phase4 plan §プリセット, Task 3)。純粋・ifcopenshell 非依存。

プリセット = 選択条件(match)+推奨操作(op)のルール列。resolve_preset は現在の
ModelData に対して各ルールがマッチする要素のGlobalIdを返すだけの純粋関数で、
実際の適用(操作リストへの追加)は呼び出し側(サーバAPI/UI)の責務とする
(§0: 適用前に必ず人間確認)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ifc_occam.core.types import ElementInfo, ModelData

# resolve_preset がサポートする match キー。増やす場合はここと _matches を揃える。
_SUPPORTED_MATCH_KEYS = {"ifc_class", "layer", "min_triangles"}


@dataclass
class PresetRule:
    """1本のルール。match の各キーはAND結合。op は Operation.to_dict() 形式
    (targets は空でよい。適用時に resolve_preset の結果で解決する)。"""

    match: dict
    op: dict

    def to_dict(self) -> dict:
        return {"match": dict(self.match), "op": dict(self.op)}

    @classmethod
    def from_dict(cls, d: dict) -> "PresetRule":
        return cls(match=dict(d["match"]), op=dict(d["op"]))


@dataclass
class Preset:
    name: str
    description: str
    rules: list[PresetRule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "rules": [rule.to_dict() for rule in self.rules],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            rules=[PresetRule.from_dict(r) for r in d.get("rules", [])],
        )


# ---------------------------------------------------------------------------
# マッチング (純粋関数)
# ---------------------------------------------------------------------------


def _matches(match: dict, elem: ElementInfo, model: ModelData) -> bool:
    if "ifc_class" in match and elem.ifc_class != match["ifc_class"]:
        return False
    if "layer" in match and elem.layer != match["layer"]:
        return False
    if "min_triangles" in match:
        if elem.shape_id is None:
            return False
        shape = model.shapes.get(elem.shape_id)
        if shape is None or shape.triangle_count < match["min_triangles"]:
            return False
    return True


def resolve_preset(
    preset: Preset, model: ModelData
) -> tuple[list[tuple[PresetRule, list[str]]], list[str]]:
    """プリセットの各ルールについて、現在の ModelData でマッチする要素の
    GlobalId リストを返す。適用はしない(人間確認用)。

    戻り値: (ルールごとの(PresetRule, gidリスト)のリスト, プリセット全体の警告リスト)。

    match の未知キーを含むルールは何にもマッチしない(gids=[])とし、
    警告文字列を第2要素に積む(例外は投げない)。
    """
    results: list[tuple[PresetRule, list[str]]] = []
    warnings: list[str] = []

    for idx, rule in enumerate(preset.rules):
        unknown_keys = sorted(k for k in rule.match if k not in _SUPPORTED_MATCH_KEYS)
        if unknown_keys:
            for key in unknown_keys:
                warnings.append(f"ルール{idx}: 不明なmatchキーです: {key!r}")
            results.append((rule, []))
            continue

        gids = [elem.global_id for elem in model.elements if _matches(rule.match, elem, model)]
        results.append((rule, gids))

    return results, warnings


# ---------------------------------------------------------------------------
# JSON シリアライズ (presets.json 全体)
# ---------------------------------------------------------------------------


def presets_to_json(presets: list[Preset]) -> str:
    """プリセット列全体を JSON 文字列化する(日本語をエスケープしない)。"""
    return json.dumps([p.to_dict() for p in presets], ensure_ascii=False, indent=2)


def presets_from_json(payload: str) -> list[Preset]:
    """JSON 文字列からプリセット列を復元する。"""
    raw = json.loads(payload)
    return [Preset.from_dict(d) for d in raw]


# ---------------------------------------------------------------------------
# ファイルI/O (presets.json, UTF-8/BOMなし, git管理外)
# ---------------------------------------------------------------------------


def load_presets(path: str | Path) -> list[Preset]:
    """presets.json を読み込む。ファイルが無ければ空リストを返す(例外にしない)。"""
    p = Path(path)
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8")
    return presets_from_json(text)


def save_presets(path: str | Path, presets: list[Preset]) -> None:
    """presets.json へ全置換で書き込む。tmp+rename でアトミックに行う
    (書き込み中のクラッシュで既存ファイルを壊さない)。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(presets_to_json(presets), encoding="utf-8")
    tmp.replace(p)
