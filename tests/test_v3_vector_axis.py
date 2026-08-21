import unittest

from shapely.geometry import Point, Polygon

from v3vector import VectorAxisConfig, VectorMedialAxisEngine


class VectorMedialAxisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = VectorMedialAxisEngine(
            VectorAxisConfig(
                boundary_sample_spacing_m=0.25,
                minimum_branch_length_m=0.35,
                simplification_tolerance_m=0.03,
                containment_tolerance_m=0.02,
            )
        )

    def test_l_shaped_axis_is_vectorial_and_bounded(self) -> None:
        footprint = Polygon(
            [(0, 0), (8, 0), (8, 2), (3, 2), (3, 7), (0, 7)]
        )
        result = self.engine.build(
            footprint,
            z=4.2,
            access_points=[Point(0.1, 1.0), Point(2.9, 6.0)],
        )

        self.assertEqual(result.method, "vector_boundary_voronoi_medial_axis")
        self.assertGreater(len(result.lines), 0)
        self.assertGreater(result.boundary_samples, 0)
        self.assertTrue(all(footprint.buffer(0.02).covers(line) for line in result.lines))
        self.assertTrue(
            all(abs(coordinate[2] - 4.2) < 1e-9 for line in result.lines for coordinate in line.coords)
        )

    def test_courtyard_hole_is_not_crossed(self) -> None:
        footprint = Polygon(
            [(0, 0), (12, 0), (12, 10), (0, 10)],
            holes=[[(3, 3), (9, 3), (9, 7), (3, 7)]],
        )
        result = self.engine.build(footprint, z=0.0)

        self.assertGreater(len(result.lines), 0)
        self.assertTrue(all(footprint.buffer(0.02).covers(line) for line in result.lines))
        self.assertTrue(
            all(not line.crosses(Polygon([(3, 3), (9, 3), (9, 7), (3, 7)])) for line in result.lines)
        )

    def test_small_ignored_hole_is_clipped_and_routed_around(self) -> None:
        column = Polygon([(4.8, 0.8), (5.2, 0.8), (5.2, 1.2), (4.8, 1.2)])
        footprint = Polygon(
            [(0, 0), (10, 0), (10, 2), (0, 2)],
            holes=[list(column.exterior.coords)],
        )
        result = self.engine.build(footprint, z=0.0)

        self.assertGreater(len(result.lines), 0)
        self.assertEqual(result.connected_components, 1)
        self.assertTrue(all(footprint.buffer(1e-7).covers(line) for line in result.lines))

    def test_output_is_deterministic(self) -> None:
        footprint = Polygon([(0, 0), (9, 0), (9, 2.5), (0, 2.5)])
        first = self.engine.build(footprint, z=1.5)
        second = self.engine.build(footprint, z=1.5)

        first_wkts = sorted(line.wkt for line in first.lines)
        second_wkts = sorted(line.wkt for line in second.lines)
        self.assertEqual(first_wkts, second_wkts)


if __name__ == "__main__":
    unittest.main()
