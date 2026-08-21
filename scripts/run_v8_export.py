"""Generate and validate HSIMG V8 products."""

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

from v8hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".qa/v8_full_run"))
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
    parser.add_argument("--shortcut-max-length", type=float, default=12.0)
    parser.add_argument("--shortcut-min-stretch", type=float, default=1.75)
    parser.add_argument("--shortcut-min-saving", type=float, default=3.0)
    parser.add_argument("--shortcut-max-per-space", type=int, default=64)
    parser.add_argument("--disable-shortcuts", action="store_true")
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
        elevator_boundary_opening_tolerance_m=args.elevator_boundary_opening_tolerance,
        elevator_hall_boundary_tolerance_m=args.elevator_hall_boundary_tolerance,
        recover_medial_axis_shortcuts=not args.disable_shortcuts,
        horizontal_shortcut_max_length_m=args.shortcut_max_length,
        horizontal_shortcut_min_stretch_ratio=args.shortcut_min_stretch,
        horizontal_shortcut_min_saving_m=args.shortcut_min_saving,
        horizontal_shortcut_max_per_space=args.shortcut_max_per_space,
        prefer_vector_medial_axis=not args.raster_axis,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    nonreciprocal = builder._nonreciprocal_pedestrian_edges()
    if nonreciprocal:
        raise RuntimeError(
            f"V8 invariant failed: {len(nonreciprocal)} nonreciprocal pedestrian edges"
        )
    cross_space_shortcuts = [
        (source, target)
        for source, target, data in builder.graph.edges(data=True)
        if data.get("relation_source") == "clearance_validated_visibility_shortcut_v8"
        and builder.graph.nodes[source].get("parent_node_id")
        != builder.graph.nodes[target].get("parent_node_id")
    ]
    if cross_space_shortcuts:
        raise RuntimeError(
            f"V8 invariant failed: {len(cross_space_shortcuts)} cross-space shortcuts"
        )

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v8_output.gpkg")
    graphml = output_dir / "HSIMG_v8_graph.graphml"
    graph_json = output_dir / "HSIMG_v8_graph.json"
    builder.export_graph(graphml, graph_json)
    validation_csv = output_dir / "HSIMG_v8_validation_report.csv"
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        remaining_non_pedestrian = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE accessible_general=0"
        ).fetchone()[0]
        shortcut_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='clearance_validated_visibility_shortcut_v8'"
        ).fetchone()[0]
        edge_types = dict(connection.execute(
            "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type"
        ).fetchall())
    if integrity != "ok":
        raise RuntimeError(f"V8 GeoPackage integrity failed: {integrity}")
    if remaining_non_pedestrian:
        raise RuntimeError("V8 invariant failed: non-pedestrian edges remain")
    if shortcut_edges != 2 * builder.horizontal_shortcuts_added:
        raise RuntimeError(
            "V8 invariant failed: exported shortcut count is not bidirectional"
        )

    issue_counts = Counter(issue.issue_type for issue in builder.issues)
    report = {
        "version": 8,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "graph_scope": "general_pedestrian_only",
            "shortcut_method": "clearance_validated_medial_axis_stretch_v8",
            "shortcut_max_length_m": config.horizontal_shortcut_max_length_m,
            "shortcut_min_stretch_ratio": config.horizontal_shortcut_min_stretch_ratio,
            "shortcut_min_saving_m": config.horizontal_shortcut_min_saving_m,
            "shortcut_max_per_space": config.horizontal_shortcut_max_per_space,
            "general_min_width_m": config.general_min_route_width_m,
            "wheelchair_min_width_m": config.wheelchair_min_route_width_m,
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
            "directed_shortcut_edges": shortcut_edges,
            "shortcut_pairs": builder.horizontal_shortcuts_added,
            "spaces_with_shortcuts": builder.horizontal_shortcut_spaces,
            "shortcut_candidates_rejected_clearance": (
                builder.horizontal_shortcut_candidates_rejected_clearance
            ),
            "remaining_non_pedestrian_edges": remaining_non_pedestrian,
            "nonreciprocal_pedestrian_edges": len(nonreciprocal),
            "cross_space_shortcuts": len(cross_space_shortcuts),
            "shortcut_validation_issues": issue_counts["cross_space_shortcut_v8"],
        },
    }
    report_path = output_dir / "run_v8_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
