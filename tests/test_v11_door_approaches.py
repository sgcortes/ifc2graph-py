from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point, Polygon

from v11hsimg import HSIMGBuilder, HSIMGConfig


def make_builder(footprint, door_width=1.20):
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
    builder.door_approach_repairs_added = 0
    builder.door_approach_spaces_repaired = 0
    builder.door_projections_without_axis_reach = 0
    builder.exterior_entrances_without_interior_reach = 0
    space = SimpleNamespace(
        space_id="space",
        ifc_guid="space-guid",
        ifc_class="IfcSpace",
        name="Entrance vestibule",
        node_class="horizontal_mobility",
        footprint=footprint,
        interior_point=footprint.representative_point(),
        elevation=0.0,
        storey_id="storey",
    )
    door = SimpleNamespace(
        door_id="door",
        ifc_guid="door-guid",
        name="Entrance door",
        width=door_width,
        wheelchair_accessible=door_width >= 0.80,
        point=Point(2, 6, 0),
    )
    builder.spaces = {"space": space}
    builder.doors = {"door": door}
    return builder, space


def add_node(builder, node_id, x, y, role, parent="space"):
    builder.graph.add_node(
        node_id,
        node_type="internal_mobility" if role != "door_side" else "door_access",
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


def add_edge(builder, source, target, edge_type):
    line = LineString([
        builder.graph.nodes[source]["geometry"].coords[0],
        builder.graph.nodes[target]["geometry"].coords[0],
    ])
    builder._add_bidirectional_edge(
        source,
        target,
        edge_type=edge_type,
        mobility_mode="walk",
        subgraph_id="subgraph",
        geometry=line,
    )


def add_isolated_door_projection(builder, x=2, y=5.8):
    add_node(builder, "axis_a", 2, 2, "axis_endpoint")
    add_node(builder, "axis_b", 6, 2, "axis_endpoint")
    add_node(builder, "projection", x, y, "door_projection")
    add_node(builder, "door_side", x, min(5.95, y + 0.1), "door_side", "door")
    add_edge(builder, "axis_a", "axis_b", "internal_axis")
    add_edge(builder, "door_side", "projection", "door_axis_projection")


class V11DoorApproachTests(unittest.TestCase):
    def test_wide_door_throat_connects_projection_to_safe_axis(self):
        builder, space = make_builder(
            Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]),
            door_width=1.45,
        )
        add_isolated_door_projection(builder)

        self.assertEqual(builder._repair_space_door_approaches(space), 1)
        local, _ = builder._horizontal_space_graph("space")
        self.assertTrue(builder._projection_reaches_axis(local, "projection"))
        throat_edges = [
            data
            for _, _, data in builder.graph.edges(data=True)
            if data.get("relation_source") == "door_width_validated_throat_v11"
        ]
        self.assertEqual(len(throat_edges), 2)
        self.assertTrue(all(edge["accessible_general"] for edge in throat_edges))

    def test_narrow_door_does_not_receive_general_access(self):
        builder, space = make_builder(
            Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]),
            door_width=0.75,
        )
        add_isolated_door_projection(builder)

        self.assertEqual(builder._repair_space_door_approaches(space), 0)
        self.assertGreater(builder.door_approach_rejections, 0)

    def test_door_approach_does_not_cross_clearance_barrier(self):
        builder, space = make_builder(Polygon(
            [(0, 0), (10, 0), (10, 6), (0, 6)],
            holes=[[(4, 0.1), (6, 0.1), (6, 5.9), (4, 5.9)]],
        ))
        add_node(builder, "axis_a", 7, 2, "axis_endpoint")
        add_node(builder, "axis_b", 9, 2, "axis_endpoint")
        add_node(builder, "projection", 2, 5.5, "door_projection")
        add_node(builder, "door_side", 2, 5.8, "door_side", "door")
        add_edge(builder, "axis_a", "axis_b", "internal_axis")
        add_edge(builder, "door_side", "projection", "door_axis_projection")

        self.assertEqual(builder._repair_space_door_approaches(space), 0)

    def test_exterior_entrance_reach_validation_detects_disconnection(self):
        builder, _ = make_builder(Polygon([(0, 0), (8, 0), (8, 6), (0, 6)]))
        interior = SimpleNamespace(
            door_id="interior",
            ifc_guid="interior-guid",
            name="Interior door",
            point=Point(6, 2, 0),
        )
        builder.doors["interior"] = interior
        builder.door_access_metadata = {
            "door": {"entrance_exit_eligible": True, "connected_space_count": 1},
            "interior": {"entrance_exit_eligible": False, "connected_space_count": 2},
        }
        builder.graph.add_node("door")
        builder.graph.add_node("interior")

        builder._validate_exterior_entrance_reach()

        self.assertEqual(builder.exterior_entrances_without_interior_reach, 1)


if __name__ == "__main__":
    unittest.main()
