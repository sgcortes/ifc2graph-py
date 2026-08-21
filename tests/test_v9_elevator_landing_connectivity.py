from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point, Polygon

from v9hsimg import HSIMGBuilder, HSIMGConfig


def component_builder(footprint):
    builder = object.__new__(HSIMGBuilder)
    builder.config = HSIMGConfig(
        horizontal_component_bridge_max_length_m=12.0,
        horizontal_component_bridge_max_per_space=8,
    )
    builder.graph = nx.MultiDiGraph()
    builder.issues = []
    builder.horizontal_component_bridge_candidates_rejected = 0
    builder.horizontal_component_bridges_added = 0
    builder.horizontal_component_bridge_spaces = 0
    builder.mobility_axes = []
    builder.subgraphs = [{
        "subgraph_id": "subgraph",
        "subgraph_type": "horizontal_mobility",
    }]
    space = SimpleNamespace(
        space_id="space",
        ifc_guid="space-guid",
        name="Hall",
        node_class="horizontal_mobility",
        footprint=footprint,
        interior_point=footprint.representative_point(),
    )
    builder.spaces = {"space": space}
    return builder, space


def add_node(builder, node_id, x, y):
    builder.graph.add_node(
        node_id,
        node_type="internal_mobility",
        node_role="axis_endpoint",
        mobility_type="horizontal",
        parent_node_id="space",
        subgraph_id="subgraph",
        hierarchy_level=2,
        x=x,
        y=y,
        z=0.0,
        geometry=Point(x, y, 0.0),
    )


def add_axis(builder, source, target):
    line = LineString([
        tuple(builder.graph.nodes[source]["geometry"].coords)[0],
        tuple(builder.graph.nodes[target]["geometry"].coords)[0],
    ])
    builder._add_bidirectional_edge(
        source,
        target,
        edge_type="internal_axis",
        mobility_mode="walk",
        subgraph_id="subgraph",
        geometry=line,
    )


class V9ElevatorLandingConnectivityTests(unittest.TestCase):
    def test_disconnected_axes_in_same_open_hall_are_joined(self):
        builder, space = component_builder(
            Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
        )
        for node_id, x, y in (
            ("a", 2, 3), ("b", 5, 3),
            ("c", 12, 3), ("d", 16, 3),
        ):
            add_node(builder, node_id, x, y)
        add_axis(builder, "a", "b")
        add_axis(builder, "c", "d")

        added = builder._recover_space_component_bridges(space)

        self.assertEqual(added, 1)
        local, _ = builder._horizontal_space_graph("space")
        self.assertTrue(nx.is_connected(local))
        bridges = [
            data for _, _, data in builder.graph.edges(data=True)
            if data.get("relation_source") == "same_space_clearance_component_bridge_v9"
        ]
        self.assertEqual(len(bridges), 2)
        self.assertTrue(all(edge["accessible_general"] for edge in bridges))

    def test_component_bridge_does_not_cross_column(self):
        builder, space = component_builder(Polygon(
            [(0, 0), (20, 0), (20, 10), (0, 10)],
            holes=[[(8, 0.5), (12, 0.5), (12, 9.5), (8, 9.5)]],
        ))
        for node_id, x, y in (
            ("a", 2, 5), ("b", 6, 5),
            ("c", 14, 5), ("d", 18, 5),
        ):
            add_node(builder, node_id, x, y)
        add_axis(builder, "a", "b")
        add_axis(builder, "c", "d")

        self.assertEqual(builder._recover_space_component_bridges(space), 0)
        self.assertGreater(
            builder.horizontal_component_bridge_candidates_rejected,
            0,
        )

    def test_ifc_wall_blocks_open_space_inference(self):
        builder = object.__new__(HSIMGBuilder)
        builder._space_ids_by_boundary_wall = {
            1: {"left", "right"},
        }

        self.assertTrue(builder._spaces_share_ifc_wall("left", "right"))
        self.assertFalse(builder._spaces_share_ifc_wall("left", "other"))


if __name__ == "__main__":
    unittest.main()
