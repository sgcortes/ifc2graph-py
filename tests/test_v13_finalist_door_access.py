from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point, Polygon

from v13hsimg import HSIMGBuilder, HSIMGConfig


def make_builder(with_hole=False):
    builder = object.__new__(HSIMGBuilder)
    builder.config = HSIMGConfig()
    builder.graph = nx.MultiDiGraph()
    builder.issues = []
    builder._visibility_graph_cache = {}
    builder.finalist_door_bridges_added = 0
    builder.finalist_spaces_repaired = 0
    builder.finalist_door_bridge_rejections = 0
    footprint = Polygon(
        [(0, 0), (12, 0), (12, 10), (0, 10)],
        holes=[[(5, 2), (7, 2), (7, 8), (5, 8)]] if with_hole else None,
    )
    space = SimpleNamespace(
        space_id="garage",
        ifc_guid="garage-guid",
        ifc_class="IfcSpace",
        name="Garage",
        node_class="finalist",
        footprint=footprint,
        interior_point=Point(3, 5, 0),
        elevation=0.0,
        storey_id="level",
        connected_door_ids=["gate_a", "gate_b"],
    )
    doors = {
        "gate_a": SimpleNamespace(
            door_id="gate_a", ifc_guid="gate-a-guid",
            wheelchair_accessible=True,
        ),
        "gate_b": SimpleNamespace(
            door_id="gate_b", ifc_guid="gate-b-guid",
            wheelchair_accessible=True,
        ),
    }
    builder.spaces = {space.space_id: space}
    builder.doors = doors
    builder.graph.add_node(
        "garage", geometry=space.interior_point, z=0.0, node_type="space"
    )
    from hsimg import stable_id
    side_a = stable_id("door_side", "gate-a-guid", "garage-guid")
    side_b = stable_id("door_side", "gate-b-guid", "garage-guid")
    builder.graph.add_node(
        side_a, geometry=Point(2, 0.05, 0), z=0.0,
        node_type="door_access", parent_node_id="gate_a",
    )
    builder.graph.add_node(
        side_b, geometry=Point(10, 0.05, 0), z=0.0,
        node_type="door_access", parent_node_id="gate_b",
    )
    builder._add_bidirectional_edge(
        "garage", side_a, edge_type="space_access", mobility_mode="walk",
        subgraph_id=None,
        geometry=LineString([(3, 5, 0), (2, 0.05, 0)]),
    )
    return builder, space, side_a, side_b


class V13FinalistDoorAccessTests(unittest.TestCase):
    def test_detached_gate_joins_anchor_in_same_space(self):
        builder, space, side_a, side_b = make_builder()
        self.assertEqual(builder._repair_finalist_space_door_access(space), 1)
        self.assertTrue(nx.has_path(nx.Graph(builder.graph), side_b, "garage"))
        repairs = [
            data for _, _, data in builder.graph.edges(data=True)
            if data.get("relation_source") == "same_ifc_space_visibility_bridge_v13"
        ]
        self.assertEqual(len(repairs), 2)
        self.assertTrue(all(edge["accessible_wheelchair"] for edge in repairs))

    def test_bridge_routes_around_internal_hole(self):
        builder, space, side_a, side_b = make_builder(with_hole=True)
        self.assertEqual(builder._repair_finalist_space_door_access(space), 1)
        repair = next(
            data for _, _, data in builder.graph.edges(data=True)
            if data.get("relation_source") == "same_ifc_space_visibility_bridge_v13"
        )
        hole = Polygon([(5, 2), (7, 2), (7, 8), (5, 8)])
        self.assertFalse(repair["geometry"].intersects(hole))

    def test_no_explicit_anchor_means_no_invented_connection(self):
        builder, space, _, _ = make_builder()
        edges = list(builder.graph.edges(keys=True))
        for source, target, key in edges:
            if source == "garage" or target == "garage":
                builder.graph.remove_edge(source, target, key)
        self.assertEqual(builder._repair_finalist_space_door_access(space), 0)


if __name__ == "__main__":
    unittest.main()
