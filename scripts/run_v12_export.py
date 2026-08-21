"""Generate and validate HSIMG V12 products."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

import networkx as nx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v12hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".qa/v12_full_run"))
    parser.add_argument("--general-min-width", type=float, default=0.90)
    parser.add_argument("--wheelchair-min-width", type=float, default=1.20)
    parser.add_argument("--general-min-door-width", type=float, default=0.60)
    parser.add_argument("--door-throat-max-length", type=float, default=2.0)
    parser.add_argument("--door-safe-connector-max-length", type=float, default=8.0)
    parser.add_argument("--route-origin-fid", type=int)
    parser.add_argument("--route-destination-fid", type=int)
    return parser.parse_args()


def accessible_graph(builder: HSIMGBuilder) -> nx.Graph:
    graph = nx.Graph()
    for source, target, data in builder.graph.edges(data=True):
        if data.get("accessible_general") is not False:
            graph.add_edge(source, target)
    return graph


def endpoint_nodes(builder: HSIMGBuilder, space_id: str) -> list[str]:
    space = builder.spaces[space_id]
    if space.node_class != "horizontal_mobility":
        return [space_id]
    _, children = builder._horizontal_space_graph(space_id)
    return children


def validate_requested_route(
    builder: HSIMGBuilder,
    origin_fid: int | None,
    destination_fid: int | None,
) -> dict[str, object] | None:
    if origin_fid is None and destination_fid is None:
        return None
    if origin_fid is None or destination_fid is None:
        raise RuntimeError("Both route FIDs must be provided")
    by_fid = {
        index: space_id
        for index, space_id in enumerate(builder.spaces, start=1)
    }
    if origin_fid not in by_fid or destination_fid not in by_fid:
        raise RuntimeError("Requested route FID is not present in the spaces layer")
    origin_id = by_fid[origin_fid]
    destination_id = by_fid[destination_fid]
    graph = accessible_graph(builder)
    origins = [node_id for node_id in endpoint_nodes(builder, origin_id) if node_id in graph]
    destinations = [
        node_id
        for node_id in endpoint_nodes(builder, destination_id)
        if node_id in graph
    ]
    route_exists = any(
        nx.has_path(graph, source, target)
        for source in origins
        for target in destinations
    )
    result = {
        "origin_fid": origin_fid,
        "origin_space_id": origin_id,
        "destination_fid": destination_fid,
        "destination_space_id": destination_id,
        "route_exists": route_exists,
    }
    if not route_exists:
        raise RuntimeError(f"Requested V12 route failed: {result}")
    return result


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = HSIMGConfig(
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        general_min_door_width_m=args.general_min_door_width,
        door_approach_max_throat_length_m=args.door_throat_max_length,
        door_approach_max_safe_connector_length_m=(
            args.door_safe_connector_max_length
        ),
        door_axis_segment_max_connector_length_m=(
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
            "nearest_safe_axis_segment_v12",
            "door_width_validated_throat_v11",
            "door_approach_safe_connector_v11",
        }
        and builder.graph.nodes[source].get("parent_node_id")
        != builder.graph.nodes[target].get("parent_node_id")
        and data.get("edge_type") != "door_throat_transition"
    ]
    if nonreciprocal or cross_space:
        raise RuntimeError(
            "V12 invariant failed: nonreciprocal or cross-space axis repair"
        )

    route_validation = validate_requested_route(
        builder,
        args.route_origin_fid,
        args.route_destination_fid,
    )

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v12_output.gpkg")
    graphml = output_dir / "HSIMG_v12_graph.graphml"
    graph_json = output_dir / "HSIMG_v12_graph.json"
    validation_csv = output_dir / "HSIMG_v12_validation_report.csv"
    builder.export_graph(graphml, graph_json)
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        remaining_non_pedestrian = connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE accessible_general=0"
        ).fetchone()[0]
        segment_edges = connection.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='nearest_safe_axis_segment_v12'"
        ).fetchone()[0]
        edge_types = dict(connection.execute(
            "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type"
        ).fetchall())
    if integrity != "ok" or remaining_non_pedestrian:
        raise RuntimeError("V12 GeoPackage integrity/pedestrian invariant failed")
    if segment_edges % 2:
        raise RuntimeError("V12 axis segment attachments are not bidirectional")

    report = {
        "version": 12,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "general_min_width_m": config.general_min_route_width_m,
            "wheelchair_min_width_m": config.wheelchair_min_route_width_m,
            "general_min_door_width_m": config.general_min_door_width_m,
            "door_throat_max_length_m": (
                config.door_approach_max_throat_length_m
            ),
            "safe_connector_max_length_m": (
                config.door_axis_segment_max_connector_length_m
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
            "directed_axis_segment_edges": segment_edges,
            "remaining_non_pedestrian_edges": remaining_non_pedestrian,
            "nonreciprocal_pedestrian_edges": len(nonreciprocal),
            "cross_space_axis_repairs": len(cross_space),
            "door_projections_without_axis_reach": (
                builder.door_projections_without_axis_reach
            ),
            "horizontal_spaces_without_vertical_reach": (
                builder.horizontal_spaces_without_network_reach
            ),
            "requested_route": route_validation,
        },
    }
    report_path = output_dir / "run_v12_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
