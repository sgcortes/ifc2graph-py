"""Generate and validate HSIMG V13 products."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

import networkx as nx
from shapely.geometry import LineString


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v13hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".qa/v13_full_run"))
    parser.add_argument("--general-min-width", type=float, default=0.90)
    parser.add_argument("--wheelchair-min-width", type=float, default=1.20)
    parser.add_argument("--general-min-door-width", type=float, default=0.60)
    parser.add_argument("--finalist-bridge-max-length", type=float, default=20.0)
    return parser.parse_args()


def pedestrian_graph(builder: HSIMGBuilder) -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(builder.graph.nodes)
    graph.add_edges_from(
        (source, target)
        for source, target, data in builder.graph.edges(data=True)
        if data.get("accessible_general") is not False
    )
    return graph


def validate_garage_gates(builder: HSIMGBuilder) -> list[dict[str, object]]:
    graph = pedestrian_graph(builder)
    results = []
    for door in builder.doors.values():
        label = f"{door.name or ''}".casefold()
        if "puerta-elevada-articulada" not in label:
            continue
        component = nx.node_connected_component(graph, door.door_id)
        spaces = [
            builder.spaces[space_id].name
            for space_id in door.connected_space_ids
            if space_id in builder.spaces
        ]
        result = {
            "door_id": door.door_id,
            "ifc_guid": door.ifc_guid,
            "name": door.name,
            "connected_spaces": spaces,
            "component_nodes": len(component),
            # Horizontal-mobility spaces route through their internal axes;
            # their semantic parent node is intentionally not an endpoint.
            # A two-node component is precisely the isolated door+side defect.
            "interior_reach": len(component) > 2 and any(
                builder.graph.nodes[node_id].get("node_type")
                in {"space", "internal_mobility", "vertical_mobility"}
                for node_id in component
            ),
        }
        results.append(result)
    failed = [item for item in results if not item["interior_reach"]]
    if failed:
        raise RuntimeError(f"V13 garage gates without interior reach: {failed}")
    return results


def validate_bridges(builder: HSIMGBuilder) -> int:
    directed = 0
    for source, target, data in builder.graph.edges(data=True):
        if data.get("relation_source") != "same_ifc_space_visibility_bridge_v13":
            continue
        directed += 1
        metadata = data.get("metadata", {})
        if not metadata and data.get("metadata_json"):
            metadata = json.loads(data["metadata_json"])
        space_id = metadata.get("space_id") or data.get("space_id")
        space = builder.spaces.get(space_id)
        geometry = data.get("geometry")
        if space is None or geometry is None:
            raise RuntimeError("V13 bridge lacks its explicit parent space")
        line_2d = LineString([(x, y) for x, y, *_ in geometry.coords])
        if not space.footprint.buffer(
            builder.config.clearance_domain_tolerance_m
        ).covers(line_2d):
            raise RuntimeError("V13 finalist bridge leaves its IFC space")
    if directed % 2:
        raise RuntimeError("V13 finalist bridges are not bidirectional")
    return directed


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = HSIMGConfig(
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        general_min_door_width_m=args.general_min_door_width,
        finalist_door_bridge_max_length_m=args.finalist_bridge_max_length,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    nonreciprocal = builder._nonreciprocal_pedestrian_edges()
    if nonreciprocal:
        raise RuntimeError(f"V13 nonreciprocal pedestrian edges: {nonreciprocal}")
    directed_bridges = validate_bridges(builder)
    gates = validate_garage_gates(builder)

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v13_output.gpkg")
    graphml = output_dir / "HSIMG_v13_graph.graphml"
    graph_json = output_dir / "HSIMG_v13_graph.json"
    validation_csv = output_dir / "HSIMG_v13_validation_report.csv"
    builder.export_graph(graphml, graph_json)
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        node_fids = dict(connection.execute(
            "SELECT node_id, fid FROM graph_nodes"
        ).fetchall())
        layer_fids = dict(connection.execute(
            "SELECT door_id, fid FROM doors"
        ).fetchall())
        edge_types = dict(connection.execute(
            "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type"
        ).fetchall())
    if integrity != "ok":
        raise RuntimeError(f"V13 GeoPackage integrity failed: {integrity}")
    for gate in gates:
        gate["graph_fid"] = node_fids.get(gate["door_id"])
        gate["doors_fid"] = layer_fids.get(gate["door_id"])

    report = {
        "version": 13,
        "source_ifc": str(args.ifc.resolve()),
        "outputs": {
            "geopackage": str(gpkg),
            "graph_json": str(graph_json),
            "graphml": str(graphml),
            "validation_csv": str(validation_csv),
        },
        "summary": builder.summary(),
        "validation": {
            "gpkg_integrity": integrity,
            "nonreciprocal_pedestrian_edges": len(nonreciprocal),
            "directed_finalist_door_bridges": directed_bridges,
            "garage_gates_checked": len(gates),
            "garage_gates_without_interior_reach": 0,
            "garage_gates": gates,
            "edge_types": edge_types,
        },
    }
    report_path = output_dir / "run_v13_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
