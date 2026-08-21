"""Generate and validate HSIMG V9 products."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v9hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".qa/v9_full_run"))
    parser.add_argument("--general-min-width", type=float, default=0.90)
    parser.add_argument("--wheelchair-min-width", type=float, default=1.20)
    parser.add_argument("--shortcut-max-length", type=float, default=12.0)
    parser.add_argument("--component-bridge-max-length", type=float, default=12.0)
    parser.add_argument("--component-bridge-max-per-space", type=int, default=32)
    parser.add_argument("--open-space-min-width", type=float, default=0.90)
    parser.add_argument("--open-space-max-connector", type=float, default=8.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = HSIMGConfig(
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        horizontal_shortcut_max_length_m=args.shortcut_max_length,
        horizontal_component_bridge_max_length_m=(
            args.component_bridge_max_length
        ),
        horizontal_component_bridge_max_per_space=(
            args.component_bridge_max_per_space
        ),
        open_space_min_boundary_width_m=args.open_space_min_width,
        open_space_max_connector_length_m=args.open_space_max_connector,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    nonreciprocal = builder._nonreciprocal_pedestrian_edges()
    cross_space_bridges = [
        (source, target)
        for source, target, data in builder.graph.edges(data=True)
        if data.get("relation_source") == "same_space_clearance_component_bridge_v9"
        and builder.graph.nodes[source].get("parent_node_id")
        != builder.graph.nodes[target].get("parent_node_id")
    ]
    if nonreciprocal or cross_space_bridges:
        raise RuntimeError(
            "V9 graph invariant failed: nonreciprocal or cross-space component bridge"
        )

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v9_output.gpkg")
    graphml = output_dir / "HSIMG_v9_graph.graphml"
    graph_json = output_dir / "HSIMG_v9_graph.json"
    validation_csv = output_dir / "HSIMG_v9_validation_report.csv"
    builder.export_graph(graphml, graph_json)
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        remaining_non_pedestrian = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE accessible_general=0"
        ).fetchone()[0]
        component_bridge_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='same_space_clearance_component_bridge_v9'"
        ).fetchone()[0]
        open_space_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='IfcSpace_wall_free_shared_boundary_v9'"
        ).fetchone()[0]
        edge_types = dict(connection.execute(
            "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type"
        ).fetchall())
    if integrity != "ok" or remaining_non_pedestrian:
        raise RuntimeError("V9 GeoPackage integrity/pedestrian invariant failed")
    if component_bridge_edges != 2 * builder.horizontal_component_bridges_added:
        raise RuntimeError("V9 component bridges are not bidirectional")
    if open_space_edges != 2 * builder.open_space_transitions_added:
        raise RuntimeError("V9 open-space transitions are not bidirectional")

    report = {
        "version": 9,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "same_space_component_bridge": True,
            "component_bridge_max_length_m": (
                config.horizontal_component_bridge_max_length_m
            ),
            "wall_free_open_space_boundaries": True,
            "open_space_min_boundary_width_m": (
                config.open_space_min_boundary_width_m
            ),
            "shared_ifc_wall_is_hard_rejection": True,
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
            "directed_component_bridge_edges": component_bridge_edges,
            "directed_open_space_edges": open_space_edges,
            "remaining_non_pedestrian_edges": remaining_non_pedestrian,
            "nonreciprocal_pedestrian_edges": len(nonreciprocal),
            "cross_space_component_bridges": len(cross_space_bridges),
            "elevator_landings_without_horizontal_reach": (
                builder.elevator_landings_without_horizontal_reach
            ),
        },
    }
    report_path = output_dir / "run_v9_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
