from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point, Polygon

from v12hsimg import HSIMGBuilder, HSIMGConfig


def make_builder(door_width=0.80):
    builder = object.__new__(HSIMGBuilder)
    builder.config = HSIMGConfig()
    builder.graph = nx.MultiDiGraph()
    builder.issues = []
    builder.mobility_axes = []
    builder.subgraphs = [{
        "subgraph_id": "subgraph",
        "subgraph_type": "horizontal_mobility",
    }]
    builder._visibility_graph_cache = {}
    builder.door_approach_rejections = 0
    builder.door_axis_segment_rejections = 0
    builder.door_axis_segment_repairs_added = 0
    builder.door_axis_segment_spaces_repaired = 0
    builder.door_projections_without_axis_reach = 0
    builder.exterior_entrances_without_interior_reach = 0
    space = SimpleNamespace(
        space_id="corridor",
        ifc_guid="corridor-guid",
        ifc_class="IfcSpace",
        name="Long corridor",
        node_class="horizontal_mobility",
        footprint=Polygon([(0, 0), (4, 0), (4, 40), (0, 40)]),
        interior_point=Point(2, 20),
        elevation=0.0,
        storey_id="storey",
    )
    door = SimpleNamespace(
        door_id="door",
        ifc_guid="door-guid",
        name="Corridor door",
        width=door_width,
        wheelchair_accessible=door_width >= 0.80,
        point=Point(0, 20, 0),
    )
    builder.spaces = {space.space_id: space}
    builder.doors = {door.door_id: door}
    return builder, space


def node(builder, node_id, x, y, role, parent="corridor"):
    builder.graph.add_node(
        node_id,
        node_type="door_access" if role == "door_side" else "internal_mobility",
        node_role=role,
        mobility_type="horizontal",
        parent_node_id=parent,
        subgraph_id="subgraph",
        hierarchy_level=2,
        x=x,
        y=y,
        z=0.0,
        geometry=Point(x, y, 0.0),
    )


def edge(builder, source, target, edge_type):
    geometry = LineString([
        builder.graph.nodes[source]["geometry"].coords[0],
        builder.graph.nodes[target]["geometry"].coords[0],
    ])
    builder._add_bidirectional_edge(
        source,
        target,
        edge_type=edge_type,
        mobility_mode="walk",
        subgraph_id="subgraph",
        geometry=geometry,
    )


def long_corridor_graph(builder):
    node(builder, "axis_south", 2, 2, "axis_endpoint")
    node(builder, "axis_north", 2, 38, "axis_endpoint")
    node(builder, "axis_mid_south", 2, 14, "door_projection")
    node(builder, "axis_mid_north", 2, 26, "door_projection")
    node(builder, "projection", 0.2, 20, "door_projection")
    node(builder, "door_side", 0.1, 20, "door_side", "door")
    edge(builder, "axis_south", "axis_mid_south", "internal_axis")
    edge(builder, "axis_mid_south", "axis_mid_north", "internal_axis")
    edge(builder, "axis_mid_north", "axis_north", "internal_axis")
    edge(builder, "door_side", "projection", "door_axis_projection")


class V12AxisSegmentDoorTests(unittest.TestCase):
    def test_long_corridor_door_connects_to_nearest_axis_segment(self):
        builder, space = make_builder(door_width=0.80)
        long_corridor_graph(builder)

        self.assertEqual(builder._repair_space_door_approaches(space), 1)
        local, _ = builder._horizontal_space_graph(space.space_id)
        self.assertTrue(builder._projection_reaches_axis(local, "projection"))
        relations = [
            data.get("relation_source")
            for _, _, data in builder.graph.edges(data=True)
        ]
        self.assertIn("nearest_safe_axis_segment_v12", relations)
        self.assertEqual(builder.door_axis_segment_repairs_added, 1)
        attachments = [
            data
            for _, _, data in builder.graph.edges(data=True)
            if data.get("relation_source") == "nearest_safe_axis_segment_v12"
        ]
        self.assertTrue(all(edge["edge_type"] == "component_connector" for edge in attachments))

    def test_door_width_is_not_compared_with_corridor_width(self):
        builder, space = make_builder(door_width=0.80)
        builder.config.general_min_route_width_m = 0.90
        long_corridor_graph(builder)

        self.assertEqual(builder._repair_space_door_approaches(space), 1)

    def test_non_pedestrian_opening_is_rejected(self):
        builder, space = make_builder(door_width=0.55)
        long_corridor_graph(builder)

        self.assertEqual(builder._repair_space_door_approaches(space), 0)
        self.assertGreater(builder.door_axis_segment_rejections, 0)

    def test_axis_segment_repair_does_not_cross_obstacle(self):
        builder, space = make_builder(door_width=0.80)
        space.footprint = Polygon(
            [(0, 0), (8, 0), (8, 40), (0, 40)],
            holes=[[(1, 10), (7, 10), (7, 30), (1, 30)]],
        )
        node(builder, "axis_south", 6, 2, "axis_endpoint")
        node(builder, "axis_north", 6, 38, "axis_endpoint")
        node(builder, "projection", 0.2, 20, "door_projection")
        node(builder, "door_side", 0.1, 20, "door_side", "door")
        edge(builder, "axis_south", "axis_north", "internal_axis")
        edge(builder, "door_side", "projection", "door_axis_projection")

        self.assertEqual(builder._repair_space_door_approaches(space), 0)


if __name__ == "__main__":
    unittest.main()
