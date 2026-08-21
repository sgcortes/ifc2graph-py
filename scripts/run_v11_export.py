"""Generate and validate HSIMG V11 products."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v11hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".qa/v11_full_run"))
    parser.add_argument("--general-min-width", type=float, default=0.90)
    parser.add_argument("--wheelchair-min-width", type=float, default=1.20)
    parser.add_argument("--door-throat-max-length", type=float, default=2.0)
    parser.add_argument("--door-safe-connector-max-length", type=float, default=8.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = HSIMGConfig(
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        door_approach_max_throat_length_m=args.door_throat_max_length,
        door_approach_max_safe_connector_length_m=(
            args.door_safe_connector_max_length
        ),
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    nonreciprocal = builder._nonreciprocal_pedestrian_edges()
    cross_space = [
        (source, target)
        for source, target, data in builder.graph.edges(data=True)
        if data.get("relation_source") in {
            "door_width_validated_throat_v11",
            "door_approach_safe_connector_v11",
        }
        and builder.graph.nodes[source].get("parent_node_id")
        != builder.graph.nodes[target].get("parent_node_id")
    ]
    if nonreciprocal or cross_space:
        raise RuntimeError(
            "V11 invariant failed: nonreciprocal or cross-space door approach"
        )

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v11_output.gpkg")
    graphml = output_dir / "HSIMG_v11_graph.graphml"
    graph_json = output_dir / "HSIMG_v11_graph.json"
    validation_csv = output_dir / "HSIMG_v11_validation_report.csv"
    builder.export_graph(graphml, graph_json)
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        remaining_non_pedestrian = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE accessible_general=0"
        ).fetchone()[0]
        throat_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='door_width_validated_throat_v11'"
        ).fetchone()[0]
        safe_connector_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='door_approach_safe_connector_v11'"
        ).fetchone()[0]
        edge_types = dict(connection.execute(
            "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type"
        ).fetchall())
    if integrity != "ok" or remaining_non_pedestrian:
        raise RuntimeError("V11 GeoPackage integrity/pedestrian invariant failed")
    if throat_edges % 2 or safe_connector_edges % 2:
        raise RuntimeError("V11 door approaches are not bidirectional")

    report = {
        "version": 11,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "general_min_width_m": config.general_min_route_width_m,
            "wheelchair_min_width_m": config.wheelchair_min_route_width_m,
            "door_throat_width_basis": "actual_ifc_door_width",
            "door_throat_max_length_m": (
                config.door_approach_max_throat_length_m
            ),
            "safe_connector_max_length_m": (
                config.door_approach_max_safe_connector_length_m
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
            "directed_door_throat_edges": throat_edges,
            "directed_door_safe_connector_edges": safe_connector_edges,
            "remaining_non_pedestrian_edges": remaining_non_pedestrian,
            "nonreciprocal_pedestrian_edges": len(nonreciprocal),
            "cross_space_door_approaches": len(cross_space),
            "door_projections_without_axis_reach": (
                builder.door_projections_without_axis_reach
            ),
            "exterior_entrances_without_interior_reach": (
                builder.exterior_entrances_without_interior_reach
            ),
        },
    }
    report_path = output_dir / "run_v11_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
