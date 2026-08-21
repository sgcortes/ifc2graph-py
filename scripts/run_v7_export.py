"""Generate V7 HSIMG products with elevators derived from labelled spaces."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v7hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path, help="Input IFC2X3 or IFC4 model")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".qa/v7_full_run"),
        help="Directory for V7 GeoPackage, graph and validation products",
    )
    parser.add_argument("--boundary-spacing", type=float, default=0.30)
    parser.add_argument("--branch-pruning", type=float, default=0.50)
    parser.add_argument("--minimum-obstacle-hole-area", type=float, default=0.05)
    parser.add_argument("--node-snap", type=float, default=0.02)
    parser.add_argument("--minimum-axis-segment", type=float, default=0.05)
    parser.add_argument("--maximum-component-connector", type=float, default=1.00)
    parser.add_argument("--general-min-width", type=float, default=0.90)
    parser.add_argument("--wheelchair-min-width", type=float, default=1.20)
    parser.add_argument("--clearance-sample-spacing", type=float, default=0.10)
    parser.add_argument("--clearance-domain-tolerance", type=float, default=0.01)
    parser.add_argument("--stair-landing-max-connector", type=float, default=6.00)
    parser.add_argument("--stair-system-max-transition", type=float, default=3.00)
    parser.add_argument("--elevator-centroid-tolerance", type=float, default=0.85)
    parser.add_argument("--elevator-footprint-tolerance", type=float, default=0.15)
    parser.add_argument("--elevator-boundary-opening-tolerance", type=float, default=0.25)
    parser.add_argument("--elevator-hall-boundary-tolerance", type=float, default=0.25)
    parser.add_argument("--raster-axis", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = HSIMGConfig(
        vector_boundary_sample_spacing_m=args.boundary_spacing,
        medial_axis_pruning_length_m=args.branch_pruning,
        medial_axis_min_hole_area_m2=args.minimum_obstacle_hole_area,
        vector_snap_tolerance_m=args.node_snap,
        vector_min_edge_length_m=args.minimum_axis_segment,
        vector_max_component_connector_length_m=args.maximum_component_connector,
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        route_width_sample_spacing_m=args.clearance_sample_spacing,
        clearance_domain_tolerance_m=args.clearance_domain_tolerance,
        stair_landing_max_connector_length_m=args.stair_landing_max_connector,
        stair_system_max_transition_length_m=args.stair_system_max_transition,
        elevator_space_centroid_tolerance_m=args.elevator_centroid_tolerance,
        elevator_space_footprint_tolerance_m=args.elevator_footprint_tolerance,
        elevator_boundary_opening_tolerance_m=(
            args.elevator_boundary_opening_tolerance
        ),
        elevator_hall_boundary_tolerance_m=(
            args.elevator_hall_boundary_tolerance
        ),
        prefer_vector_medial_axis=not args.raster_axis,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    nonreciprocal = builder._nonreciprocal_pedestrian_edges()
    if nonreciprocal:
        raise RuntimeError(
            f"V7 export invariant failed: {len(nonreciprocal)} nonreciprocal pedestrian edges"
        )
    fragmented_stairs = builder._fragmented_stair_subgraphs()
    if fragmented_stairs:
        raise RuntimeError(
            f"V7 export invariant failed: {len(fragmented_stairs)} fragmented stair subgraphs"
        )

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v7_output.gpkg")
    graphml = output_dir / "HSIMG_v7_graph.graphml"
    graph_json = output_dir / "HSIMG_v7_graph.json"
    builder.export_graph(graphml, graph_json)
    validation_csv = output_dir / "HSIMG_v7_validation_report.csv"
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        edge_types = dict(connection.execute(
            "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type ORDER BY edge_type"
        ).fetchall())
        elevator_systems = connection.execute(
            "SELECT COUNT(*) FROM vertical_elements WHERE vertical_type='elevator'"
        ).fetchone()[0]
        elevator_stops = connection.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE node_role='elevator_stop'"
        ).fetchone()[0]
        elevator_vertical_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE edge_type='vertical_path' "
            "AND mobility_mode='elevator'"
        ).fetchone()[0]
        elevator_access_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE edge_type='elevator_cabin_access'"
        ).fetchone()[0]
        accessible_elevator_access_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE edge_type='elevator_cabin_access' "
            "AND accessible_wheelchair=1"
        ).fetchone()[0]
        remaining_non_pedestrian = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE accessible_general=0"
        ).fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"V7 GeoPackage integrity failed: {integrity}")
    if remaining_non_pedestrian:
        raise RuntimeError("V7 export invariant failed: non-pedestrian edges remain")

    issue_counts = Counter(issue.issue_type for issue in builder.issues)
    report = {
        "version": 7,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "graph_scope": "general_pedestrian_only",
            "elevator_source": "explicitly_labelled_IfcSpace_stacks",
            "elevator_space_terms": list(config.elevator_space_terms),
            "elevator_space_centroid_tolerance_m": config.elevator_space_centroid_tolerance_m,
            "elevator_space_footprint_tolerance_m": config.elevator_space_footprint_tolerance_m,
            "elevator_access_method": "IfcSpace_boundary_wall_opening_only",
            "elevator_boundary_opening_tolerance_m": (
                config.elevator_boundary_opening_tolerance_m
            ),
            "elevator_hall_boundary_tolerance_m": (
                config.elevator_hall_boundary_tolerance_m
            ),
        },
        "outputs": {
            "geopackage": str(gpkg),
            "graph_json": str(graph_json),
            "graphml": str(graphml),
            "validation_csv": str(validation_csv),
        },
        "summary": builder.summary(),
        "validation": {
            "gpkg_integrity": integrity,
            "edge_types": edge_types,
            "elevator_systems": elevator_systems,
            "elevator_stops": elevator_stops,
            "directed_elevator_vertical_edges": elevator_vertical_edges,
            "directed_elevator_cabin_access_edges": elevator_access_edges,
            "directed_accessible_elevator_cabin_access_edges": accessible_elevator_access_edges,
            "elevator_door_connections": builder.elevator_door_connections,
            "synthetic_elevator_opening_portals": (
                builder.synthetic_elevator_opening_portals
            ),
            "rejected_boundary_openings": (
                builder.elevator_boundary_openings_rejected
            ),
            "elevator_stops_without_door": issue_counts["elevator_stop_unconnected_v7"],
            "elevator_stops_without_boundary_opening": issue_counts[
                "elevator_stop_without_boundary_opening_v7"
            ],
            "elevator_boundary_openings_without_hall_access": issue_counts[
                "elevator_boundary_opening_without_hall_access_v7"
            ],
            "elevator_stops_without_accessible_door": issue_counts[
                "elevator_stop_without_accessible_door_v7"
            ],
            "duplicate_elevator_spaces": issue_counts["duplicate_elevator_spaces_on_storey_v7"],
            "rejected_transport_elevators": builder.rejected_transport_elevators,
            "remaining_non_pedestrian_edges": remaining_non_pedestrian,
            "nonreciprocal_pedestrian_edges": len(nonreciprocal),
            "fragmented_stair_subgraphs": len(fragmented_stairs),
        },
    }
    report_path = output_dir / "run_v7_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
