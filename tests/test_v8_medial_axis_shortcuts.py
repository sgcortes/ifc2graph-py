from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point, Polygon

from v8hsimg import HSIMGBuilder, HSIMGConfig


def builder_for(footprint, coordinates, edges, **config):
    builder = object.__new__(HSIMGBuilder)
    builder.config = HSIMGConfig(**config)
    builder.graph = nx.MultiDiGraph()
    builder.issues = []
    builder.mobility_axes = []
    builder.subgraphs = [{
        "subgraph_id": "subgraph",
        "subgraph_type": "horizontal_mobility",
    }]
    builder.horizontal_shortcuts_added = 0
    builder.horizontal_shortcut_spaces = 0
    builder.horizontal_shortcut_candidates_rejected_clearance = 0
    builder.horizontal_shortcut_total_saving_m = 0.0
    space = SimpleNamespace(
        space_id="space",
        ifc_guid="space-guid",
        name="Test hall",
        node_class="horizontal_mobility",
        footprint=footprint,
        interior_point=footprint.representative_point(),
    )
    builder.spaces = {"space": space}
    for node_id, (x, y) in coordinates.items():
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
    for source, target in edges:
        line = LineString([
            coordinates[source] + (0.0,),
            coordinates[target] + (0.0,),
        ])
        builder._add_bidirectional_edge(
            source,
            target,
            edge_type="internal_axis",
            mobility_mode="walk",
            subgraph_id="subgraph",
            geometry=line,
        )
    return builder, space


class V8MedialAxisShortcutTests(unittest.TestCase):
    def test_clear_wide_shortcut_replaces_large_medial_axis_detour(self):
        builder, space = builder_for(
            Polygon([(0, 0), (20, 0), (20, 20), (0, 20)]),
            {
                "a": (5, 5),
                "b": (15, 5),
                "c": (15, 15),
                "d": (5, 15),
            },
            [("a", "b"), ("b", "c"), ("c", "d")],
            horizontal_shortcut_max_length_m=12.0,
            horizontal_shortcut_min_stretch_ratio=1.75,
            horizontal_shortcut_min_saving_m=3.0,
        )

        added = builder._recover_space_shortcuts(space)

        self.assertGreaterEqual(added, 1)
        shortcuts = [
            data for _, _, data in builder.graph.edges(data=True)
            if data.get("relation_source") == "clearance_validated_visibility_shortcut_v8"
        ]
        self.assertEqual(len(shortcuts), 2)
        self.assertTrue(all(edge["accessible_general"] for edge in shortcuts))
        self.assertTrue(all(edge["accessible_wheelchair"] for edge in shortcuts))

    def test_shortcut_crossing_obstacle_hole_is_rejected(self):
        footprint = Polygon(
            [(0, 0), (20, 0), (20, 20), (0, 20)],
            holes=[[(8, 6), (12, 6), (12, 14), (8, 14)]],
        )
        builder, space = builder_for(
            footprint,
            {
                "a": (5, 10),
                "b": (5, 17),
                "c": (15, 17),
                "d": (15, 10),
            },
            [("a", "b"), ("b", "c"), ("c", "d")],
            horizontal_shortcut_max_length_m=12.0,
            horizontal_shortcut_min_stretch_ratio=1.75,
            horizontal_shortcut_min_saving_m=3.0,
        )

        added = builder._recover_space_shortcuts(space)

        self.assertEqual(added, 0)
        self.assertGreater(builder.horizontal_shortcut_candidates_rejected_clearance, 0)

    def test_reasonable_axis_route_is_not_densified(self):
        builder, space = builder_for(
            Polygon([(0, 0), (12, 0), (12, 4), (0, 4)]),
            {"a": (1, 2), "b": (6, 2), "c": (11, 2)},
            [("a", "b"), ("b", "c")],
            horizontal_shortcut_max_length_m=12.0,
            horizontal_shortcut_min_stretch_ratio=1.75,
            horizontal_shortcut_min_saving_m=3.0,
        )

        self.assertEqual(builder._recover_space_shortcuts(space), 0)

    def test_shortcuts_are_bidirectional_and_stay_in_parent_space(self):
        builder, space = builder_for(
            Polygon([(0, 0), (20, 0), (20, 20), (0, 20)]),
            {"a": (5, 5), "b": (15, 5), "c": (15, 15), "d": (5, 15)},
            [("a", "b"), ("b", "c"), ("c", "d")],
            horizontal_shortcut_max_length_m=12.0,
        )

        builder._recover_space_shortcuts(space)

        shortcuts = [
            (source, target, data)
            for source, target, data in builder.graph.edges(data=True)
            if data.get("relation_source") == "clearance_validated_visibility_shortcut_v8"
        ]
        self.assertTrue(shortcuts)
        for source, target, data in shortcuts:
            reverse = builder.graph.get_edge_data(target, source)
            self.assertTrue(any(
                edge.get("relation_source") == data.get("relation_source")
                for edge in reverse.values()
            ))
            self.assertEqual(
                builder.graph.nodes[source]["parent_node_id"],
                builder.graph.nodes[target]["parent_node_id"],
            )


if __name__ == "__main__":
    unittest.main()
