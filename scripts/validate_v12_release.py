"""Independent topology checks for an exported HSIMG V12 GeoPackage."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

import networkx as nx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("geopackage", type=Path)
    parser.add_argument("--origin-fid", type=int, required=True)
    parser.add_argument("--destination-fid", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = args.geopackage.resolve()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    spaces = {
        int(row["fid"]): row["space_id"]
        for row in cursor.execute("SELECT fid, space_id FROM spaces")
    }
    origin = spaces[args.origin_fid]
    destination = spaces[args.destination_fid]

    graph = nx.DiGraph()
    for row in cursor.execute(
        "SELECT source_id, target_id, length_3d, mobility_mode, edge_type, "
        "relation_source FROM graph_edges WHERE accessible_general=1"
    ):
        graph.add_edge(
            row["source_id"],
            row["target_id"],
            weight=float(row["length_3d"] or 0.0),
            mobility_mode=row["mobility_mode"],
            edge_type=row["edge_type"],
            relation_source=row["relation_source"],
        )
    undirected = graph.to_undirected()
    origin_nodes = [
        row["node_id"]
        for row in cursor.execute(
            "SELECT node_id FROM graph_nodes WHERE parent_node_id=? "
            "AND node_type='internal_mobility' AND mobility_type='horizontal'",
            (origin,),
        )
        if row["node_id"] in graph
    ]
    if not origin_nodes:
        origin_nodes = [origin]
    destination_nodes = [destination]
    candidates = []
    for source in origin_nodes:
        for target in destination_nodes:
            if source in graph and target in graph and nx.has_path(graph, source, target):
                route = nx.shortest_path(graph, source, target, weight="weight")
                candidates.append((nx.path_weight(graph, route, weight="weight"), route))
    if not candidates:
        raise RuntimeError("The requested V12 route does not exist")
    route_length, route = min(candidates, key=lambda item: item[0])
    route_edges = [graph[source][target] for source, target in zip(route, route[1:])]
    storeys = []
    for node_id in route:
        row = cursor.execute(
            "SELECT storey_id FROM graph_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        if row and row["storey_id"] not in storeys:
            storeys.append(row["storey_id"])

    signatures = {
        (
            row["source_id"],
            row["target_id"],
            row["edge_type"],
            row["relation_source"] or "",
        )
        for row in cursor.execute(
            "SELECT source_id, target_id, edge_type, relation_source "
            "FROM graph_edges"
        )
    }
    nonreciprocal = sum(
        (target, source, edge_type, relation_source) not in signatures
        for source, target, edge_type, relation_source in signatures
    )
    cross_space = cursor.execute(
        "SELECT COUNT(*) FROM graph_edges e "
        "JOIN graph_nodes a ON a.node_id=e.source_id "
        "JOIN graph_nodes b ON b.node_id=e.target_id "
        "WHERE e.relation_source='nearest_safe_axis_segment_v12' "
        "AND coalesce(a.parent_node_id,'')<>coalesce(b.parent_node_id,'')"
    ).fetchone()[0]
    door_id = "door_c2f1ba9a1c6ece2b"
    projection_id = "projection_981259fee95766b7"
    destination_component = nx.node_connected_component(undirected, destination)
    report = {
        "file": str(path),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "integrity": cursor.execute("PRAGMA integrity_check").fetchone()[0],
        "nodes": cursor.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0],
        "edges": cursor.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0],
        "route": {
            "origin_fid": args.origin_fid,
            "destination_fid": args.destination_fid,
            "exists": True,
            "length_m": round(float(route_length), 3),
            "node_count": len(route),
            "storeys": storeys,
            "vertical_modes": sorted({
                edge["mobility_mode"]
                for edge in route_edges
                if edge["mobility_mode"] in {"elevator", "stair", "stairs", "ramp"}
            }),
            "uses_corrected_door_195279": door_id in route,
            "uses_corrected_projection": projection_id in route,
        },
        "destination_component_size": len(destination_component),
        "corrected_door_component_size": (
            len(nx.node_connected_component(undirected, door_id))
            if door_id in undirected else 1
        ),
        "v12_directed_repairs": cursor.execute(
            "SELECT COUNT(*) FROM graph_edges "
            "WHERE relation_source='nearest_safe_axis_segment_v12'"
        ).fetchone()[0],
        "nonreciprocal_edges": nonreciprocal,
        "cross_space_v12_repairs": cross_space,
    }
    if report["integrity"] != "ok" or nonreciprocal or cross_space:
        raise RuntimeError(json.dumps(report, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
