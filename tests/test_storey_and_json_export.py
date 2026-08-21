import json

from hsimg import _json, containing_storey


class FakeEntity:
    def __init__(self, entity_id, ifc_class):
        self._id = entity_id
        self._ifc_class = ifc_class
        self.ContainedInStructure = []
        self.ReferencedInStructures = []
        self.Decomposes = []

    def id(self):
        return self._id

    def is_a(self, ifc_class=None):
        return self._ifc_class if ifc_class is None else self._ifc_class == ifc_class


class Decomposition:
    def __init__(self, parent):
        self.RelatingObject = parent


def test_containing_storey_accepts_direct_decomposition_parent():
    storey = FakeEntity(10, "IfcBuildingStorey")
    space = FakeEntity(20, "IfcSpace")
    space.Decomposes.append(Decomposition(storey))

    assert containing_storey(space) is storey


def test_json_serialization_replaces_non_finite_numbers_with_null():
    encoded = _json({"positive": float("inf"), "negative": -float("inf"), "nan": float("nan")})

    assert json.loads(encoded) == {"positive": None, "negative": None, "nan": None}
    assert "Infinity" not in encoded
    assert "NaN" not in encoded
