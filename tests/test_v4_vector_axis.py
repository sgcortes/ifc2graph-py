import unittest

from shapely.geometry import LineString, Point, Polygon, box

from v4vector import VectorAxisConfig, VectorMedialAxisEngine


class V4VectorMedialAxisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = VectorMedialAxisEngine(
            VectorAxisConfig(
                boundary_sample_spacing_m=0.25,
                minimum_branch_length_m=0.35,
                simplification_tolerance_m=0.03,
                minimum_hole_area_m2=0.05,
                snap_tolerance_m=0.02,
                minimum_edge_length_m=0.05,
                maximum_component_connector_length_m=1.0,
            )
        )

    def test_column_is_retained_and_mesh_sliver_is_removed_consistently(self):
        column = box(4.8, 0.8, 5.2, 1.2)  # 0.16 m2
        mesh_sliver = box(2.0, 0.9, 2.05, 1.0)  # 0.005 m2
        footprint = Polygon(
            [(0, 0), (10, 0), (10, 2), (0, 2)],
            holes=[column.exterior.coords, mesh_sliver.exterior.coords],
        )

        result = self.engine.build(footprint, z=0.0)
        cleaned = Polygon(
            footprint.exterior.coords,
            holes=[column.exterior.coords],
        )

        self.assertGreater(len(result.lines), 0)
        self.assertEqual(result.input_holes, 2)
        self.assertEqual(result.retained_obstacle_holes, 1)
        self.assertEqual(result.removed_artifact_holes, 1)
        self.assertTrue(all(cleaned.buffer(1e-7).covers(line) for line in result.lines))
        self.assertTrue(all(not line.crosses(column) for line in result.lines))

    def test_long_component_connector_is_refused(self):
        domain = box(0, 0, 20, 5)
        lines = [
            LineString([(1, 2), (3, 2)]),
            LineString([(10, 2), (12, 2)]),
        ]

        connected, connectors = self.engine._connect_near_components(lines, domain)

        self.assertEqual(connectors, [])
        self.assertEqual(len(self.engine._line_components(connected)), 2)

    def test_short_visible_component_gap_is_joined(self):
        domain = box(0, 0, 10, 5)
        lines = [
            LineString([(1, 2), (3, 2)]),
            LineString([(3.5, 2), (6, 2)]),
        ]

        connected, connectors = self.engine._connect_near_components(lines, domain)

        self.assertEqual(len(connectors), 1)
        self.assertLessEqual(connectors[0].length, 1.0)
        self.assertEqual(len(self.engine._line_components(connected)), 1)

    def test_precision_grid_merges_centimetric_endpoint_gap(self):
        merged = self.engine._merge_segments(
            [
                LineString([(0, 0), (1.001, 0)]),
                LineString([(1.009, 0), (2, 0)]),
            ]
        )

        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0].length, 2.0, places=6)
        self.assertTrue(
            all(line.length >= self.engine.config.minimum_edge_length_m for line in merged)
        )

    def test_output_is_deterministic(self):
        footprint = Polygon([(0, 0), (9, 0), (9, 2.5), (0, 2.5)])
        first = self.engine.build(
            footprint,
            z=1.5,
            access_points=[Point(0.1, 1.0)],
        )
        second = self.engine.build(
            footprint,
            z=1.5,
            access_points=[Point(0.1, 1.0)],
        )

        self.assertEqual(
            sorted(line.wkt for line in first.lines),
            sorted(line.wkt for line in second.lines),
        )
        self.assertEqual(first.line_sources, second.line_sources)


if __name__ == "__main__":
    unittest.main()
