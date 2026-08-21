import unittest

from shapely.geometry import LineString, Polygon

from v5hsimg import (
    HSIMGConfig,
    classify_ramp_use,
    cleaned_clearance_domain,
    minimum_route_width,
)


class V5AccessSemanticsTests(unittest.TestCase):
    def test_minimum_route_width_detects_narrow_gap(self) -> None:
        corridor = Polygon([(0, 0), (10, 0), (10, 0.8), (0, 0.8)])
        axis = LineString([(1, 0.4, 0), (9, 0.4, 0)])
        self.assertAlmostEqual(
            minimum_route_width(axis, corridor, sample_spacing_m=0.1),
            0.8,
            places=6,
        )

    def test_clearance_domain_retains_real_columns_only(self) -> None:
        column = Polygon([(4, 2), (4.4, 2), (4.4, 2.4), (4, 2.4)])
        sliver = Polygon([(7, 2), (7.01, 2), (7.01, 2.01), (7, 2.01)])
        space = Polygon(
            [(0, 0), (10, 0), (10, 5), (0, 5)],
            holes=[column.exterior.coords, sliver.exterior.coords],
        )
        cleaned = cleaned_clearance_domain(space, minimum_hole_area_m2=0.05)
        self.assertEqual(len(cleaned.interiors), 1)

    def test_vehicle_ramp_name_disables_pedestrian_routing(self) -> None:
        result = classify_ramp_use("Rampa:Coches 15%", {})
        self.assertEqual(result["route_type"], "vehicle_only")
        self.assertFalse(result["pedestrian_access"])
        self.assertTrue(result["vehicle_access"])

    def test_explicit_pedestrian_access_overrides_vehicle_name(self) -> None:
        result = classify_ramp_use(
            "Garage vehicle ramp",
            {"HSIMG.PedestrianAccess": True, "HSIMG.VehicleAccess": True},
        )
        self.assertEqual(result["route_type"], "mixed")
        self.assertTrue(result["pedestrian_access"])

    def test_config_rejects_wheelchair_width_below_general_width(self) -> None:
        with self.assertRaises(ValueError):
            HSIMGConfig(
                general_min_route_width_m=1.0,
                wheelchair_min_route_width_m=0.9,
            )


if __name__ == "__main__":
    unittest.main()
