"""Generate and validate clearance-aware V5 HSIMG products from an IFC model."""

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

from v5hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path, help="Input IFC2X3 or IFC4 model")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".qa/v5_full_run"),
        help="Directory for V5 GeoPackage, graph and validation products",
    )
    parser.add_argument("--boundary-spacing", type=float, default=0.30)
    parser.add_argument("--branch-pruning", type=float, default=0.50)
    parser.add_argument("--minimum-obstacle-hole-area", type=float, default=0.05)
    parser.add_argument("--node-snap", type=float, default=0.02)
    parser.add_argument("--minimum-axis-segment", type=float, default=0.05)
    parser.add_argument("--maximum-component-connector", type=float, default=1.00)
    parser.add_argument(
        "--general-min-width",
        type=float,
        default=0.90,
        help="Minimum usable width for a general pedestrian route (m)",
    )
    parser.add_argument(
        "--wheelchair-min-width",
        type=float,
        default=1.20,
        help="Minimum usable width for a wheelchair route (m)",
    )
    parser.add_argument(
        "--clearance-sample-spacing",
        type=float,
        default=0.10,
        help="Maximum spacing between route-clearance samples (m)",
    )
    parser.add_argument(
        "--raster-axis",
        action="store_true",
        help="Use the legacy raster skeleton instead of the V4/V5 vector axis",
    )
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
        vector_max_component_connector_length_m=(
            args.maximum_component_connector
        ),
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        route_width_sample_spacing_m=args.clearance_sample_spacing,
        prefer_vector_medial_axis=not args.raster_axis,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v5_output.gpkg")
    graphml = output_dir / "HSIMG_v5_graph.graphml"
    graph_json = output_dir / "HSIMG_v5_graph.json"
    builder.export_graph(graphml, graph_json)
    validation_csv = output_dir / "HSIMG_v5_validation_report.csv"
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        layers = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM gpkg_contents ORDER BY table_name"
            )
        }
        edge_types = dict(
            connection.execute(
                "SELECT edge_type, COUNT(*) FROM graph_edges "
                "GROUP BY edge_type ORDER BY edge_type"
            ).fetchall()
        )
        route_restrictions = dict(
            connection.execute(
                "SELECT COALESCE(restriction_reason, '<NONE>'), COUNT(*) "
                "FROM graph_edges GROUP BY restriction_reason "
                "ORDER BY restriction_reason"
            ).fetchall()
        )
        orphan_doors = connection.execute(
            "SELECT COUNT(*) FROM door_access_v5 WHERE orphan_external=1"
        ).fetchone()[0]
        ramp_footprints = (
            connection.execute(
                "SELECT COUNT(*) FROM vertical_footprints WHERE vertical_type='ramp'"
            ).fetchone()[0]
            if "vertical_footprints" in layers
            else 0
        )

    payload = json.loads(graph_json.read_text(encoding="utf-8"))
    issue_counts = Counter(issue.issue_type for issue in builder.issues)
    report = {
        "version": 5,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "axis_method": (
                "vector_obstacle_aware_medial_axis_v4_plus_clearance_v5"
                if config.prefer_vector_medial_axis
                else "raster_axis_plus_clearance_v5"
            ),
            "general_min_route_width_m": config.general_min_route_width_m,
            "wheelchair_min_route_width_m": (
                config.wheelchair_min_route_width_m
            ),
            "clearance_sample_spacing_m": (
                config.route_width_sample_spacing_m
            ),
            "minimum_obstacle_hole_area_m2": (
                config.medial_axis_min_hole_area_m2
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
            "layers": sorted(layers),
            "edge_types": edge_types,
            "route_restrictions": route_restrictions,
            "orphan_external_doors": orphan_doors,
            "ramp_footprints": ramp_footprints,
            "json_nodes": len(payload.get("nodes", [])),
            "json_edges": len(payload.get("edges", payload.get("links", []))),
            "narrow_route_issues": issue_counts[
                "narrow_general_routes_restricted"
            ],
            "orphan_door_issues": issue_counts["orphan_external_door"],
            "ramp_slope_mismatches": issue_counts["ramp_slope_name_mismatch"],
        },
    }
    report_path = output_dir / "run_v5_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
