import ifcopenshell

from tests.fixtures_ifc import build_wall_with_window_ifc

from ifc_occam.core.cascade import compute_delete_closure


def _gid(entity) -> str:
    return entity.GlobalId


def test_delete_wall_cascades_to_opening_and_window():
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    opening = f.by_type("IfcOpeningElement")[0]
    window = f.by_type("IfcWindow")[0]

    closure = compute_delete_closure(f, [_gid(wall)])

    assert closure.direct == [_gid(wall)]
    cascaded_gids = {item.global_id for item in closure.cascaded}
    assert cascaded_gids == {_gid(opening), _gid(window)}

    reasons = {item.global_id: item.reason for item in closure.cascaded}
    assert reasons[_gid(opening)] == "開口(親要素の削除)"
    assert reasons[_gid(window)] == "開口の充填要素"

    assert closure.all_gids == {_gid(wall), _gid(opening), _gid(window)}


def test_delete_assembly_cascades_recursively_to_members():
    f = build_wall_with_window_ifc()
    assembly = f.by_type("IfcElementAssembly")[0]
    members = f.by_type("IfcBeam")

    closure = compute_delete_closure(f, [_gid(assembly)])

    assert closure.direct == [_gid(assembly)]
    cascaded_gids = {item.global_id for item in closure.cascaded}
    assert cascaded_gids == {_gid(m) for m in members}
    for item in closure.cascaded:
        assert item.reason == "集約の子部材"

    assert closure.all_gids == {_gid(assembly)} | {_gid(m) for m in members}


def test_delete_element_with_no_relations_has_only_direct():
    f = build_wall_with_window_ifc()
    window = f.by_type("IfcWindow")[0]

    closure = compute_delete_closure(f, [_gid(window)])

    assert closure.direct == [_gid(window)]
    assert closure.cascaded == []
    assert closure.all_gids == {_gid(window)}


def test_duplicate_targets_do_not_duplicate_in_all_gids():
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    opening = f.by_type("IfcOpeningElement")[0]

    # opening is itself passed directly, AND it is reachable via wall's cascade.
    closure = compute_delete_closure(f, [_gid(wall), _gid(opening)])

    assert closure.direct == [_gid(wall), _gid(opening)]
    # opening must not appear again in cascaded since it's already a direct target.
    cascaded_gids = [item.global_id for item in closure.cascaded]
    assert cascaded_gids.count(_gid(opening)) == 0

    window = f.by_type("IfcWindow")[0]
    assert closure.all_gids == {_gid(wall), _gid(opening), _gid(window)}


def test_compute_delete_closure_does_not_mutate_ifc_file():
    f = build_wall_with_window_ifc()
    wall = f.by_type("IfcWall")[0]
    before_counts = {
        cls: len(f.by_type(cls))
        for cls in (
            "IfcWall",
            "IfcOpeningElement",
            "IfcWindow",
            "IfcElementAssembly",
            "IfcBeam",
            "IfcRelVoidsElement",
            "IfcRelFillsElement",
            "IfcRelAggregates",
        )
    }

    compute_delete_closure(f, [_gid(wall)])

    after_counts = {
        cls: len(f.by_type(cls))
        for cls in (
            "IfcWall",
            "IfcOpeningElement",
            "IfcWindow",
            "IfcElementAssembly",
            "IfcBeam",
            "IfcRelVoidsElement",
            "IfcRelFillsElement",
            "IfcRelAggregates",
        )
    }
    assert before_counts == after_counts


def test_compute_delete_closure_on_small_ifc_does_not_raise(small_ifc_path):
    f = ifcopenshell.open(str(small_ifc_path))

    element = f.by_type("IfcElement")[0]
    closure = compute_delete_closure(f, [element.GlobalId])
    assert closure.direct == [element.GlobalId]
    assert element.GlobalId in closure.all_gids

    building = f.by_type("IfcBuilding")[0]
    building_closure = compute_delete_closure(f, [building.GlobalId])
    assert building_closure.direct == [building.GlobalId]
    assert len(building_closure.all_gids) >= 1
