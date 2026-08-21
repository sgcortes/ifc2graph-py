import unittest

import networkx as nx
from shapely.geometry import LineString, Point

from v4hsimg import HSIMGBuilder, HSIMGConfig


class V4BuilderCleanupTests(unittest.TestCase):
    def test_short_axis_piece_is_contracted_into_projection(self):
        builder = object.__new__(HSIMGBuilder)
        builder.config = HSIMGConfig(vector_min_edge_length_m=0.05)
        builder.graph = nx.MultiDiGraph()
        builder.subgraphs = [
            {
                "subgraph_id": "sg",
                "parent_node_id": "space",
                "node_count": 3,
                "edge_count": 4,
            }
        ]
        builder.graph.add_node(
            "projection",
            node_role="door_projection",
            subgraph_id="sg",
            geometry=Point(0, 0, 0),
        )
        builder.graph.add_node(
            "endpoint",
            node_role="axis_endpoint",
            subgraph_id="sg",
            geometry=Point(0.01, 0, 0),
        )
        builder.graph.add_node(
            "far",
            node_role="axis_endpoint",
            subgraph_id="sg",
            geometry=Point(1, 0, 0),
        )

        def add(source, target, length):
            geometry = LineString(
                [
                    builder.graph.nodes[source]["geometry"].coords[0],
                    builder.graph.nodes[target]["geometry"].coords[0],
                ]
            )
            key = f"{source}-{target}"
            builder.graph.add_edge(
                source,
                target,
                key=key,
                edge_id=key,
                edge_type="internal_axis",
                subgraph_id="sg",
                geometry=geometry,
                length_3d=length,
                estimated_time=length,
                effort_cost=length,
            )

        add("projection", "endpoint", 0.01)
        add("endpoint", "projection", 0.01)
        add("endpoint", "far", 0.99)
        add("far", "endpoint", 0.99)

        merged = builder._collapse_short_horizontal_edges()

        self.assertEqual(merged, {"sg": 1})
        self.assertNotIn("endpoint", builder.graph)
        self.assertIn("projection", builder.graph)
        self.assertTrue(builder.graph.has_edge("projection", "far"))
        self.assertTrue(builder.graph.has_edge("far", "projection"))
        self.assertTrue(
            all(
                data["length_3d"] >= 0.05
                for _, _, data in builder.graph.edges(data=True)
            )
        )


if __name__ == "__main__":
    unittest.main()
