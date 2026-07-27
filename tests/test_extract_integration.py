import numpy as np
import pytest
import ifcopenshell.api
import ifcopenshell.geom

from ifc_occam.core.extract import (
    _dominant_diffuse,
    _graph_fallback_diffuse,
    extract_elements_light,
    extract_model,
)
from tests.fixtures_ifc import (
    build_millimeter_single_element_ifc,
    build_single_element_with_child_styled_brep_ifc,
    build_single_element_with_different_top_and_child_styles_ifc,
    build_single_element_with_styled_item_ifc,
    build_wall_with_window_ifc,
)


_IDENTITY_MATRIX_FLAT = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


class _FakeGeometry:
    def __init__(self, gid):
        self.id = gid
        self.verts = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        self.faces = [0, 1, 2]
        self.materials = ()  # _dominant_diffuse が触れる属性。空なら None を返す。


class _FakeTransformation:
    def __init__(self):
        self.matrix = list(_IDENTITY_MATRIX_FLAT)


class _FakeElem:
    def __init__(self, guid, gid):
        self.guid = guid
        self.geometry = _FakeGeometry(gid)
        self.transformation = _FakeTransformation()


class _FastShape:
    def __init__(self, gid):
        self.geometry = _FakeGeometry(gid)
        self.transformation = _FakeTransformation()


def _fast_create_shape(settings, product):
    """本物の create_shape の代わりに高速なダミー形状を返す(フォールバック経路を高速化)。"""
    return _FastShape(f"fallback-{product.GlobalId}")


class _FakeIteratorInitFalse:
    """initialize() が False を返すケースを再現するフェイクイテレータ。"""

    def __init__(self, *args, **kwargs):
        pass

    def initialize(self):
        return False

    def get(self):  # pragma: no cover - 呼ばれてはならない
        raise AssertionError("get() should not be called when initialize() is False")

    def next(self):  # pragma: no cover - 呼ばれてはならない
        raise AssertionError("next() should not be called when initialize() is False")


class _FakeIteratorRecovers:
    """途中で get() が例外を出しても next() で継続できることを確かめるフェイク。"""

    def __init__(self, *args, **kwargs):
        self.get_calls = 0
        self.next_calls = 0
        self._pos = 0

    def initialize(self):
        self._pos = 0
        return True

    def get(self):
        self.get_calls += 1
        if self._pos == 1:
            raise RuntimeError("boom-mid-stream")
        return _FakeElem(f"guid{self._pos}", f"shape{self._pos}")

    def next(self):
        self.next_calls += 1
        self._pos += 1
        return self._pos < 3


class _FakeIteratorNextAlsoFails:
    """get() 失敗後の next() も失敗し続ける場合に走査を打ち切ることを確かめるフェイク。"""

    def __init__(self, *args, **kwargs):
        self.get_calls = 0
        self.next_calls = 0

    def initialize(self):
        return True

    def get(self):
        self.get_calls += 1
        raise RuntimeError("boom-always")

    def next(self):
        self.next_calls += 1
        raise RuntimeError("next-boom")


def test_extract_small_ifc_invariants(small_ifc_path):
    model, warnings = extract_model(small_ifc_path)

    assert model.schema in ("IFC4", "IFC2X3")
    assert len(model.elements) > 0

    gids = [e.global_id for e in model.elements]
    assert len(gids) == len(set(gids)), "GlobalId must be unique"

    for e in model.elements:
        if e.shape_id is not None:
            assert e.shape_id in model.shapes

    assert any(s.triangle_count > 0 for s in model.shapes.values())

    for s in model.shapes.values():
        assert s.vertices.ndim == 2 and s.vertices.shape[1] == 3
        assert s.faces.ndim == 2 and s.faces.shape[1] == 3
        if len(s.faces):
            assert s.faces.max() < len(s.vertices)


def test_elements_with_geometry_have_placement_matrix(small_ifc_path):
    """幾何のある全要素は (4,4) の同次変換行列を placement に持つ。"""
    model, _ = extract_model(small_ifc_path)

    with_geometry = [e for e in model.elements if e.shape_id is not None]
    assert len(with_geometry) > 0

    for e in with_geometry:
        assert e.placement is not None, f"{e.global_id}: placement is None"
        assert isinstance(e.placement, np.ndarray)
        assert e.placement.shape == (4, 4)
        assert e.placement.dtype == np.float64


def test_placement_matrix_is_finite_homogeneous_transform(small_ifc_path):
    """placement の回転/平行移動部分は有限値で、最下行は同次変換の規約 [0,0,0,1]。"""
    model, _ = extract_model(small_ifc_path)

    with_geometry = [e for e in model.elements if e.shape_id is not None]
    for e in with_geometry:
        assert np.all(np.isfinite(e.placement)), f"{e.global_id}: non-finite placement"
        np.testing.assert_allclose(e.placement[3], [0.0, 0.0, 0.0, 1.0])


def test_warnings_are_strings(small_ifc_path):
    _, warnings = extract_model(small_ifc_path)
    assert all(isinstance(w, str) for w in warnings)


def test_iterator_initialize_false_emits_warning(small_ifc_path, monkeypatch):
    """iterator.initialize() が False の場合、フォールバックへ移行した旨を警告に積む。"""
    monkeypatch.setattr(ifcopenshell.geom, "iterator", _FakeIteratorInitFalse)
    monkeypatch.setattr(ifcopenshell.geom, "create_shape", _fast_create_shape)

    model, warnings = extract_model(small_ifc_path)

    assert any("initialize" in w for w in warnings)
    assert len(model.elements) > 0


def test_iterator_mid_stream_exception_continues_via_next(small_ifc_path, monkeypatch):
    """get() が途中で例外を出しても、警告を積んで next() で継続することを確かめる。"""
    created: list[_FakeIteratorRecovers] = []

    def _factory(*args, **kwargs):
        inst = _FakeIteratorRecovers()
        created.append(inst)
        return inst

    monkeypatch.setattr(ifcopenshell.geom, "iterator", _factory)
    monkeypatch.setattr(ifcopenshell.geom, "create_shape", _fast_create_shape)

    model, warnings = extract_model(small_ifc_path)

    fake = created[0]
    # 例外が起きた要素をスキップしつつ、その前後の要素は処理し続けている
    assert fake.get_calls == 3
    assert fake.next_calls == 3
    assert any("boom-mid-stream" in w for w in warnings)
    assert len(model.elements) > 0


def test_iterator_next_failure_after_exception_breaks_bulk_loop(small_ifc_path, monkeypatch):
    """get() 失敗直後の next() も失敗する場合はバルク走査を打ち切り、無限ループしない。"""
    created: list[_FakeIteratorNextAlsoFails] = []

    def _factory(*args, **kwargs):
        inst = _FakeIteratorNextAlsoFails()
        created.append(inst)
        return inst

    monkeypatch.setattr(ifcopenshell.geom, "iterator", _factory)
    monkeypatch.setattr(ifcopenshell.geom, "create_shape", _fast_create_shape)

    model, warnings = extract_model(small_ifc_path)

    fake = created[0]
    assert fake.get_calls == 1
    assert fake.next_calls == 1
    assert any("boom-always" in w for w in warnings)
    assert any("next-boom" in w for w in warnings)
    # バルク走査を打ち切った分は個別フォールバックで拾われ、要素が失われない
    assert len(model.elements) > 0


@pytest.mark.slow
def test_per_element_fallback_real_coverage_full_small_ifc(small_ifc_path, monkeypatch):
    """iterator.initialize() を意図的に失敗させ、small.ifc 全体を本物の
    create_shape フォールバック経路で走らせる(遅い・実カバレッジ確認用)。
    通常スイートからは除外(`-m "not slow"` が既定)、`-m slow` で実行する。
    """
    monkeypatch.setattr(ifcopenshell.geom, "iterator", _FakeIteratorInitFalse)
    # create_shape は本物のまま(monkeypatch しない)。

    model, warnings = extract_model(small_ifc_path)

    assert any("initialize" in w for w in warnings)
    assert len(model.elements) > 0

    with_geometry = [e for e in model.elements if e.shape_id is not None]
    assert len(with_geometry) > 0
    for e in with_geometry:
        assert e.shape_id in model.shapes
        assert e.placement is not None
        assert e.placement.shape == (4, 4)
        assert np.all(np.isfinite(e.placement))
        np.testing.assert_allclose(e.placement[3], [0.0, 0.0, 0.0, 1.0])


def test_extract_reads_the_diffuse_colour_from_styles():
    """IfcStyledItem の色が ElementInfo.color に入る。"""
    f = build_single_element_with_styled_item_ifc(rgb=(1.0, 0.0, 0.0))
    model, _warnings = extract_model(f)
    element = model.elements[0]
    assert element.color is not None
    r, g, b = element.color
    assert (round(r, 3), round(g, 3), round(b, 3)) == (1.0, 0.0, 0.0)


def test_extract_reads_colour_when_the_style_is_on_a_child_item():
    """スタイルが内側の IfcClosedShell に付いていても色が取れる(Rebro出力の形)。"""
    f = build_single_element_with_child_styled_brep_ifc(rgb=(0.0, 0.25, 1.0))
    model, _warnings = extract_model(f)
    r, g, b = model.elements[0].color
    assert (round(r, 3), round(g, 3), round(b, 3)) == (0.0, 0.25, 1.0)


def test_extract_leaves_colour_none_without_styles():
    """幾何はあるがスタイルが無い要素の color は None。

    以前は build_millimeter_single_element_ifc を使っていたが、あれは Body の
    Items が空で幾何生成自体が失敗する(shape_id=None)ため、色の解決を一度も
    通らずに color is None が自明に成立していた(レビュー指摘: テストが何も
    検証していなかった)。スタイルの有無だけが違う状態を作るため、子アイテムに
    スタイルが付いたフィクスチャから IfcStyledItem を取り除いて使う。
    幾何が生成されていること(shape_id が None でないこと)を先に確認して、
    同じ穴に落ちないようにする。
    """
    f = build_single_element_with_child_styled_brep_ifc()
    for styled_item in list(f.by_type("IfcStyledItem")):
        f.remove(styled_item)
    model, _warnings = extract_model(f)
    element = model.elements[0]
    assert element.shape_id is not None
    assert element.color is None


def test_extract_prefers_top_level_colour_over_child_when_both_are_styled():
    """トップレベルと子アイテムで色が違う場合、トップレベルの色が勝つ(統合テスト)。

    この構成では geom自体がトップレベルのスタイルを直接解決するため(子は見ない)、
    end-to-endで「トップレベルの色が勝つ」ことを固定する回帰テスト。ただし
    この経路では _graph_fallback_diffuse は発火しない(geomが先に解決するため)。
    _styled_items_in_subtree の「自分->子」の順序保証そのものを直接確認するのは
    下の test_graph_fallback_diffuse_prefers_top_level_over_child。
    """
    f = build_single_element_with_different_top_and_child_styles_ifc(
        top_rgb=(1.0, 0.0, 0.0), child_rgb=(0.0, 1.0, 0.0)
    )
    model, _warnings = extract_model(f)
    r, g, b = model.elements[0].color
    assert (round(r, 3), round(g, 3), round(b, 3)) == (1.0, 0.0, 0.0)


def test_graph_fallback_diffuse_prefers_top_level_over_child():
    """_graph_fallback_diffuse を直接呼ぶ単体テスト(geomを経由しない)。

    トップレベルと子アイテムで色が違うフィクスチャの product を直接渡し、
    _styled_items_in_subtree の「自分(トップレベル)->子」の先行順により
    トップレベルの色が返ることを確認する。extract_model/ifcopenshell.geom を
    経由しないため、geomの一次解決に頼らず _graph_fallback_diffuse 自体の
    順序保証を検証できる。
    """
    f = build_single_element_with_different_top_and_child_styles_ifc(
        top_rgb=(1.0, 0.0, 0.0), child_rgb=(0.0, 1.0, 0.0)
    )
    product = f.by_type("IfcBuildingElementProxy")[0]

    rgb = _graph_fallback_diffuse(product)

    assert rgb is not None
    assert (round(rgb[0], 3), round(rgb[1], 3), round(rgb[2], 3)) == (1.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# extract_elements_light (CUI Phase1 Task5): メッシュ化しない軽量要素列挙。
# ifcopenshell.geom を一切使わないこと(呼ばれたら例外にするテストで検証)。
# ---------------------------------------------------------------------------


def test_extract_elements_light_returns_gid_class_name_tuples():
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]

    rows = extract_elements_light(f)

    assert isinstance(rows, list)
    assert len(rows) == len(f.by_type("IfcProduct"))
    assert all(isinstance(row, tuple) and len(row) == 3 for row in rows)

    row = next(r for r in rows if r[0] == wall.GlobalId)
    assert row == (wall.GlobalId, "IfcWall", "Wall1")


def test_extract_elements_light_handles_missing_name():
    f = ifcopenshell.file(schema="IFC4")
    ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", f)
    elem = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcWall")
    elem.Name = None

    rows = extract_elements_light(f)

    row = next(r for r in rows if r[0] == elem.GlobalId)
    assert row == (elem.GlobalId, "IfcWall", None)


def test_extract_elements_light_does_not_use_geometry(monkeypatch):
    """create_shape/iterator/settings のいずれを呼んでも例外になるようにしても、
    extract_elements_light は正常に完了する(ジオメトリ機構に一切触れない証明)。
    """
    f = build_wall_with_window_ifc()

    def _boom(*args, **kwargs):
        raise AssertionError("extract_elements_light must not touch ifcopenshell.geom")

    monkeypatch.setattr(ifcopenshell.geom, "create_shape", _boom)
    monkeypatch.setattr(ifcopenshell.geom, "iterator", _boom)
    monkeypatch.setattr(ifcopenshell.geom, "settings", _boom)

    rows = extract_elements_light(f)

    assert len(rows) == len(f.by_type("IfcProduct"))


# --- _dominant_diffuse の単体テスト(フェーズ最終レビュー I-2) ---
#
# 色の一次経路(_dominant_diffuse)と代替経路(_graph_fallback_diffuse)は
# 互いを隠す。統合テストだけだと、一次経路を壊しても代替経路が拾ってしまい
# GREEN のままになる(レビュアが実測: _dominant_diffuse を常に None にしても
# 全テストが通り、実データでは全1,381要素が黙ってグラフ走査に落ちる)。
# 一次経路を単独で固定する。


class _FakeColour:
    """ifcopenshell 0.8.5 の colour を模した最小オブジェクト。r/g/b はメソッド。"""

    def __init__(self, rgb):
        self._rgb = rgb

    def r(self):
        return self._rgb[0]

    def g(self):
        return self._rgb[1]

    def b(self):
        return self._rgb[2]


class _FakeMaterial:
    def __init__(self, rgb):
        self.diffuse = _FakeColour(rgb) if rgb is not None else None


class _FakeGeometryWithMaterials:
    def __init__(self, materials, material_ids):
        self.materials = materials
        self.material_ids = material_ids


def test_dominant_diffuse_returns_the_only_material():
    geometry = _FakeGeometryWithMaterials([_FakeMaterial((0.25, 0.5, 0.75))], [0, 0, 0, 0])
    assert _dominant_diffuse(geometry) == (0.25, 0.5, 0.75)


def test_dominant_diffuse_picks_the_material_covering_the_most_faces():
    """面ごとに色が違う要素は多数決で1色に潰す(ビューアが要素単位で塗るため)。"""
    geometry = _FakeGeometryWithMaterials(
        [_FakeMaterial((1.0, 0.0, 0.0)), _FakeMaterial((0.0, 1.0, 0.0))],
        [0, 1, 1, 1, 0],  # index1 が3面、index0 が2面
    )
    assert _dominant_diffuse(geometry) == (0.0, 1.0, 0.0)


def test_dominant_diffuse_returns_none_without_materials():
    assert _dominant_diffuse(_FakeGeometryWithMaterials([], [])) is None


def test_dominant_diffuse_returns_none_when_no_face_supports_any_material():
    """面が1つも指していない material しか無ければ None。

    支持のない色を「多数決の勝者」として返すと、色情報が無いのに色があることに
    なり、「色情報なし: N要素」の件数が嘘になる。
    """
    geometry = _FakeGeometryWithMaterials(
        [_FakeMaterial((1.0, 0.0, 0.0))], [-1, -1, 99]
    )
    assert _dominant_diffuse(geometry) is None


def test_dominant_diffuse_uses_the_single_material_when_no_face_assignment_exists():
    """面ごとの割り当てが無く material が1つだけなら、それが形状全体の色。"""
    geometry = _FakeGeometryWithMaterials([_FakeMaterial((0.1, 0.2, 0.3))], [])
    assert _dominant_diffuse(geometry) == (0.1, 0.2, 0.3)
