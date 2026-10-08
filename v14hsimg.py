"""HSIMG V14: functional horizontal routes and exact polyline contraction.

Generator V14 is independent of the IFC model's revision number. Historical
V13 behavior remains available by importing v13hsimg.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import networkx as nx

from horizontal_cleanup import AXIS_TYPES, simplify_horizontal_graph
import v13hsimg as v13


@dataclass(slots=True)
class HSIMGConfig(v13.HSIMGConfig):
    simplify_functional_horizontal_routes: bool = True


class HSIMGBuilder(v13.HSIMGBuilder):
    def __init__(self, ifc_model: Any, config: HSIMGConfig | Mapping[str, Any] | None = None):
        resolved = HSIMGConfig(**dict(config)) if isinstance(config, Mapping) else config or HSIMGConfig()
        if not isinstance(resolved, HSIMGConfig):
            raise TypeError("V14 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.horizontal_cleanup = None
        self.axis_segments_replaced = 0

    def _add_axis_segment_door_approach(self, space: Any, candidate: dict[str, Any]) -> None:
        """Insert a real junction: replace its original edge, don't overlay it."""
        super()._add_axis_segment_door_approach(space, candidate)
        source, target = candidate["axis_source"], candidate["axis_target"]
        junction = candidate["junction"]
        original = candidate["axis_geometry"]
        # An endpoint attachment does not split the original line into two parts.
        along = original.project(junction)
        if along <= 1e-7 or original.length - along <= 1e-7:
            return
        # Both newly added halves must exist before the old representation goes.
        from hsimg import stable_id
        junction_id = stable_id("axis_junction_v12", space.ifc_guid,
                               candidate["door_id"], *junction.coords[0])
        if not all(self.graph.has_edge(junction_id, n) and self.graph.has_edge(n, junction_id)
                   for n in (source, target)):
            return
        removed = 0
        for u, v in ((source, target), (target, source)):
            for key, data in list(self.graph.get_edge_data(u, v, default={}).items()):
                if (data.get("edge_type") == candidate["axis_edge"].get("edge_type")
                    and data.get("geometry") is not None and data["geometry"].equals(original)):
                    self.graph.remove_edge(u, v, key)
                    removed += 1
        self.axis_segments_replaced += removed

    def clean_horizontal_routes(self):
        if not self.config.simplify_functional_horizontal_routes or self.horizontal_cleanup is not None:
            return self.horizontal_cleanup
        self.horizontal_cleanup = simplify_horizontal_graph(self.graph)
        self._rebuild_horizontal_axes_from_graph()
        self._refresh_subgraph_counts()
        self.model_metadata["horizontal_cleanup_v14"] = json.dumps(self.horizontal_cleanup.summary)
        return self.horizontal_cleanup

    def _projection_reaches_axis(self, local: nx.Graph, projection_id: str) -> bool:
        if self.horizontal_cleanup is None:
            return super()._projection_reaches_axis(local, projection_id)
        if projection_id not in local:
            return False
        component = nx.node_connected_component(local, projection_id)
        # After contraction an axis can run directly between two door projections.
        # Its existence no longer requires a vertex called 'axis_endpoint'.
        return any(data.get("edge_type") in AXIS_TYPES
                   and data.get("accessible_general") is not False
                   and float(data.get("length_3d", 0)) > 0
                   for _, _, data in self.graph.subgraph(component).edges(data=True))

    def _validate_walkable_region_coverage(self) -> None:
        if self.horizontal_cleanup is None:
            return super()._validate_walkable_region_coverage()
        validated = missing = fragmented = 0
        tolerance = self.config.walkable_region_node_tolerance_m
        for space in self.spaces.values():
            if (space.node_class != "horizontal_mobility" or space.footprint is None
                    or space.footprint.is_empty):
                continue
            local, _ = self._horizontal_space_graph(space.space_id)
            memberships = {node: index for index, component in enumerate(nx.connected_components(local))
                           for node in component}
            for part in self._polygon_parts(self._general_clearance_domain(space)):
                if part.area < self.config.walkable_region_min_area_m2:
                    continue
                validated += 1
                domain = part.buffer(tolerance)
                components = {memberships[node] for node in local
                              if local.degree(node) > 0 and domain.covers(self.graph.nodes[node]["geometry"])}
                # Count the complete routing geometry, including contracted bends,
                # even when both retained access vertices lie outside this region.
                for u, v, data in self.graph.subgraph(local.nodes).edges(data=True):
                    geometry = data.get("geometry")
                    if (local.has_edge(u, v) and geometry is not None
                            and geometry.intersection(domain).length > 1e-6):
                        components.add(memberships[u])
                if not components:
                    missing += 1
                    self._issue("error", "walkable_region_without_graph_v14",
                                f"Walkable region in {space.name} has no routing geometry",
                                "Review missing accesses and clearance geometry",
                                related_ifc_guid=space.ifc_guid, related_node_id=space.space_id,
                                geometry=part.representative_point())
                elif len(components) > 1:
                    fragmented += 1
                    self._issue("error", "fragmented_walkable_region_v14",
                                f"Walkable region in {space.name} contains {len(components)} graph components",
                                "Review missing connections in this region",
                                related_ifc_guid=space.ifc_guid, related_node_id=space.space_id,
                                geometry=part.representative_point())
        self.walkable_regions_validated = validated
        self.walkable_regions_without_graph = missing
        self.fragmented_walkable_regions = fragmented

    def validate_graph(self):
        # V6.run_all invokes validation after vertical construction and profile costs.
        # Pruning any earlier could remove a future stair/elevator landing attachment.
        self.clean_horizontal_routes()
        return super().validate_graph()

    def export_geopackage(self, output_path: str | Path) -> Path:
        output = super().export_geopackage(output_path)
        if self.horizontal_cleanup is not None:
            with sqlite3.connect(output) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS horizontal_cleanup_v14 "
                                   "(fid INTEGER PRIMARY KEY, record_type TEXT NOT NULL, data_json TEXT NOT NULL)")
                connection.execute("DELETE FROM horizontal_cleanup_v14")
                rows = [("summary", self.horizontal_cleanup.summary)]
                rows.extend(("subgraph", row) for row in self.horizontal_cleanup.spaces)
                rows.extend(("action", row) for row in self.horizontal_cleanup.audit)
                connection.executemany("INSERT INTO horizontal_cleanup_v14(record_type,data_json) VALUES (?,?)",
                                       [(kind, json.dumps(row, sort_keys=True, allow_nan=False)) for kind, row in rows])
                connection.execute("INSERT OR REPLACE INTO gpkg_contents "
                                   "(table_name,data_type,identifier,description,last_change) VALUES "
                                   "('horizontal_cleanup_v14','attributes','horizontal_cleanup_v14',"
                                   "'V14 functional routing cleanup and route preservation audit',"
                                   "strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        result.update({"HSIMG version": 14, "Version": 14,
                       "Replaced directed axis segments": self.axis_segments_replaced})
        if self.horizontal_cleanup is not None:
            result["Horizontal cleanup"] = self.horizontal_cleanup.summary
        return result


__all__ = ["HSIMGBuilder", "HSIMGConfig"]
