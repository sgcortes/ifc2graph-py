"""Reduce horizontal axes without changing shortest routes between real accesses.

The reduction runs AFTER all door and vertical attachments. It never invents a
straight shortcut: retained edges keep their geometry and degree-two vertices
are concatenated into polylines. This is a routing abstraction, not a graph for
counting alternative evacuation routes.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

import networkx as nx
from shapely.geometry import LineString

from hsimg import stable_id

AXIS_TYPES = {"internal_axis", "component_connector"}
SCENARIOS = (
    ("general_length", "accessible_general", "length_3d"),
    ("general_cost", "accessible_general", "cost_general"),
    ("wheelchair_length", "accessible_wheelchair", "length_3d"),
    ("wheelchair_cost", "accessible_wheelchair", "cost_wheelchair"),
)


@dataclass
class CleanupResult:
    summary: dict[str, Any]
    spaces: list[dict[str, Any]]
    audit: list[dict[str, Any]]


def metadata(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("metadata_json", "{}")
    try:
        return json.loads(value) if isinstance(value, str) else dict(value)
    except (ValueError, TypeError):
        return {}


def functional_terminals(graph: nx.MultiDiGraph, nodes: set[str]) -> set[str]:
    """Protect actual attachments, never a repair's provenance alone."""
    protected = {
        node for node in nodes
        if graph.nodes[node].get("node_role") in {
            "door_projection", "door_side", "elevator_opening_hall_side", "landing",
        } or graph.nodes[node].get("node_type") != "internal_mobility"
        or graph.nodes[node].get("is_destination") is True
    }
    for source, target, data in graph.edges(data=True):
        if data.get("edge_type") not in AXIS_TYPES:
            protected.update({source, target} & nodes)
        elif (source in nodes) != (target in nodes):
            protected.update({source, target} & nodes)
    return protected


def route_graph(graph: nx.MultiDiGraph, nodes: set[str], flag: str, weight: str) -> nx.DiGraph:
    """Stable edge order also makes equal-cost route selection reproducible."""
    result = nx.DiGraph()
    result.add_nodes_from(sorted(nodes))
    for u, v, key, data in sorted(graph.subgraph(nodes).edges(keys=True, data=True),
                                  key=lambda item: (item[0], item[1], str(item[2]))):
        if data.get("edge_type") not in AXIS_TYPES:
            continue
        if (flag == "accessible_general" and data.get(flag) is False) or (
            flag == "accessible_wheelchair" and data.get(flag) is not True
        ):
            continue
        cost = float(data.get(weight, data.get("length_3d", 0.0)))
        if not math.isfinite(cost) or cost <= 0:
            continue
        current = result.get_edge_data(u, v)
        if current is None or cost < current["weight"]:
            result.add_edge(u, v, weight=cost, original_key=key)
    return result


def terminal_distances(local: nx.DiGraph, terminals: set[str]) -> dict[tuple[str, str], float]:
    result = {}
    for source in sorted(terminals):
        distances = nx.single_source_dijkstra_path_length(local, source)
        for target in sorted(terminals - {source}):
            result[source, target] = distances.get(target, math.inf)
    return result


def _oriented_coordinates(graph: nx.MultiDiGraph, source: str, data: dict[str, Any]):
    line = data.get("geometry")
    if not isinstance(line, LineString) or line.is_empty:
        return None
    coordinates = [tuple(c) for c in line.coords]
    node = graph.nodes[source]
    origin = (node["x"], node["y"], node.get("z", 0.0))
    def distance(point):
        return math.dist(origin, (*point[:2], point[2] if len(point) > 2 else 0.0))
    if distance(coordinates[-1]) < distance(coordinates[0]):
        coordinates.reverse()
    return coordinates


def _contract_degree_two(graph: nx.MultiDiGraph, nodes: set[str], protected: set[str], audit: list) -> int:
    count = 0
    candidates = list(sorted(nodes - protected))
    while candidates:
        middle = candidates.pop()
        if middle not in graph or middle in protected:
            continue
        neighbors = set(graph.predecessors(middle)) | set(graph.successors(middle))
        if len(neighbors) != 2 or middle in neighbors or not neighbors <= nodes:
            continue
        left, right = sorted(neighbors)
        # Keep parallel alternatives and directional/restriction boundaries explicit.
        if graph.has_edge(left, right) or graph.has_edge(right, left):
            continue
        directions = ((left, middle), (middle, right), (right, middle), (middle, left))
        if any(graph.number_of_edges(u, v) != 1 for u, v in directions):
            continue
        edges = [next(iter(graph[u][v].values())) for u, v in directions]
        signature = lambda d: (d.get("subgraph_id"), d.get("mobility_mode"),
                               d.get("accessible_general"), d.get("accessible_wheelchair"),
                               d.get("restriction_reason"))
        if any(d.get("edge_type") not in AXIS_TYPES for d in edges):
            continue
        if len({signature(d) for d in edges}) != 1:
            continue
        replacements = []
        for source, target, first, second in (
            (left, right, edges[0], edges[1]), (right, left, edges[2], edges[3]),
        ):
            one = _oriented_coordinates(graph, source, first)
            two = _oriented_coordinates(graph, middle, second)
            if one is None or two is None or math.dist(one[-1], two[0]) > 1e-6:
                break
            # The exact original vertices, including bends around obstacles, survive.
            line = LineString(one + two[1:])
            first_meta, second_meta = metadata(first), metadata(second)
            original_ids = (first_meta.get("source_edge_ids", [first["edge_id"]])
                            + second_meta.get("source_edge_ids", [second["edge_id"]]))
            edge_id = stable_id("axis_chain_v14", source, target, *original_ids)
            data = dict(first)
            data.update(edge_id=edge_id, source=source, target=target,
                        edge_type="internal_axis", geometry=line, geometry_wkt=line.wkt,
                        relation_source="concatenated_axis_chain_v14",
                        validation_status="geometry_and_route_preserved_v14")
            for attribute in ("length_3d", "horizontal_length", "estimated_time",
                              "effort_cost", "cost_general", "cost_wheelchair"):
                if attribute in first and attribute in second:
                    data[attribute] = float(first[attribute]) + float(second[attribute])
            coordinates = list(line.coords)
            data["vertical_displacement"] = coordinates[-1][2] - coordinates[0][2] if line.has_z else 0.0
            data["confidence"] = min(first.get("confidence", 1), second.get("confidence", 1))
            widths = [m["minimum_route_width_m"] for m in (first_meta, second_meta)
                      if m.get("minimum_route_width_m") is not None]
            data["metadata_json"] = json.dumps({
                "method": "exact_polyline_concatenation_v14", "source_edge_ids": original_ids,
                "minimum_route_width_m": min(widths) if len(widths) == 2 else None,
                "contracted_node_ids": first_meta.get("contracted_node_ids", []) + [middle]
                                       + second_meta.get("contracted_node_ids", []),
            }, sort_keys=True)
            replacements.append((source, target, edge_id, data))
        if len(replacements) != 2:
            continue
        audit.append({"action": "contract_degree_two", "node_id": middle,
                      "subgraph_id": edges[0].get("subgraph_id"),
                      "replacement_edges": [row[2] for row in replacements]})
        graph.remove_node(middle)
        nodes.discard(middle)
        for source, target, key, data in replacements:
            graph.add_edge(source, target, key=key, **data)
        count += 1
        candidates.extend(n for n in (left, right) if n not in protected)
    return count


def simplify_horizontal_graph(graph: nx.MultiDiGraph) -> CleanupResult:
    """Keep the union of terminal shortest paths for both supported profiles.

    Both length and the configured profile cost are protected. Sparse components
    with fewer than two terminals are retained for diagnosis. Every ordered pair
    of terminals is rechecked after pruning AND polyline contraction; failures
    abort the caller before an export can be published.
    """
    groups: dict[str, set[str]] = {}
    for node, data in graph.nodes(data=True):
        if data.get("mobility_type") == "horizontal" and data.get("subgraph_id"):
            groups.setdefault(data["subgraph_id"], set()).add(node)
    initial_nodes, initial_edges = graph.number_of_nodes(), graph.number_of_edges()
    audit, reports = [], []
    total_checked = total_contracted = total_pruned_nodes = total_removed_edges = 0
    max_error = 0.0
    for subgraph_id, nodes in sorted(groups.items()):
        protected = functional_terminals(graph, nodes)
        axis = nx.Graph()
        axis.add_nodes_from(nodes)
        axis.add_edges_from((u, v) for u, v, d in graph.subgraph(nodes).edges(data=True)
                            if d.get("edge_type") in AXIS_TYPES)
        keep = set()
        sparse_components = 0
        for component in nx.connected_components(axis):
            if len(component & protected) < 2:
                sparse_components += int(len(component) > 1)
                keep.update((u, v, k) for u, v, k in graph.subgraph(component).edges(keys=True))
        before = {}
        for name, flag, weight in SCENARIOS:
            local = route_graph(graph, nodes, flag, weight)
            before[name] = terminal_distances(local, protected)
            for source in sorted(protected):
                _, paths = nx.single_source_dijkstra(local, source)
                for target in sorted(protected - {source}):
                    path = paths.get(target, [])
                    for u, v in zip(path, path[1:]):
                        keep.add((u, v, local[u][v]["original_key"]))
        # Preserve exact reverse representations of retained physical connections.
        for u, v, key in tuple(keep):
            selected = graph[u][v][key]
            for reverse_key, reverse in sorted(graph.get_edge_data(v, u, default={}).items()):
                if (selected.get("edge_type") == reverse.get("edge_type")
                    and selected.get("accessible_general") == reverse.get("accessible_general")
                    and selected.get("accessible_wheelchair") == reverse.get("accessible_wheelchair")
                    and selected.get("geometry") is not None
                    and selected["geometry"].equals(reverse.get("geometry"))):
                    keep.add((v, u, reverse_key))
                    break
        removed_edges = 0
        for u, v, key, data in list(graph.subgraph(nodes).edges(keys=True, data=True)):
            if data.get("edge_type") not in AXIS_TYPES or (u, v, key) in keep:
                continue
            audit.append({"action": "remove_unused_axis", "subgraph_id": subgraph_id,
                          "edge_id": data.get("edge_id", key), "source": u, "target": v,
                          "length_m": data.get("length_3d"), "geometry_wkt": data.get("geometry_wkt"),
                          "reason": "not_used_by_any_functional_terminal_shortest_route"})
            graph.remove_edge(u, v, key)
            removed_edges += 1
        unused = {n for n in nodes - protected if graph.degree(n) == 0}
        graph.remove_nodes_from(unused)
        nodes.difference_update(unused)
        contracted = _contract_degree_two(graph, nodes, protected, audit)
        checked, error = 0, 0.0
        for name, flag, weight in SCENARIOS:
            after = terminal_distances(route_graph(graph, nodes, flag, weight), protected)
            for pair, expected in before[name].items():
                actual = after[pair]
                if math.isinf(expected) and math.isinf(actual):
                    continue
                delta = abs(actual - expected)
                if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-7):
                    raise RuntimeError(f"Horizontal cleanup changed {name} route {pair}: {expected} -> {actual}")
                checked += 1
                error = max(error, delta)
        reports.append({"subgraph_id": subgraph_id, "functional_terminals": len(protected),
                        "removed_directed_edges": removed_edges, "removed_unused_nodes": len(unused),
                        "contracted_nodes": contracted, "retained_sparse_components": sparse_components,
                        "checked_finite_routes": checked, "maximum_cost_change": error})
        total_checked += checked
        total_contracted += contracted
        total_pruned_nodes += len(unused)
        total_removed_edges += removed_edges
        max_error = max(max_error, error)
    return CleanupResult({
        "method": "functional_terminal_shortest_path_union_v14",
        "nodes_before": initial_nodes, "nodes_after": graph.number_of_nodes(),
        "directed_edges_before": initial_edges, "directed_edges_after": graph.number_of_edges(),
        "unused_directed_edges_removed": total_removed_edges,
        "unused_nodes_removed": total_pruned_nodes, "degree_two_nodes_contracted": total_contracted,
        "checked_finite_terminal_routes": total_checked, "maximum_cost_change": max_error,
        "horizontal_subgraphs": len(reports), "status": "passed",
    }, reports, audit)
