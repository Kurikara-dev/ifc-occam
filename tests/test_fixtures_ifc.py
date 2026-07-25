from tests.fixtures_ifc import build_wall_with_window_ifc


def test_build_wall_with_window_ifc_contains_expected_relationships():
    f = build_wall_with_window_ifc()

    walls = f.by_type("IfcWall")
    openings = f.by_type("IfcOpeningElement")
    windows = f.by_type("IfcWindow")
    assemblies = f.by_type("IfcElementAssembly")
    beams = f.by_type("IfcBeam")

    assert len(walls) == 1
    assert len(openings) == 1
    assert len(windows) == 1
    assert len(assemblies) == 1
    assert len(beams) == 2

    wall = walls[0]
    opening = openings[0]
    window = windows[0]
    assembly = assemblies[0]

    voids_rels = wall.HasOpenings
    assert len(voids_rels) == 1
    assert voids_rels[0].is_a("IfcRelVoidsElement")
    assert voids_rels[0].RelatedOpeningElement == opening

    fills_rels = window.FillsVoids
    assert len(fills_rels) == 1
    assert fills_rels[0].is_a("IfcRelFillsElement")
    assert fills_rels[0].RelatingOpeningElement == opening

    aggregate_rels = assembly.IsDecomposedBy
    assert len(aggregate_rels) == 1
    assert aggregate_rels[0].is_a("IfcRelAggregates")
    assert set(aggregate_rels[0].RelatedObjects) == set(beams)
