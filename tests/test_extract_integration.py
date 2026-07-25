import numpy as np
import pytest
import ifcopenshell.api
import ifcopenshell.geom

from ifc_occam.core.extract import extract_elements_light, extract_model
from tests.fixtures_ifc import build_wall_with_window_ifc


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
