"""Validate an already generated HSIMG V13 GeoPackage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import geopandas as gpd
import networkx as nx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("geopackage", type=Path)
    args = parser.parse_args()
    gpkg = args.geopackage.resolve()

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        nodes = connection.execute(
            "SELECT node_id, fid FROM graph_nodes"
        ).fetchall()
        edges = connection.execute(
            "SELECT source_id, target_id, relation_source, metadata_json "
            "FROM graph_edges WHERE accessible_general IS NOT 0"
        ).fetchall()
        gates = connection.execute(
            "SELECT d.fid, n.fid, d.door_id, d.name "
            "FROM doors d JOIN graph_nodes n ON n.node_id=d.door_id "
            "WHERE lower(d.name) LIKE '%puerta-elevada-articulada%' "
            "ORDER BY d.fid"
        ).fetchall()
        edge_count = connection.execute(
            "SELECT COUNT(1) FROM graph_edges"
        ).fetchone()[0]

    graph = nx.Graph()
    graph.add_nodes_from(node_id for node_id, _ in nodes)
    graph.add_edges_from((source, target) for source, target, _, _ in edges)
    edge_pairs = {(source, target) for source, target, _, _ in edges}
    nonreciprocal = sorted(
        (source, target)
        for source, target in edge_pairs
        if (target, source) not in edge_pairs
    )
    gate_results = [
        {
            "doors_fid": doors_fid,
            "graph_fid": graph_fid,
            "door_id": door_id,
            "name": name,
            "component_nodes": len(nx.node_connected_component(graph, door_id)),
        }
        for doors_fid, graph_fid, door_id, name in gates
    ]
    isolated = [item for item in gate_results if item["component_nodes"] <= 2]

    bridges = gpd.read_file(
        gpkg,
        layer="graph_edges",
        where="relation_source='same_ifc_space_visibility_bridge_v13'",
    )
    spaces = gpd.read_file(gpkg, layer="spaces").set_index("space_id")
    outside = []
    wrong_space = []
    for _, edge in bridges.iterrows():
        metadata = json.loads(edge["metadata_json"] or "{}")
        space_id = metadata.get("space_id")
        if space_id not in spaces.index:
            wrong_space.append(edge["edge_id"])
            continue
        if not spaces.loc[space_id].geometry.buffer(0.02).covers(edge.geometry):
            outside.append(edge["edge_id"])

    requested = {
        item["doors_fid"]: item
        for item in gate_results
        if 1332 <= item["doors_fid"] <= 1338
    }
    missing_requested = sorted(set(range(1332, 1339)) - set(requested))
    errors = {
        "integrity": integrity != "ok",
        "nonreciprocal": len(nonreciprocal),
        "isolated_articulated_gates": len(isolated),
        "missing_requested_gates": missing_requested,
        "bridges_outside_ifc_space": len(outside),
        "bridges_without_ifc_space": len(wrong_space),
        "odd_directed_bridge_count": len(bridges) % 2,
    }
    if any(value for value in errors.values()):
        raise RuntimeError(f"V13 release validation failed: {errors}")

    report = {
        "version": 13,
        "geopackage": str(gpkg),
        "integrity_check": integrity,
        "graph_nodes": len(nodes),
        "graph_edges": edge_count,
        "directed_finalist_space_bridges": len(bridges),
        "nonreciprocal_pedestrian_edges": len(nonreciprocal),
        "articulated_gates_checked": len(gate_results),
        "isolated_articulated_gates": len(isolated),
        "requested_level_minus_1_gates": [requested[fid] for fid in sorted(requested)],
        "bridges_outside_ifc_space": len(outside),
        "status": "passed",
    }
    report_path = gpkg.with_name("v13_release_validation.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
