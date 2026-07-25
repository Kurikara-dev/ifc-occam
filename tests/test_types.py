import numpy as np
from ifc_occam.core.types import ShapeInfo, ElementInfo, ModelData

def _shape(shape_id="s1", n_tri=2):
    vertices = np.zeros((3, 3), dtype=np.float64)
    faces = np.zeros((n_tri, 3), dtype=np.int64)
    return ShapeInfo(shape_id=shape_id, vertices=vertices, faces=faces)

def test_triangle_count_is_number_of_faces():
    assert _shape(n_tri=5).triangle_count == 5

def test_model_data_holds_elements_and_shapes():
    s = _shape()
    e = ElementInfo(
        global_id="GUID1", ifc_class="IfcWall", name=None,
        shape_id="s1", is_mapped=False,
        representation_types=("SweptSolid",), layer=None,
    )
    m = ModelData(schema="IFC4", elements=[e], shapes={"s1": s})
    assert m.shapes[m.elements[0].shape_id].triangle_count == 2
