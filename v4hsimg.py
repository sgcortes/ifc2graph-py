"""HSIMG V4: obstacle-aware horizontal graphs with bounded repairs.

The IFC extraction, semantic hierarchy, vertical mobility and V3 door
semantics remain compatible with :mod:`v3hsimg`.  V4 replaces only the vector
horizontal-axis engine and records whether each line is a true medial ridge or
a short component-repair connector.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping, Optional

import networkx as nx
from shapely.geometry import LineString
from shapely.ops import unary_union

import v3hsimg as v3
from v4vector import VectorAxisConfig, VectorMedialAxisEngine


@dataclass(slots=True)
class HSIMGConfig(v3.HSIMGConfig):
    """V4 configuration with column retention and bounded graph repair."""

    medial_axis_min_hole_area_m2: float = 0.05
    vector_snap_tolerance_m: float = 0.02
    vector_min_edge_length_m: float = 0.05
    vector_max_component_connector_length_m: float = 1.00
    vector_max_component_connectors: int = 32

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.vector_min_edge_length_m <= 0:
            raise ValueError("vector_min_edge_length_m must be positive")
        if self.vector_max_component_connector_length_m <= 0:
            raise ValueError(
                "vector_max_component_connector_length_m must be positive"
            )
        if self.vector_max_component_connectors < 0:
            raise ValueError("vector_max_component_connectors must be non-negative")


class HSIMGBuilder(v3.HSIMGBuilder):
    """Unified V4 builder retaining the stable V3 public API."""

    def __init__(
        self,
        ifc_model: Any,
        config: HSIMGConfig | Mapping[str, Any] | None = None,
    ):
        if config is None:
            resolved = HSIMGConfig()
        elif isinstance(config, Mapping):
            resolved = HSIMGConfig(**dict(config))
        elif isinstance(config, HSIMGConfig):
            resolved = config
        else:
            raise TypeError("V4 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.vector_axis = VectorMedialAxisEngine(
            VectorAxisConfig(
                boundary_sample_spacing_m=(
                    self.config.vector_boundary_sample_spacing_m
                ),
                minimum_branch_length_m=self.config.medial_axis_pruning_length_m,
                simplification_tolerance_m=(
                    self.config.vector_simplification_tolerance_m
                ),
                containment_tolerance_m=self.config.spatial_tolerance_m,
                minimum_hole_area_m2=self.config.medial_axis_min_hole_area_m2,
                snap_tolerance_m=self.config.vector_snap_tolerance_m,
                minimum_edge_length_m=self.config.vector_min_edge_length_m,
                maximum_component_connector_length_m=(
                    self.config.vector_max_component_connector_length_m
                ),
                maximum_component_connectors=(
                    self.config.vector_max_component_connectors
                ),
            )
        )

    @staticmethod
    def _merge_edge_metadata(serialized: Any, additions: Mapping[str, Any]) -> str:
        try:
            current = json.loads(serialized) if serialized else {}
        except (TypeError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {"legacy_metadata": current}
        current.update(additions)
        return v3._json(current)

    def _annotate_v4_result(self, space: Any, result: Any) -> None:
        axes = [
            row
            for row in self.mobility_axes
            if row.get("parent_node_id") == space.space_id
        ]
        for row, source in zip(axes, result.line_sources):
            row["extraction_method"] = source
            row["metadata_json"] = v3._json(
                {**result.diagnostics, "line_source": source}
            )

        subgraph_id = v3.stable_id("subgraph", space.ifc_guid, "horizontal")
        for row in self.subgraphs:
            if row.get("subgraph_id") != subgraph_id:
                continue
            row["extraction_method"] = result.method
            parameters = {
                "axis_method": result.method,
                "boundary_sample_spacing_m": (
                    self.config.vector_boundary_sample_spacing_m
                ),
                "simplification_tolerance_m": (
                    self.config.vector_simplification_tolerance_m
                ),
                "pruning_m": self.config.medial_axis_pruning_length_m,
                "minimum_obstacle_hole_area_m2": (
                    self.config.medial_axis_min_hole_area_m2
                ),
                "snap_tolerance_m": self.config.vector_snap_tolerance_m,
                "minimum_edge_length_m": self.config.vector_min_edge_length_m,
                "maximum_component_connector_length_m": (
                    self.config.vector_max_component_connector_length_m
                ),
            }
            row["parameters_json"] = v3._json(parameters)

        connector_lines = [
            line
            for line, source in zip(result.lines, result.line_sources)
            if source == self.vector_axis.CONNECTOR_SOURCE
        ]
        connector_domain = (
            unary_union(connector_lines).buffer(
                max(self.config.vector_snap_tolerance_m * 0.25, 1e-6)
            )
            if connector_lines
            else None
        )
        if connector_domain is not None:
            for _, _, _, data in self.graph.edges(keys=True, data=True):
                if data.get("subgraph_id") != subgraph_id:
                    continue
                if data.get("edge_type") != "internal_axis":
                    continue
                geometry = data.get("geometry")
                if not isinstance(geometry, LineString) or geometry.length <= 0:
                    continue
                overlap = float(geometry.intersection(connector_domain).length)
                if overlap / float(geometry.length) < 0.50:
                    continue
                data["edge_type"] = "component_connector"
                data["relation_source"] = "bounded_geometry_repair"
                data["confidence"] = min(float(data.get("confidence", 1.0)), 0.75)
                data["metadata_json"] = self._merge_edge_metadata(
                    data.get("metadata_json"),
                    {
                        "axis_method": self.vector_axis.CONNECTOR_SOURCE,
                        "maximum_connector_length_m": (
                            self.config.vector_max_component_connector_length_m
                        ),
                    },
                )

        if result.component_connectors:
            longest = max(result.component_connector_lengths_m, default=0.0)
            self._issue(
                "info",
                "bounded_component_connectors_added",
                f"V4 added {result.component_connectors} local connectors to "
                f"{space.name}; longest is {longest:.2f} m",
                "Inspect connector provenance if this space is used for routing",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        if result.connected_components > 1:
            self._issue(
                "warning",
                "horizontal_axis_components_remaining",
                f"V4 retained {result.connected_components} bounded components "
                f"for {space.name}",
                "Review IFC obstacles or connect only access-relevant components",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        if result.removed_artifact_holes >= 50:
            self._issue(
                "info",
                "mesh_artifact_holes_removed",
                f"V4 removed {result.removed_artifact_holes} small mesh holes "
                f"({result.removed_artifact_hole_area_m2:.2f} m2) from {space.name}",
                "Confirm the configured hole-area threshold against IFC columns",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )

    @staticmethod
    def _preferred_cluster_node(graph: nx.MultiDiGraph, nodes: set[str]) -> str:
        return min(
            nodes,
            key=lambda node_id: (
                graph.nodes[node_id].get("node_role") != "door_projection",
                node_id,
            ),
        )

    def _collapse_short_horizontal_edges(self) -> dict[str, int]:
        """Merge nodes joined by sub-threshold axis pieces.

        Door projections are inserted after vector-axis construction and can
        split an otherwise valid segment only millimetres from its endpoint.
        V4 contracts those pairs, preserves projection nodes preferentially,
        and rewrites incident geometries to the surviving coordinates.
        """
        parent: dict[str, str] = {}

        def find(node_id: str) -> str:
            parent.setdefault(node_id, node_id)
            while parent[node_id] != node_id:
                parent[node_id] = parent[parent[node_id]]
                node_id = parent[node_id]
            return node_id

        def union(first: str, second: str) -> None:
            root_first, root_second = find(first), find(second)
            if root_first != root_second:
                parent[root_second] = root_first

        for source, target, _, data in self.graph.edges(keys=True, data=True):
            if data.get("edge_type") not in {
                "internal_axis",
                "component_connector",
            }:
                continue
            if float(data.get("length_3d", 0.0)) >= (
                self.config.vector_min_edge_length_m
            ):
                continue
            if self.graph.nodes[source].get("subgraph_id") != (
                self.graph.nodes[target].get("subgraph_id")
            ):
                continue
            union(source, target)

        groups: dict[str, set[str]] = {}
        for node_id in parent:
            groups.setdefault(find(node_id), set()).add(node_id)
        groups = {root: nodes for root, nodes in groups.items() if len(nodes) > 1}
        if not groups:
            return {}

        mapping: dict[str, str] = {}
        merged_by_subgraph: dict[str, int] = {}
        for nodes in groups.values():
            survivor = self._preferred_cluster_node(self.graph, nodes)
            subgraph_id = self.graph.nodes[survivor].get("subgraph_id")
            for node_id in nodes:
                mapping[node_id] = survivor
            merged_by_subgraph[subgraph_id] = (
                merged_by_subgraph.get(subgraph_id, 0) + len(nodes) - 1
            )

        rebuilt = nx.MultiDiGraph()
        for node_id, data in self.graph.nodes(data=True):
            survivor = mapping.get(node_id, node_id)
            if survivor != node_id:
                continue
            rebuilt.add_node(node_id, **dict(data))

        for source, target, key, original in self.graph.edges(
            keys=True,
            data=True,
        ):
            new_source = mapping.get(source, source)
            new_target = mapping.get(target, target)
            if new_source == new_target:
                continue
            data = dict(original)
            geometry = data.get("geometry")
            if isinstance(geometry, LineString):
                coordinates = list(geometry.coords)
                start = rebuilt.nodes[new_source]["geometry"].coords[0]
                end = rebuilt.nodes[new_target]["geometry"].coords[0]
                dimensions = len(coordinates[0])

                def sized(coordinate):
                    values = list(coordinate)
                    while len(values) < dimensions:
                        values.append(0.0)
                    return tuple(values[:dimensions])

                coordinates[0] = sized(start)
                coordinates[-1] = sized(end)
                geometry = LineString(coordinates)
                length = v3.length_3d(geometry)
                if length <= 1e-9:
                    continue
                previous = max(float(data.get("length_3d", length)), 1e-9)
                scale = length / previous
                data["geometry"] = geometry
                data["geometry_wkt"] = geometry.wkt
                data["length_3d"] = length
                data["horizontal_length"] = float(geometry.length)
                data["vertical_displacement"] = float(
                    coordinates[-1][2] - coordinates[0][2]
                ) if dimensions >= 3 else 0.0
                for cost_name in ("estimated_time", "effort_cost"):
                    value = data.get(cost_name)
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        data[cost_name] = float(value) * scale
            edge_id = v3.stable_id(
                "edge_v4",
                new_source,
                new_target,
                data.get("edge_type"),
                data.get("subgraph_id"),
                data.get("edge_id", key),
            )
            data.update(
                {
                    "edge_id": edge_id,
                    "source": new_source,
                    "target": new_target,
                }
            )
            rebuilt.add_edge(new_source, new_target, key=edge_id, **data)

        self.graph = rebuilt
        for subgraph in self.subgraphs:
            subgraph_id = subgraph.get("subgraph_id")
            subgraph["node_count"] = sum(
                data.get("subgraph_id") == subgraph_id
                for _, data in self.graph.nodes(data=True)
            )
            subgraph["edge_count"] = sum(
                data.get("subgraph_id") == subgraph_id
                for _, _, data in self.graph.edges(data=True)
            )
        return merged_by_subgraph

    def build_horizontal_subgraphs(self) -> list[dict[str, Any]]:
        if not self.config.prefer_vector_medial_axis:
            return super().build_horizontal_subgraphs()
        result_start = len(self.vector_axis.completed_results)
        subgraphs = super().build_horizontal_subgraphs()
        results = self.vector_axis.completed_results[result_start:]
        spaces = [
            space
            for space in self.spaces.values()
            if space.node_class == "horizontal_mobility"
            and space.footprint is not None
        ]
        if len(results) != len(spaces):
            raise RuntimeError(
                "V4 vector results do not align with horizontal mobility spaces"
            )
        merged_by_subgraph = self._collapse_short_horizontal_edges()
        for space, result in zip(spaces, results):
            self._annotate_v4_result(space, result)
            subgraph_id = v3.stable_id("subgraph", space.ifc_guid, "horizontal")
            merged = merged_by_subgraph.get(subgraph_id, 0)
            if merged:
                self._issue(
                    "info",
                    "near_coincident_horizontal_nodes_merged",
                    f"V4 merged {merged} near-coincident internal nodes in "
                    f"{space.name}",
                    "No action required; review only if a door projection was moved",
                    related_ifc_guid=space.ifc_guid,
                    related_node_id=space.space_id,
                    geometry=space.interior_point,
                )
        return subgraphs

    def validate_graph(self):
        issues = super().validate_graph()
        for subgraph in self.subgraphs:
            subgraph_id = subgraph.get("subgraph_id")
            short_edges = []
            for _, _, _, data in self.graph.edges(keys=True, data=True):
                if data.get("subgraph_id") != subgraph_id:
                    continue
                if data.get("edge_type") not in {
                    "internal_axis",
                    "component_connector",
                }:
                    continue
                if float(data.get("length_3d", 0.0)) < (
                    self.config.vector_min_edge_length_m
                ):
                    short_edges.append(data)
            if not short_edges:
                continue
            parent_id = subgraph.get("parent_node_id")
            space = self.spaces.get(parent_id)
            self._issue(
                "warning",
                "short_horizontal_edges_remaining",
                f"{len(short_edges)} directed horizontal edges remain below "
                f"{self.config.vector_min_edge_length_m:.2f} m",
                "Merge nearby projection and axis nodes",
                related_ifc_guid=space.ifc_guid if space else None,
                related_node_id=parent_id,
                geometry=space.interior_point if space else None,
            )
        return self.issues

    def summary(self) -> dict[str, Any]:
        summary = super().summary()
        summary.update(
            {
                "HSIMG version": 4,
                "Horizontal axis method": (
                    "vector_obstacle_aware_medial_axis_v4"
                    if self.config.prefer_vector_medial_axis
                    else "raster_medial_axis"
                ),
                "Bounded component connectors": sum(
                    data.get("edge_type") == "component_connector"
                    for _, _, data in self.graph.edges(data=True)
                )
                // 2,
                "Removed artifact holes": sum(
                    result.removed_artifact_holes
                    for result in self.vector_axis.completed_results
                ),
            }
        )
        return summary


DoorRecord = v3.DoorRecord
GeometryEngine = v3.GeometryEngine
SpaceRecord = v3.SpaceRecord
ValidationIssue = v3.ValidationIssue
VerticalMobilityRecord = v3.VerticalMobilityRecord


__all__ = [
    "DoorRecord",
    "GeometryEngine",
    "HSIMGBuilder",
    "HSIMGConfig",
    "SpaceRecord",
    "ValidationIssue",
    "VerticalMobilityRecord",
]
