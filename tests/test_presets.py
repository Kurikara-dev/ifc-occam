"""core/presets.py のテスト (phase4 plan Task3)。純粋関数とファイルI/Oのみ。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from ifc_occam.core.ops import (
    _VALID_OPS,
    _VALID_SCOPES,
    _VALID_SIMPLIFY_METHODS,
)
from ifc_occam.core.presets import (
    Preset,
    PresetRule,
    delete_preset,
    load_presets,
    presets_from_json,
    presets_to_json,
    resolve_preset,
    save_presets,
)
from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo

_PRESET_SAMPLES_PATH = Path(__file__).resolve().parent.parent / "web" / "preset-samples.json"


def _model() -> ModelData:
    small_f = np.array([[0, 1, 2]], dtype=np.int64)  # triangle_count=1
    big_f = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)  # 4
    v = np.eye(4, 3)
    shapes = {
        "small": ShapeInfo("small", v, small_f),
        "big": ShapeInfo("big", v, big_f),
    }
    identity = np.eye(4)
    elements = [
        ElementInfo("G1", "IfcPipeFitting", "PF-1", "small", False, ("SweptSolid",), "Layer-A", placement=identity),
        ElementInfo("G2", "IfcPipeFitting", "PF-2", "big", False, ("SweptSolid",), "Layer-B", placement=identity),
        ElementInfo("G3", "IfcLightFixture", "LF-1", "small", False, ("SweptSolid",), "Layer-A", placement=identity),
        ElementInfo("G4", "IfcWall", None, None, False, (), None, placement=None),  # 幾何なし
    ]
    return ModelData(schema="IFC4", elements=elements, shapes=shapes)


# --- resolve_preset: マッチング ---------------------------------------------


def test_resolve_ifc_class_match():
    preset = Preset("p", "d", [PresetRule(match={"ifc_class": "IfcPipeFitting"}, op={"op": "delete"})])
    results, warnings = resolve_preset(preset, _model())
    assert warnings == []
    assert len(results) == 1
    rule, gids = results[0]
    assert sorted(gids) == ["G1", "G2"]


def test_resolve_layer_match():
    preset = Preset("p", "d", [PresetRule(match={"layer": "Layer-A"}, op={"op": "delete"})])
    results, warnings = resolve_preset(preset, _model())
    assert warnings == []
    _, gids = results[0]
    assert sorted(gids) == ["G1", "G3"]


def test_resolve_min_triangles_match_excludes_geometryless():
    preset = Preset("p", "d", [PresetRule(match={"min_triangles": 4}, op={"op": "delete"})])
    results, warnings = resolve_preset(preset, _model())
    assert warnings == []
    _, gids = results[0]
    # big shape(4三角形) のみが閾値を満たす。幾何なし要素(G4)はマッチしない。
    assert gids == ["G2"]


def test_resolve_and_combination_requires_all_keys():
    preset = Preset(
        "p",
        "d",
        [PresetRule(match={"ifc_class": "IfcPipeFitting", "layer": "Layer-A"}, op={"op": "delete"})],
    )
    results, warnings = resolve_preset(preset, _model())
    assert warnings == []
    _, gids = results[0]
    assert gids == ["G1"]


def test_resolve_no_match_returns_empty_gids():
    preset = Preset("p", "d", [PresetRule(match={"ifc_class": "IfcDoor"}, op={"op": "delete"})])
    results, warnings = resolve_preset(preset, _model())
    assert warnings == []
    _, gids = results[0]
    assert gids == []


def test_resolve_unknown_key_matches_nothing_and_warns():
    preset = Preset("p", "d", [PresetRule(match={"color": "red"}, op={"op": "delete"})])
    results, warnings = resolve_preset(preset, _model())
    assert len(warnings) == 1
    assert "color" in warnings[0]
    _, gids = results[0]
    assert gids == []


def test_resolve_multiple_rules_independent():
    preset = Preset(
        "p",
        "d",
        [
            PresetRule(match={"ifc_class": "IfcLightFixture"}, op={"op": "delete"}),
            PresetRule(match={"ifc_class": "IfcPipeFitting"}, op={"op": "simplify", "params": {"method": "bbox"}}),
        ],
    )
    results, warnings = resolve_preset(preset, _model())
    assert warnings == []
    assert len(results) == 2
    assert results[0][1] == ["G3"]
    assert sorted(results[1][1]) == ["G1", "G2"]


# --- JSON roundtrip ----------------------------------------------------------


def test_preset_to_dict_from_dict_roundtrip():
    preset = Preset("p1", "説明", [PresetRule(match={"ifc_class": "IfcWall"}, op={"op": "delete"})])
    d = preset.to_dict()
    restored = Preset.from_dict(d)
    assert restored == preset


def test_presets_to_json_from_json_roundtrip():
    presets = [
        Preset("p1", "説明1", [PresetRule(match={"ifc_class": "IfcWall"}, op={"op": "delete"})]),
        Preset("p2", "説明2", []),
    ]
    text = presets_to_json(presets)
    # 日本語がエスケープされずそのまま入っていること(ensure_ascii=False相当)
    assert "説明1" in text
    restored = presets_from_json(text)
    assert restored == presets


def test_presets_to_json_is_valid_json():
    presets = [Preset("p", "d", [])]
    text = presets_to_json(presets)
    json.loads(text)  # 例外にならないこと


# --- ファイルI/O --------------------------------------------------------------


def test_load_presets_missing_file_returns_empty(tmp_path):
    path = tmp_path / "no_such_presets.json"
    assert load_presets(path) == []


def test_save_and_load_presets_roundtrip(tmp_path):
    path = tmp_path / "presets.json"
    presets = [
        Preset("CFD用", "什器・照明を削除", [PresetRule(match={"ifc_class": "IfcLightFixture"}, op={"op": "delete"})])
    ]
    save_presets(path, presets)
    assert load_presets(path) == presets
    # UTF-8 (BOMなし) で書かれていること
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "什器" in text


# --- delete_preset (GUI改修Task6: 削除API) -----------------------------------


def test_delete_preset_removes_named_preset_and_persists(tmp_path):
    path = tmp_path / "presets.json"
    save_presets(path, [Preset("a", "dA", []), Preset("b", "dB", [])])

    result = delete_preset(path, "a")

    assert result == [Preset("b", "dB", [])]
    # ファイルにも反映されている(保存まで行う)こと
    assert load_presets(path) == [Preset("b", "dB", [])]


def test_delete_preset_unknown_name_returns_unchanged_list(tmp_path):
    path = tmp_path / "presets.json"
    save_presets(path, [Preset("a", "dA", [])])

    result = delete_preset(path, "存在しない名前")

    assert result == [Preset("a", "dA", [])]
    assert load_presets(path) == [Preset("a", "dA", [])]


def test_delete_preset_missing_file_returns_empty_list(tmp_path):
    path = tmp_path / "no_such_presets.json"

    result = delete_preset(path, "x")

    assert result == []


def test_delete_preset_supports_japanese_name_with_spaces_and_slash(tmp_path):
    path = tmp_path / "presets.json"
    save_presets(path, [Preset("テスト プリセット/名前", "d", []), Preset("keep", "d", [])])

    result = delete_preset(path, "テスト プリセット/名前")

    assert result == [Preset("keep", "d", [])]


# --- web/preset-samples.json の整合性 -----------------------------------------


def test_preset_samples_json_parses_as_preset_list():
    payload = _PRESET_SAMPLES_PATH.read_text(encoding="utf-8")
    presets = presets_from_json(payload)
    assert len(presets) >= 1
    for preset in presets:
        assert isinstance(preset, Preset)
        assert preset.name
        assert isinstance(preset.rules, list)


def test_preset_samples_rules_pass_ui_validity_rules():
    payload = _PRESET_SAMPLES_PATH.read_text(encoding="utf-8")
    presets = presets_from_json(payload)
    assert presets, "preset-samples.json にプリセットが1件も無い"

    for preset in presets:
        assert preset.rules, f"プリセット{preset.name!r}にルールが1件も無い"
        for rule in preset.rules:
            op = rule.op
            assert op.get("op") in _VALID_OPS, f"不正な op: {op!r}"
            scope = op.get("scope", "element")
            assert scope in _VALID_SCOPES, f"不正な scope: {op!r}"
            if op.get("op") == "simplify":
                method = op.get("params", {}).get("method")
                assert method in _VALID_SIMPLIFY_METHODS, f"不正な simplify method: {op!r}"


def test_save_presets_overwrites_atomically(tmp_path):
    path = tmp_path / "presets.json"
    save_presets(path, [Preset("a", "d", [])])
    save_presets(path, [Preset("b", "d", [])])
    result = load_presets(path)
    assert [p.name for p in result] == ["b"]
    # tmp ファイルが残っていないこと
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
