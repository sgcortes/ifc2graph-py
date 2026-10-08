import math
from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point

from horizontal_cleanup import simplify_horizontal_graph
from v14hsimg import HSIMGBuilder, HSIMGConfig


def node(graph, name, x, y, terminal=False):
    graph.add_node(name, x=x, y=y, z=0.0, geometry=Point(x, y, 0),
                   node_type="internal_mobility", mobility_type="horizontal",
                   node_role="door_projection" if terminal else "axis_endpoint",
                   parent_node_id="space", subgraph_id="axes", hierarchy_level=2)


def edge(graph, u, v, wheelchair=True, kind="internal_axis", bidirectional=True, suffix="", cost=None):
    line = LineString([graph.nodes[u]["geometry"], graph.nodes[v]["geometry"]])
    directions = [(u, v, line)]
    if bidirectional:
        directions.append((v, u, LineString(list(line.coords)[::-1])))
    for source, target, geometry in directions:
        key = f"{source}-{target}{suffix}"
        graph.add_edge(source, target, key=key, edge_id=key, source=source, target=target,
                       edge_type=kind, subgraph_id="axes", mobility_mode="walk",
                       geometry=geometry, geometry_wkt=geometry.wkt, length_3d=line.length,
                       horizontal_length=line.length, accessible_general=True,
                       accessible_wheelchair=wheelchair, estimated_time=line.length,
                       effort_cost=line.length, cost_general=cost if cost is not None else line.length,
                       cost_wheelchair=line.length if wheelchair else math.inf,
                       metadata_json="{}")


def corridor():
    graph = nx.MultiDiGraph()
    for name, x, y, terminal in (("A", 0, 0, True), ("B", 10, 0, True),
                                 ("main", 5, 0, False), ("detour", 5, 3, False),
                                 ("spur", 5, -2, False)):
        node(graph, name, x, y, terminal)
    for u, v in (("A", "main"), ("main", "B"), ("A", "detour"),
                 ("detour", "B"), ("main", "spur")):
        edge(graph, u, v)
    return graph


class FunctionalCleanupTests(unittest.TestCase):
    def test_column_detour_and_nonfunctional_spur_disappear(self):
        graph = corridor()
        result = simplify_horizontal_graph(graph)
        self.assertEqual(set(graph), {"A", "B"})
        self.assertEqual(graph.number_of_edges(), 2)
        self.assertEqual(nx.shortest_path_length(graph, "A", "B", weight="length_3d"), 10)
        self.assertEqual(result.summary["unused_nodes_removed"], 2)
        self.assertEqual(result.summary["degree_two_nodes_contracted"], 1)

    def test_door_on_detour_makes_it_functional(self):
        graph = corridor()
        node(graph, "door", 5, 4)
        graph.nodes["door"]["node_type"] = "door_access"
        edge(graph, "door", "detour", kind="door_axis_projection")
        simplify_horizontal_graph(graph)
        self.assertTrue(nx.has_path(graph, "door", "A"))
        self.assertTrue(nx.has_path(graph, "door", "B"))
        self.assertIn("detour", graph)
        self.assertNotIn("spur", graph)

    def test_longer_wheelchair_route_is_retained(self):
        graph = corridor()
        for u, v, d in graph.edges(data=True):
            if "main" in {u, v}:
                d["accessible_wheelchair"] = False
                d["cost_wheelchair"] = math.inf
        result = simplify_horizontal_graph(graph)
        self.assertIn("detour", graph)  # parallel paths prevent contraction
        accessible = nx.DiGraph((u, v) for u, v, d in graph.edges(data=True)
                                if d["accessible_wheelchair"])
        self.assertTrue(nx.has_path(accessible, "A", "B"))
        self.assertEqual(result.summary["maximum_cost_change"], 0)

    def test_bends_are_preserved_as_polyline_vertices(self):
        graph = nx.MultiDiGraph()
        for name, x, y, terminal in (("A", 0, 0, True), ("bend", 0, 5, False), ("B", 5, 5, True)):
            node(graph, name, x, y, terminal)
        edge(graph, "A", "bend")
        edge(graph, "bend", "B")
        simplify_horizontal_graph(graph)
        line = next(iter(graph["A"]["B"].values()))["geometry"]
        self.assertEqual(list(line.coords), [(0, 0, 0), (0, 5, 0), (5, 5, 0)])
        self.assertEqual(line.length, 10)

    def test_single_access_component_is_retained_for_diagnosis(self):
        graph = nx.MultiDiGraph()
        node(graph, "A", 0, 0, True)
        node(graph, "end", 9, 0)
        edge(graph, "A", "end")
        result = simplify_horizontal_graph(graph)
        self.assertEqual(set(graph), {"A", "end"})
        self.assertEqual(result.spaces[0]["retained_sparse_components"], 1)

    def test_one_way_path_stays_one_way(self):
        graph = nx.MultiDiGraph()
        node(graph, "A", 0, 0, True)
        node(graph, "B", 5, 0, True)
        edge(graph, "A", "B", bidirectional=False)
        simplify_horizontal_graph(graph)
        self.assertTrue(nx.has_path(graph, "A", "B"))
        self.assertFalse(nx.has_path(graph, "B", "A"))

    def test_duplicate_arcs_are_removed_but_reverse_is_preserved(self):
        graph = nx.MultiDiGraph()
        node(graph, "A", 0, 0, True)
        node(graph, "B", 5, 0, True)
        edge(graph, "A", "B")
        edge(graph, "A", "B", suffix="duplicate")
        simplify_horizontal_graph(graph)
        self.assertEqual(graph.number_of_edges(), 2)

    def test_minimum_profile_cost_is_preserved_even_when_longer(self):
        graph = corridor()
        for u, v, d in graph.edges(data=True):
            if "detour" in {u, v}:
                d["cost_general"] = 0.1
        simplify_horizontal_graph(graph)
        self.assertAlmostEqual(nx.shortest_path_length(graph, "A", "B", weight="cost_general"), 0.2)
        self.assertEqual(nx.shortest_path_length(graph, "A", "B", weight="length_3d"), 10)

    def test_vertical_attachment_is_protected(self):
        graph = corridor()
        graph.nodes["spur"]["node_role"] = "landing"
        simplify_horizontal_graph(graph)
        self.assertIn("spur", graph)
        self.assertTrue(nx.has_path(graph, "A", "spur"))

    def test_large_corridor_ring_with_four_accesses_is_not_a_spanning_tree(self):
        graph = nx.MultiDiGraph()
        for name, x, y in (("A", 0, 0), ("B", 20, 0), ("C", 20, 20), ("D", 0, 20)):
            node(graph, name, x, y, True)
        for u, v in (("A", "B"), ("B", "C"), ("C", "D"), ("D", "A")):
            edge(graph, u, v)
        simplify_horizontal_graph(graph)
        self.assertEqual(graph.number_of_edges(), 8)


class AxisSubdivisionTests(unittest.TestCase):
    def test_door_junction_replaces_original_axis_in_both_directions(self):
        from test_v12_axis_segment_doors import make_builder, long_corridor_graph
        builder, space = make_builder()
        builder.__class__ = HSIMGBuilder
        builder.config = HSIMGConfig()
        builder.axis_segments_replaced = 0
        builder.horizontal_cleanup = None
        long_corridor_graph(builder)
        self.assertEqual(builder._repair_space_door_approaches(space), 1)
        self.assertFalse(builder.graph.has_edge("axis_mid_south", "axis_mid_north"))
        self.assertFalse(builder.graph.has_edge("axis_mid_north", "axis_mid_south"))
        self.assertEqual(builder.axis_segments_replaced, 2)
        self.assertAlmostEqual(nx.shortest_path_length(builder.graph, "axis_mid_south",
                                                       "axis_mid_north", weight="length_3d"), 12)

    def test_two_door_projections_are_a_valid_contracted_axis(self):
        builder = object.__new__(HSIMGBuilder)
        builder.horizontal_cleanup = object()
        builder.graph = nx.MultiDiGraph()
        node(builder.graph, "A", 0, 0, True)
        node(builder.graph, "B", 10, 0, True)
        edge(builder.graph, "A", "B")
        self.assertTrue(builder._projection_reaches_axis(nx.Graph(builder.graph), "A"))
        for _, _, d in builder.graph.edges(data=True):
            d["edge_type"] = "door_throat_transition"
        self.assertFalse(builder._projection_reaches_axis(nx.Graph(builder.graph), "A"))

    def test_region_coverage_uses_polyline_interior_not_only_nodes(self):
        from shapely.geometry import Polygon
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig()
        builder.horizontal_cleanup = object()
        builder.graph = nx.MultiDiGraph()
        builder.issues = []
        node(builder.graph, "A", .1, 2, True)
        node(builder.graph, "B", 9.9, 2, True)
        edge(builder.graph, "A", "B")
        builder.spaces = {"space": SimpleNamespace(space_id="space", ifc_guid="guid", name="corridor",
                          node_class="horizontal_mobility", footprint=Polygon([(0,0),(10,0),(10,4),(0,4)]))}
        builder._validate_walkable_region_coverage()
        self.assertEqual(builder.walkable_regions_without_graph, 0)
        self.assertEqual(builder.walkable_regions_validated, 1)


if __name__ == "__main__":
    unittest.main()
