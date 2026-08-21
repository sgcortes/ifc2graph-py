import json
from types import SimpleNamespace
import unittest

import networkx as nx
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry import MultiLineString

from v6hsimg import (
    HSIMGBuilder,
    HSIMGConfig,
    connect_stair_flight_lines,
    line_supported_by_clearance_domain,
)


class V6PedestrianPruningTests(unittest.TestCase):
    def test_stair_flights_are_joined_through_landing_gap(self):
        flights = MultiLineString([
            LineString([(0, 0, 0), (2, 0, 2)]),
            LineString([(4, 0, 2.3), (6, 0, 4)]),
        ])

        path, connectors, unresolved = connect_stair_flight_lines(
            flights,
            maximum_connector_length_m=3.0,
        )

        self.assertIsInstance(path, MultiLineString)
        self.assertEqual(connectors, 1)
        self.assertEqual(unresolved, [])
        self.assertEqual(len(path.geoms), 3)
        self.assertEqual(tuple(path.geoms[1].coords[0]), (2.0, 0.0, 2.0))
        self.assertEqual(tuple(path.geoms[1].coords[-1]), (4.0, 0.0, 2.3))

    def test_excessive_stair_flight_gap_is_not_fabricated(self):
        flights = MultiLineString([
            LineString([(0, 0, 0), (1, 0, 1)]),
            LineString([(10, 0, 1.2), (11, 0, 2)]),
        ])

        path, connectors, unresolved = connect_stair_flight_lines(
            flights,
            maximum_connector_length_m=3.0,
        )

        self.assertEqual(connectors, 0)
        self.assertEqual(len(path.geoms), 2)
        self.assertEqual(len(unresolved), 1)
        self.assertGreater(unresolved[0], 3.0)

    def test_consecutive_ifc_stairs_are_joined_across_shared_landing(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig(stair_system_max_transition_length_m=3.0)
        builder.graph = nx.MultiDiGraph()
        builder.stair_system_transitions_added = 0
        builder.vertical_elements = {
            "lower_stair": SimpleNamespace(
                vertical_id="lower_stair", vertical_type="stair", ifc_guid="lower-guid"
            ),
            "upper_stair": SimpleNamespace(
                vertical_id="upper_stair", vertical_type="stair", ifc_guid="upper-guid"
            ),
        }
        coordinates = {
            "lower_bottom": (0.0, 0.0, 5.0, "level_1", "lower_stair"),
            "lower_top": (0.0, 0.0, 10.0, "level_2", "lower_stair"),
            "upper_bottom": (2.0, 0.0, 10.5, "level_2", "upper_stair"),
            "upper_top": (2.0, 0.0, 15.0, "level_3", "upper_stair"),
        }
        for node_id, (x, y, z, storey_id, parent_id) in coordinates.items():
            builder.graph.add_node(
                node_id,
                node_type="internal_mobility",
                parent_node_id=parent_id,
                storey_id=storey_id,
                x=x,
                y=y,
                z=z,
                geometry=Point(x, y, z),
            )

        added = builder._connect_consecutive_stair_objects()

        self.assertEqual(added, 1)
        self.assertEqual(builder.stair_system_transitions_added, 1)
        forward = list(builder.graph.get_edge_data("lower_top", "upper_bottom").values())
        reverse = list(builder.graph.get_edge_data("upper_bottom", "lower_top").values())
        self.assertEqual(forward[0]["edge_type"], "stair_landing_transition")
        self.assertEqual(reverse[0]["edge_type"], "stair_landing_transition")
        self.assertTrue(forward[0]["accessible_general"])
        self.assertFalse(forward[0]["accessible_wheelchair"])

    def test_distant_ifc_stairs_are_not_joined(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig(stair_system_max_transition_length_m=3.0)
        builder.graph = nx.MultiDiGraph()
        builder.stair_system_transitions_added = 0
        builder.vertical_elements = {
            "a": SimpleNamespace(vertical_id="a", vertical_type="stair", ifc_guid="a"),
            "b": SimpleNamespace(vertical_id="b", vertical_type="stair", ifc_guid="b"),
        }
        for node_id, x, z, storey_id, parent in (
            ("a0", 0.0, 0.0, "l0", "a"),
            ("a1", 0.0, 5.0, "l1", "a"),
            ("b0", 10.0, 5.0, "l1", "b"),
            ("b1", 10.0, 10.0, "l2", "b"),
        ):
            builder.graph.add_node(
                node_id, node_type="internal_mobility", parent_node_id=parent,
                storey_id=storey_id, x=x, y=0.0, z=z,
                geometry=Point(x, 0.0, z),
            )

        self.assertEqual(builder._connect_consecutive_stair_objects(), 0)
        self.assertEqual(builder.graph.number_of_edges(), 0)

    def test_handicap_accessible_does_not_close_door(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig(default_door_open=True)
        door = SimpleNamespace(
            ifc_guid="door-guid",
            name="Internal door",
            properties={"Pset_DoorCommon.HandicapAccessible": False},
        )

        is_open, source = builder._door_open_state(door)

        self.assertTrue(is_open)
        self.assertEqual(source, "config:default_door_open")

    def test_explicit_closed_property_closes_door(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig(default_door_open=True)
        door = SimpleNamespace(
            ifc_guid="door-guid",
            name="Internal door",
            properties={"Pset_DoorCommon.IsClosed": True},
        )

        is_open, source = builder._door_open_state(door)

        self.assertFalse(is_open)
        self.assertEqual(source, "IFC_property:Pset_DoorCommon.IsClosed")

    def test_handicap_accessible_only_controls_wheelchair_state(self):
        door = SimpleNamespace(
            properties={"Pset_DoorCommon.HandicapAccessible": False},
        )

        accessible, source = HSIMGBuilder._explicit_door_wheelchair_state(door)

        self.assertFalse(accessible)
        self.assertEqual(source, "Pset_DoorCommon.HandicapAccessible")

    def test_closed_door_restriction_is_applied_in_both_directions(self):
        builder = object.__new__(HSIMGBuilder)
        builder.graph = nx.MultiDiGraph()
        builder.door_access_metadata = {
            "door": {
                "door_open": False,
                "door_status": "closed",
                "connected_space_count": 1,
            }
        }
        builder.graph.add_node("door", parent_node_id=None, metadata_json="{}")
        builder.graph.add_node("side", parent_node_id="door", metadata_json="{}")
        builder.graph.add_node("space", parent_node_id=None, metadata_json="{}")
        builder.graph.add_edge(
            "side", "space", key="forward", accessible_general=True,
            accessible_wheelchair=True, metadata_json="{}",
        )
        builder.graph.add_edge(
            "space", "side", key="reverse", accessible_general=True,
            accessible_wheelchair=True, metadata_json="{}",
        )

        builder._apply_door_metadata_to_graph()

        for _, _, data in builder.graph.edges(data=True):
            self.assertFalse(data["accessible_general"])
            self.assertFalse(data["accessible_wheelchair"])
            self.assertEqual(data["restriction_reason"], "door_closed")

    def test_open_nonwheelchair_door_restricts_all_incident_edges(self):
        builder = object.__new__(HSIMGBuilder)
        builder.graph = nx.MultiDiGraph()
        builder.door_access_metadata = {
            "door": {
                "door_open": True,
                "door_status": "open",
                "connected_space_count": 2,
                "wheelchair_accessible": False,
            }
        }
        builder.graph.add_node("door", parent_node_id=None, metadata_json="{}")
        builder.graph.add_node("side", parent_node_id="door", metadata_json="{}")
        builder.graph.add_node("projection", parent_node_id="space", metadata_json="{}")
        builder.graph.add_edge(
            "side", "projection", key="forward", accessible_general=True,
            accessible_wheelchair=True, metadata_json="{}",
        )
        builder.graph.add_edge(
            "projection", "side", key="reverse", accessible_general=True,
            accessible_wheelchair=True, metadata_json="{}",
        )

        builder._apply_door_metadata_to_graph()

        for _, _, data in builder.graph.edges(data=True):
            self.assertTrue(data["accessible_general"])
            self.assertFalse(data["accessible_wheelchair"])
            self.assertEqual(
                data["restriction_reason"],
                "door_not_wheelchair_accessible",
            )

    def test_nonreciprocal_pedestrian_edge_is_detected(self):
        builder = object.__new__(HSIMGBuilder)
        builder.graph = nx.MultiDiGraph()
        builder.graph.add_edge(
            "a", "b", edge_type="space_access", subgraph_id=None,
            accessible_general=True,
        )
        self.assertEqual(
            builder._nonreciprocal_pedestrian_edges(),
            [("a", "b", "space_access")],
        )
        builder.graph.add_edge(
            "b", "a", edge_type="space_access", subgraph_id=None,
            accessible_general=True,
        )
        self.assertEqual(builder._nonreciprocal_pedestrian_edges(), [])

    def test_eroded_domain_rejects_substandard_corridor(self):
        narrow = Polygon([(0, 0), (10, 0), (10, 0.8), (0, 0.8)])
        axis = LineString([(1, 0.4), (9, 0.4)])
        self.assertFalse(
            line_supported_by_clearance_domain(axis, narrow, required_width_m=0.9)
        )

    def test_eroded_domain_accepts_wide_corridor(self):
        wide = Polygon([(0, 0), (10, 0), (10, 1.4), (0, 1.4)])
        axis = LineString([(1, 0.7), (9, 0.7)])
        self.assertTrue(
            line_supported_by_clearance_domain(axis, wide, required_width_m=0.9)
        )
        self.assertTrue(
            line_supported_by_clearance_domain(axis, wide, required_width_m=1.2)
        )

    def test_door_projection_no_longer_exempts_narrow_internal_edge(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig(
            general_min_route_width_m=0.9,
            wheelchair_min_route_width_m=1.2,
        )
        builder.graph = nx.MultiDiGraph()
        builder.issues = []
        builder.clearance_restricted_directed_edges = 0
        footprint = Polygon([(0, 0), (4, 0), (4, 0.8), (0, 0.8)])
        builder.spaces = {
            "space": SimpleNamespace(
                space_id="space",
                ifc_guid="space-guid",
                name="Narrow hall",
                footprint=footprint,
                interior_point=Point(2, 0.4),
            )
        }
        builder.graph.add_node(
            "projection",
            parent_node_id="space",
            node_role="door_projection",
            geometry=Point(0.5, 0.4, 0),
        )
        builder.graph.add_node(
            "axis",
            parent_node_id="space",
            node_role="axis_endpoint",
            geometry=Point(3.5, 0.4, 0),
        )
        builder.graph.add_edge(
            "projection",
            "axis",
            key="edge",
            edge_type="internal_axis",
            geometry=LineString([(0.5, 0.4, 0), (3.5, 0.4, 0)]),
            accessible_general=True,
            accessible_wheelchair=True,
            metadata_json=json.dumps({}),
        )

        builder._apply_horizontal_clearance()

        data = builder.graph["projection"]["axis"]["edge"]
        self.assertFalse(data["accessible_general"])
        self.assertFalse(data["accessible_wheelchair"])
        self.assertEqual(
            data["restriction_reason"],
            "insufficient_general_clearance",
        )
        metadata = json.loads(data["metadata_json"])
        self.assertFalse(metadata["door_transition_exemption"])

    def test_inaccessible_edge_is_physically_removed(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig()
        builder.graph = nx.MultiDiGraph()
        builder.pruned_non_pedestrian_directed_edges = 0
        builder.graph.add_node("a")
        builder.graph.add_node("b")
        builder.graph.add_edge(
            "a",
            "b",
            key="blocked",
            edge_type="internal_axis",
            accessible_general=False,
        )
        builder.graph.add_edge(
            "b",
            "a",
            key="blocked-reverse",
            edge_type="internal_axis",
            accessible_general=False,
        )

        removed = builder._remove_inaccessible_edges(horizontal_only=True)

        self.assertEqual(removed, 2)
        self.assertEqual(builder.graph.number_of_edges(), 0)

    def test_component_without_door_access_is_removed(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig()
        builder.graph = nx.MultiDiGraph()
        builder.subgraphs = [
            {"subgraph_id": "sg", "subgraph_type": "horizontal_mobility"}
        ]
        builder.pruned_internal_nodes = 0
        builder.pruned_accessless_components = 0
        for node_id in ("a", "b"):
            builder.graph.add_node(
                node_id,
                subgraph_id="sg",
                hierarchy_level=2,
                node_role="axis_endpoint",
                node_type="internal_mobility",
            )
        builder.graph.add_edge(
            "a", "b", edge_type="internal_axis", subgraph_id="sg"
        )

        removed = builder._prune_accessless_components()

        self.assertEqual(removed, 1)
        self.assertNotIn("a", builder.graph)
        self.assertNotIn("b", builder.graph)

    def test_unprotected_spur_is_removed_between_protected_accesses(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig()
        builder.graph = nx.MultiDiGraph()
        builder.pruned_internal_nodes = 0
        builder.pruned_unprotected_dead_end_nodes = 0
        roles = {
            "door_a": "door_projection",
            "junction": "axis_endpoint",
            "door_b": "door_projection",
            "spur": "axis_endpoint",
        }
        for node_id, role in roles.items():
            builder.graph.add_node(node_id, node_role=role)
        for source, target in (
            ("door_a", "junction"),
            ("junction", "door_a"),
            ("junction", "door_b"),
            ("door_b", "junction"),
            ("junction", "spur"),
            ("spur", "junction"),
        ):
            builder.graph.add_edge(
                source,
                target,
                edge_type="internal_axis",
                subgraph_id="sg",
            )

        removed = builder._prune_dead_end_branches()

        self.assertEqual(removed, 1)
        self.assertNotIn("spur", builder.graph)
        self.assertIn("door_a", builder.graph)
        self.assertIn("door_b", builder.graph)
        self.assertIn("junction", builder.graph)


if __name__ == "__main__":
    unittest.main()
