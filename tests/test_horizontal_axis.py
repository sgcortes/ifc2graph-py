import unittest

from shapely.geometry import LineString, Point, Polygon, box

from hsimg import (
    HSIMGConfig,
    _horizontal_substrings,
    _pixel_skeleton_lines,
    local_interior_point_at_boundary,
)


class HorizontalAxisTests(unittest.TestCase):
    def test_ring_corridor_ignores_small_obstacle_holes(self):
        outer = box(0, 0, 20, 14)
        courtyard = box(4, 4, 16, 10)
        small_holes = [box(x, 11, x + 0.4, 11.4) for x in range(5, 15, 2)]
        corridor = Polygon(
            outer.exterior.coords,
            [courtyard.exterior.coords, *(hole.exterior.coords for hole in small_holes)],
        )

        axes = _pixel_skeleton_lines(
            corridor,
            0.0,
            HSIMGConfig(
                skeleton_resolution_m=0.2,
                medial_axis_min_hole_area_m2=1.0,
            ),
        )

        self.assertLessEqual(len(axes), 4)
        self.assertGreater(max(axis.length for axis in axes), 30.0)

    def test_axis_is_split_at_door_projection_stations(self):
        pieces = _horizontal_substrings(
            LineString([(0, 0, 0), (10, 0, 0)]),
            [2.0, 7.0],
            0.0,
            1e-6,
        )

        self.assertEqual(len(pieces), 3)
        self.assertAlmostEqual(sum(piece.length for piece in pieces), 10.0)
        self.assertEqual([round(piece.length, 4) for piece in pieces], [2.0, 5.0, 3.0])

    def test_door_side_inset_stays_local_in_courtyard_ring(self):
        corridor = Polygon(
            [(0, 0), (20, 0), (20, 20), (0, 20)],
            holes=[[(5, 5), (15, 5), (15, 15), (5, 15)]],
        )
        door = Point(10, 5)

        side = local_interior_point_at_boundary(corridor, door, 0.05)

        self.assertIsNotNone(side)
        self.assertTrue(corridor.covers(side))
        self.assertLess(side.distance(door), 0.20)
        self.assertGreaterEqual(side.distance(corridor.boundary), 0.049)


if __name__ == "__main__":
    unittest.main()
