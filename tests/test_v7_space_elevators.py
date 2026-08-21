from types import SimpleNamespace
import unittest

from shapely.geometry import Point, Polygon
import networkx as nx

from v7hsimg import (
    HSIMGBuilder,
    HSIMGConfig,
    cluster_elevator_spaces,
    is_semantic_elevator_opening,
    is_elevator_space,
    opening_profile_dimensions,
)


def space(identifier, storey, x, y, long_name="Elevador"):
    footprint = Polygon([
        (x - 0.75, y - 0.75),
        (x + 0.75, y - 0.75),
        (x + 0.75, y + 0.75),
        (x - 0.75, y + 0.75),
    ])
    return SimpleNamespace(
        space_id=f"space_{identifier}",
        ifc_guid=f"guid_{identifier}",
        ifc_entity_id=identifier,
        ifc_class="IfcSpace",
        name=str(identifier),
        long_name=long_name,
        object_type=None,
        storey_id=storey,
        footprint=footprint,
        interior_point=Point(x, y, 0),
        properties={},
    )


class FakeIfcEntity:
    def __init__(self, identifier, ifc_class, **attributes):
        self._identifier = identifier
        self._ifc_class = ifc_class
        for key, value in attributes.items():
            setattr(self, key, value)

    def id(self):
        return self._identifier

    def is_a(self, ifc_class=None):
        if ifc_class is None:
            return self._ifc_class
        return self._ifc_class == ifc_class

    def get_info(self, **_kwargs):
        return self.__dict__


class V7SpaceElevatorTests(unittest.TestCase):
    def test_semantic_unfilled_elevator_opening_profile_is_recognised(self):
        terms = HSIMGConfig().elevator_opening_profile_terms
        self.assertTrue(is_semantic_elevator_opening(
            ["0900 x 2032mm MIO Puesta Ascensor"], terms
        ))
        self.assertTrue(is_semantic_elevator_opening(
            ["Elevator Door 900"], terms
        ))
        self.assertFalse(is_semantic_elevator_opening(
            ["Hueco ventana 1200 x 1000"], terms
        ))

    def test_boundary_opening_tolerances_are_conservative(self):
        config = HSIMGConfig()
        self.assertEqual(config.elevator_boundary_opening_tolerance_m, 0.25)
        self.assertEqual(config.elevator_hall_boundary_tolerance_m, 0.25)
        self.assertLess(
            config.elevator_boundary_opening_tolerance_m,
            0.90,
        )

    def test_opening_width_comes_from_profile_not_extrusion_depth(self):
        points = FakeIfcEntity(
            2,
            "IfcCartesianPointList2D",
            CoordList=(
                (-0.45, -1.10),
                (0.45, -1.10),
                (0.45, 1.10),
                (-0.45, 1.10),
            ),
        )
        opening = FakeIfcEntity(1, "IfcOpeningElement", Representation=points)

        width, height = opening_profile_dimensions(opening)

        self.assertAlmostEqual(width, 0.90)
        self.assertAlmostEqual(height, 2.20)

    def test_explicit_multilingual_elevator_semantics(self):
        terms = HSIMGConfig().elevator_space_terms
        self.assertTrue(is_elevator_space(space(1, "l0", 0, 0), terms))
        self.assertTrue(
            is_elevator_space(
                SimpleNamespace(
                    name="A-01", long_name="ASCENSOR", object_type=None,
                    properties={},
                ),
                terms,
            )
        )
        self.assertFalse(
            is_elevator_space(
                SimpleNamespace(
                    name="Office", long_name="Elevated meeting room",
                    object_type=None, properties={},
                ),
                terms,
            )
        )

    def test_vertical_alignment_keeps_adjacent_shafts_separate(self):
        spaces = [
            space(1, "l0", 0.0, 0.0),
            space(2, "l1", 0.1, 0.0),
            space(3, "l0", 5.0, 0.0),
            space(4, "l1", 5.1, 0.0),
        ]
        clusters = cluster_elevator_spaces(spaces, 0.85, 0.15)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sorted(len(cluster) for cluster in clusters), [2, 2])

    def test_duplicate_storey_spaces_are_consolidated_into_one_stop(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig()
        builder.storeys = {
            "l0": SimpleNamespace(name="Level 0", elevation=0.0),
            "l1": SimpleNamespace(name="Level 1", elevation=4.7),
            "l2": SimpleNamespace(name="Level 2", elevation=9.4),
        }
        builder.issues = []
        builder.elevator_stop_space_ids = {}
        cluster = [
            space(1, "l0", 0.0, 0.0),
            space(2, "l0", 0.05, 0.0),
            space(3, "l1", 0.0, 0.0),
            space(4, "l2", 0.0, 0.0),
        ]

        record = builder._space_cluster_record(cluster, 0)

        self.assertIsNotNone(record)
        self.assertEqual(record.connected_storeys, ["l0", "l1", "l2"])
        self.assertEqual(len(record.path.geoms), 2)
        self.assertEqual(
            len(builder.elevator_stop_space_ids[(record.vertical_id, "l0")]),
            2,
        )
        self.assertEqual(
            [issue.issue_type for issue in builder.issues],
            ["duplicate_elevator_spaces_on_storey_v7"],
        )

    def test_prevertical_component_sizes_ignore_cross_storey_edges(self):
        builder = object.__new__(HSIMGBuilder)
        builder.graph = nx.MultiDiGraph()
        for node_id, storey_id in (("a", "l0"), ("b", "l0"), ("c", "l1")):
            builder.graph.add_node(node_id, storey_id=storey_id)
        builder.graph.add_edge("a", "b", accessible_general=True)
        builder.graph.add_edge("b", "c", accessible_general=True)

        builder._cache_prevertical_component_sizes()

        self.assertEqual(builder._prevertical_component_size_by_node["a"], 2)
        self.assertEqual(builder._prevertical_component_size_by_node["b"], 2)
        self.assertEqual(builder._prevertical_component_size_by_node["c"], 1)


if __name__ == "__main__":
    unittest.main()
