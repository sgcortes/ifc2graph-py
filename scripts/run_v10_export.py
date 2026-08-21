"""Generate and validate HSIMG V10 products."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v10hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".qa/v10_full_run"))
    parser.add_argument("--general-min-width", type=float, default=0.90)
    parser.add_argument("--wheelchair-min-width", type=float, default=1.20)
    parser.add_argument("--dead-end-max-length", type=float, default=1.50)
    parser.add_argument("--accessless-max-length", type=float, default=2.00)
    parser.add_argument("--backbone-max-path", type=float, default=80.0)
    parser.add_argument("--backbone-max-repairs", type=int, default=32)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = HSIMGConfig(
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        dead_end_max_prune_length_m=args.dead_end_max_length,
        accessless_component_max_prune_length_m=args.accessless_max_length,
        clearance_backbone_max_path_length_m=args.backbone_max_path,
        clearance_backbone_max_repairs_per_space=args.backbone_max_repairs,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    nonreciprocal = builder._nonreciprocal_pedestrian_edges()
    cross_space_repairs = [
        (source, target)
        for source, target, data in builder.graph.edges(data=True)
        if data.get("relation_source")
        == "clearance_domain_visibility_backbone_v10"
        and builder.graph.nodes[source].get("parent_node_id")
        != builder.graph.nodes[target].get("parent_node_id")
    ]
    if nonreciprocal or cross_space_repairs:
        raise RuntimeError(
            "V10 graph invariant failed: nonreciprocal or cross-space repair"
        )

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v10_output.gpkg")
    graphml = output_dir / "HSIMG_v10_graph.graphml"
    graph_json = output_dir / "HSIMG_v10_graph.json"
    validation_csv = output_dir / "HSIMG_v10_validation_report.csv"
    builder.export_graph(graphml, graph_json)
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        remaining_non_pedestrian = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE accessible_general=0"
        ).fetchone()[0]
        repair_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='clearance_domain_visibility_backbone_v10'"
        ).fetchone()[0]
        edge_types = dict(connection.execute(
            "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type"
        ).fetchall())
    if integrity != "ok" or remaining_non_pedestrian:
        raise RuntimeError("V10 GeoPackage integrity/pedestrian invariant failed")
    if repair_edges % 2:
        raise RuntimeError("V10 backbone repairs are not bidirectional")

    report = {
        "version": 10,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "general_min_width_m": config.general_min_route_width_m,
            "wheelchair_min_width_m": config.wheelchair_min_route_width_m,
            "bounded_dead_end_pruning_m": config.dead_end_max_prune_length_m,
            "preserve_long_accessless_components": True,
            "accessless_max_prune_length_m": (
                config.accessless_component_max_prune_length_m
            ),
            "clearance_domain_backbone_repair": True,
            "backbone_max_path_length_m": (
                config.clearance_backbone_max_path_length_m
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
            "directed_backbone_repair_edges": repair_edges,
            "surviving_backbone_repairs": repair_edges // 2,
            "backbone_repair_attempts": (
                builder.clearance_backbone_repairs_added
            ),
            "remaining_non_pedestrian_edges": remaining_non_pedestrian,
            "nonreciprocal_pedestrian_edges": len(nonreciprocal),
            "cross_space_backbone_repairs": len(cross_space_repairs),
            "walkable_regions_without_graph": (
                builder.walkable_regions_without_graph
            ),
            "fragmented_walkable_regions": builder.fragmented_walkable_regions,
            "elevator_landings_without_horizontal_reach": (
                builder.elevator_landings_without_horizontal_reach
            ),
        },
    }
    report_path = output_dir / "run_v10_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
