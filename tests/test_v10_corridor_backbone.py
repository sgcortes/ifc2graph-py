from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point, Polygon

from v10hsimg import HSIMGBuilder, HSIMGConfig


def builder_for(footprint, **config_overrides):
    builder = object.__new__(HSIMGBuilder)
    builder.config = HSIMGConfig(**config_overrides)
    builder.graph = nx.MultiDiGraph()
    builder.issues = []
    builder.mobility_axes = []
    builder.subgraphs = [{
        "subgraph_id": "subgraph",
        "subgraph_type": "horizontal_mobility",
    }]
    builder.pruned_internal_nodes = 0
    builder.pruned_accessless_components = 0
    builder.pruned_unprotected_dead_end_nodes = 0
    builder.preserved_long_accessless_components = 0
    builder.bounded_dead_end_nodes_removed = 0
    builder.clearance_backbone_repairs_added = 0
    builder.clearance_backbone_spaces_repaired = 0
    builder.clearance_backbone_candidates_rejected = 0
    builder.walkable_regions_validated = 0
    builder.walkable_regions_without_graph = 0
    builder.fragmented_walkable_regions = 0
    space = SimpleNamespace(
        space_id="space",
        ifc_guid="space-guid",
        name="Ring corridor",
        node_class="horizontal_mobility",
        footprint=footprint,
        interior_point=footprint.representative_point(),
    )
    builder.spaces = {"space": space}
    return builder, space


def add_node(builder, node_id, x, y, role="axis_endpoint"):
    builder.graph.add_node(
        node_id,
        node_type="internal_mobility",
        node_role=role,
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
        builder.graph.nodes[source]["geometry"].coords[0],
        builder.graph.nodes[target]["geometry"].coords[0],
    ])
    builder._add_bidirectional_edge(
        source,
        target,
        edge_type="internal_axis",
        mobility_mode="walk",
        subgraph_id="subgraph",
        geometry=line,
    )


class V10CorridorBackboneTests(unittest.TestCase):
    def test_long_dead_end_is_preserved_but_short_spur_is_removed(self):
        builder, _ = builder_for(
            Polygon([(0, 0), (20, 0), (20, 10), (0, 10)]),
            dead_end_max_prune_length_m=1.5,
        )
        for node_id, x, y, role in (
            ("door", 2, 5, "door_projection"),
            ("junction", 8, 5, "axis_endpoint"),
            ("long_leaf", 15, 5, "axis_endpoint"),
            ("short_leaf", 8, 5.5, "axis_endpoint"),
        ):
            add_node(builder, node_id, x, y, role)
        add_axis(builder, "door", "junction")
        add_axis(builder, "junction", "long_leaf")
        add_axis(builder, "junction", "short_leaf")

        builder._prune_dead_end_branches()

        self.assertIn("long_leaf", builder.graph)
        self.assertNotIn("short_leaf", builder.graph)
        self.assertEqual(builder.bounded_dead_end_nodes_removed, 1)

    def test_long_accessless_component_is_preserved(self):
        builder, _ = builder_for(
            Polygon([(0, 0), (20, 0), (20, 10), (0, 10)]),
            accessless_component_max_prune_length_m=2.0,
        )
        add_node(builder, "a", 2, 5)
        add_node(builder, "b", 8, 5)
        add_axis(builder, "a", "b")

        builder._prune_accessless_components()

        self.assertIn("a", builder.graph)
        self.assertIn("b", builder.graph)
        self.assertEqual(builder.preserved_long_accessless_components, 1)

    def test_visibility_repair_connects_around_large_hole(self):
        footprint = Polygon(
            [(0, 0), (24, 0), (24, 20), (0, 20)],
            holes=[[(7, 4), (17, 4), (17, 16), (7, 16)]],
        )
        builder, space = builder_for(footprint)
        for node_id, x, y in (
            ("left_a", 3, 8),
            ("left_b", 5, 8),
            ("right_a", 19, 8),
            ("right_b", 21, 8),
        ):
            add_node(builder, node_id, x, y)
        add_axis(builder, "left_a", "left_b")
        add_axis(builder, "right_a", "right_b")

        added = builder._recover_space_clearance_backbone(space)

        self.assertEqual(added, 1)
        local, _ = builder._horizontal_space_graph("space")
        self.assertTrue(nx.is_connected(local))
        repairs = [
            data
            for _, _, data in builder.graph.edges(data=True)
            if data.get("relation_source")
            == "clearance_domain_visibility_backbone_v10"
        ]
        self.assertEqual(len(repairs), 2)
        hole = Polygon([(7, 4), (17, 4), (17, 16), (7, 16)])
        self.assertTrue(all(not edge["geometry"].crosses(hole) for edge in repairs))
        self.assertTrue(all(edge["accessible_general"] for edge in repairs))

    def test_two_isolated_door_projections_seed_empty_walkable_region(self):
        builder, space = builder_for(
            Polygon([(0, 0), (12, 0), (12, 5), (0, 5)])
        )
        add_node(builder, "door_a", 2, 2.5, "door_projection")
        add_node(builder, "door_b", 10, 2.5, "door_projection")

        self.assertEqual(builder._recover_space_clearance_backbone(space), 1)
        local, _ = builder._horizontal_space_graph("space")
        self.assertTrue(nx.is_connected(local))

    def test_repair_refuses_components_separated_by_clearance_barrier(self):
        footprint = Polygon(
            [(0, 0), (20, 0), (20, 10), (0, 10)],
            holes=[[(8, 0.1), (12, 0.1), (12, 9.9), (8, 9.9)]],
        )
        builder, space = builder_for(footprint)
        for node_id, x, y in (
            ("left_a", 2, 5),
            ("left_b", 6, 5),
            ("right_a", 14, 5),
            ("right_b", 18, 5),
        ):
            add_node(builder, node_id, x, y)
        add_axis(builder, "left_a", "left_b")
        add_axis(builder, "right_a", "right_b")

        self.assertEqual(builder._recover_space_clearance_backbone(space), 0)
        self.assertFalse(nx.is_connected(builder._horizontal_space_graph("space")[0]))


if __name__ == "__main__":
    unittest.main()
