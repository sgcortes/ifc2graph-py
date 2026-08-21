"""Generate pedestrian-pruned V6 HSIMG products from an IFC model."""

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

from v6hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path, help="Input IFC2X3 or IFC4 model")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".qa/v6_full_run"),
        help="Directory for V6 GeoPackage, graph and validation products",
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
        help="Minimum route width retained in the exported graph (m)",
    )
    parser.add_argument(
        "--wheelchair-min-width",
        type=float,
        default=1.20,
        help="Minimum width marked wheelchair-accessible (m)",
    )
    parser.add_argument(
        "--clearance-sample-spacing",
        type=float,
        default=0.10,
        help="Maximum spacing between clearance samples (m)",
    )
    parser.add_argument(
        "--clearance-domain-tolerance",
        type=float,
        default=0.01,
        help="Numerical tolerance for eroded-domain coverage (m)",
    )
    parser.add_argument(
        "--stair-landing-max-connector",
        type=float,
        default=6.00,
        help="Maximum gap joined between flights of one IfcStair (m)",
    )
    parser.add_argument(
        "--stair-system-max-transition",
        type=float,
        default=3.00,
        help="Maximum shared-landing gap between consecutive IfcStair objects (m)",
    )
    parser.add_argument(
        "--raster-axis",
        action="store_true",
        help="Use the legacy raster skeleton before V6 clearance pruning",
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
        clearance_domain_tolerance_m=args.clearance_domain_tolerance,
        stair_landing_max_connector_length_m=(
            args.stair_landing_max_connector
        ),
        stair_system_max_transition_length_m=(
            args.stair_system_max_transition
        ),
        prefer_vector_medial_axis=not args.raster_axis,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()
    nonreciprocal_pedestrian = builder._nonreciprocal_pedestrian_edges()
    if nonreciprocal_pedestrian:
        preview = ", ".join(
            f"{source}->{target} ({edge_type})"
            for source, target, edge_type in nonreciprocal_pedestrian[:5]
        )
        raise RuntimeError(
            "V6 export invariant failed: nonreciprocal pedestrian edges "
            f"remain ({len(nonreciprocal_pedestrian)}): {preview}"
        )
    fragmented_stairs = builder._fragmented_stair_subgraphs()
    if fragmented_stairs:
        raise RuntimeError(
            "V6 export invariant failed: fragmented stair subgraphs remain "
            f"({len(fragmented_stairs)}): {fragmented_stairs[:5]}"
        )

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v6_output.gpkg")
    graphml = output_dir / "HSIMG_v6_graph.graphml"
    graph_json = output_dir / "HSIMG_v6_graph.json"
    builder.export_graph(graphml, graph_json)
    validation_csv = output_dir / "HSIMG_v6_validation_report.csv"
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
        remaining_non_pedestrian = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE accessible_general=0"
        ).fetchone()[0]
        remaining_wheelchair_restricted = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE accessible_general=1 AND accessible_wheelchair=0"
        ).fetchone()[0]

    if remaining_non_pedestrian:
        raise RuntimeError(
            "V6 export invariant failed: non-pedestrian edges remain in graph_edges"
        )

    payload = json.loads(graph_json.read_text(encoding="utf-8"))
    issue_counts = Counter(issue.issue_type for issue in builder.issues)
    report = {
        "version": 6,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "axis_method": (
                "vector_obstacle_aware_v4_plus_pedestrian_pruning_v6"
                if config.prefer_vector_medial_axis
                else "raster_axis_plus_pedestrian_pruning_v6"
            ),
            "graph_scope": "general_pedestrian_only",
            "general_min_route_width_m": config.general_min_route_width_m,
            "wheelchair_min_route_width_m": config.wheelchair_min_route_width_m,
            "clearance_sample_spacing_m": config.route_width_sample_spacing_m,
            "clearance_domain_tolerance_m": (
                config.clearance_domain_tolerance_m
            ),
            "stair_landing_max_connector_length_m": (
                config.stair_landing_max_connector_length_m
            ),
            "stair_system_max_transition_length_m": (
                config.stair_system_max_transition_length_m
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
            "remaining_non_pedestrian_edges": remaining_non_pedestrian,
            "remaining_wheelchair_restricted_edges": (
                remaining_wheelchair_restricted
            ),
            "json_nodes": len(payload.get("nodes", [])),
            "json_edges": len(payload.get("edges", payload.get("links", []))),
            "pruned_route_issue_spaces": issue_counts[
                "narrow_general_routes_pruned_v6"
            ],
            "pruning_invariant_errors": issue_counts[
                "non_pedestrian_edges_remain_in_v6"
            ],
            "nonreciprocal_pedestrian_edges": len(
                nonreciprocal_pedestrian
            ),
            "reciprocity_invariant_errors": issue_counts[
                "nonreciprocal_pedestrian_edge_v6"
            ],
            "fragmented_stair_subgraphs": len(fragmented_stairs),
            "fragmented_stair_invariant_errors": issue_counts[
                "fragmented_stair_subgraph_v6"
            ],
            "stair_terminals_without_space_access": len(
                builder._stairs_missing_terminal_access()
            ),
            "inter_storey_stair_transitions_added": (
                builder.stair_system_transitions_added
            ),
        },
    }
    report_path = output_dir / "run_v6_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
