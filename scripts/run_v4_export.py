"""Generate and validate obstacle-aware V4 HSIMG products from an IFC model."""

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

from v4hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path, help="Input IFC2X3 or IFC4 model")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".qa/v4_full_run"),
        help="Directory for V4 GeoPackage, graph and validation products",
    )
    parser.add_argument(
        "--boundary-spacing",
        type=float,
        default=0.30,
        help="Boundary sampling interval in metres",
    )
    parser.add_argument(
        "--branch-pruning",
        type=float,
        default=0.50,
        help="Maximum unprotected leaf length removed from the medial axis",
    )
    parser.add_argument(
        "--minimum-obstacle-hole-area",
        type=float,
        default=0.05,
        help="Minimum polygon-hole area retained as a fixed obstacle (m2)",
    )
    parser.add_argument(
        "--node-snap",
        type=float,
        default=0.02,
        help="Precision grid used to merge near-coincident axis nodes (m)",
    )
    parser.add_argument(
        "--minimum-axis-segment",
        type=float,
        default=0.05,
        help="Minimum vector-axis segment length retained (m)",
    )
    parser.add_argument(
        "--maximum-component-connector",
        type=float,
        default=1.00,
        help="Maximum local component-repair connector length (m)",
    )
    parser.add_argument(
        "--raster-axis",
        action="store_true",
        help="Disable the V4 vector method and use the legacy raster skeleton",
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
        prefer_vector_medial_axis=not args.raster_axis,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v4_output.gpkg")
    graphml = output_dir / "HSIMG_v4_graph.graphml"
    graph_json = output_dir / "HSIMG_v4_graph.json"
    builder.export_graph(graphml, graph_json)
    validation_csv = output_dir / "HSIMG_v4_validation_report.csv"
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        axis_methods = dict(
            connection.execute(
                "SELECT extraction_method, COUNT(*) FROM mobility_axes "
                "GROUP BY extraction_method ORDER BY extraction_method"
            ).fetchall()
        )
        edge_types = dict(
            connection.execute(
                "SELECT edge_type, COUNT(*) FROM graph_edges "
                "GROUP BY edge_type ORDER BY edge_type"
            ).fetchall()
        )
        node_storeys = dict(
            connection.execute(
                "SELECT COALESCE(storey_id, '<NULL>'), COUNT(*) "
                "FROM graph_nodes GROUP BY storey_id ORDER BY storey_id"
            ).fetchall()
        )

    payload = json.loads(graph_json.read_text(encoding="utf-8"))
    issue_counts = Counter(issue.issue_type for issue in builder.issues)
    report = {
        "version": 4,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "axis_method": (
                "vector_obstacle_aware_medial_axis_v4"
                if config.prefer_vector_medial_axis
                else "raster_medial_axis"
            ),
            "boundary_sample_spacing_m": config.vector_boundary_sample_spacing_m,
            "branch_pruning_m": config.medial_axis_pruning_length_m,
            "minimum_obstacle_hole_area_m2": (
                config.medial_axis_min_hole_area_m2
            ),
            "node_snap_tolerance_m": config.vector_snap_tolerance_m,
            "minimum_axis_segment_m": config.vector_min_edge_length_m,
            "maximum_component_connector_m": (
                config.vector_max_component_connector_length_m
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
            "axis_methods": axis_methods,
            "edge_types": edge_types,
            "json_nodes": len(payload.get("nodes", [])),
            "json_edges": len(payload.get("edges", payload.get("links", []))),
            "node_storeys": node_storeys,
            "axis_components_remaining": issue_counts[
                "horizontal_axis_components_remaining"
            ],
            "short_horizontal_edges": issue_counts[
                "short_horizontal_edges_remaining"
            ],
        },
    }
    report_path = output_dir / "run_v4_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
