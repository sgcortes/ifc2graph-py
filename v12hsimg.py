"""HSIMG V12: door-to-axis junctions for continuous circulation networks.

V12 extends V11 without adding proximity-only links between spaces.  When an
explicit IFC door already relates two spaces but its projection cannot reach a
horizontal axis, V12 attaches the projection to the nearest safe point on an
existing axis segment of the *same* space.  The connector is accepted only
inside the cleaned walkable footprint and therefore cannot cross a wall or an
obstacle.

Door width and corridor clearance are deliberately separate constraints.  A
pedestrian doorway is checked against ``general_min_door_width_m`` while the
route beyond the short doorway throat continues to use the full corridor
clearance rules inherited from V11.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import networkx as nx
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, substring

import v11hsimg as v11


@dataclass(slots=True)
class HSIMGConfig(v11.HSIMGConfig):
    repair_door_axis_segments: bool = True
    general_min_door_width_m: float = 0.60
    door_axis_segment_max_connector_length_m: float = 8.0

    def __post_init__(self) -> None:
        super().__post_init__()
        for field_name in (
            "general_min_door_width_m",
            "door_axis_segment_max_connector_length_m",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")


class HSIMGBuilder(v11.HSIMGBuilder):
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
            raise TypeError("V12 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.door_axis_segment_repairs_added = 0
        self.door_axis_segment_spaces_repaired = 0
        self.door_axis_segment_rejections = 0
        self.horizontal_spaces_without_network_reach = 0

    def _horizontal_space_graph(
        self,
        space_id: str,
    ) -> tuple[nx.Graph, list[str]]:
        node_ids = [
            node_id
            for node_id, data in self.graph.nodes(data=True)
            if data.get("parent_node_id") == space_id
            and data.get("node_type") == "internal_mobility"
            and data.get("mobility_type") == "horizontal"
            and data.get("node_role") in {
                "axis_endpoint",
                "axis_junction",
                "door_projection",
                "door_clearance_entry",
            }
        ]
        node_set = set(node_ids)
        local = nx.Graph()
        local.add_nodes_from(node_ids)
        for source, target, data in self.graph.edges(data=True):
            if source not in node_set or target not in node_set:
                continue
            if data.get("edge_type") not in {
                "internal_axis",
                "component_connector",
                "door_throat_transition",
            }:
                continue
            length = self._edge_length(data)
            previous = local.get_edge_data(source, target, {}).get("weight")
            if previous is None or length < previous:
                local.add_edge(source, target, weight=length)
        return local, node_ids

    def _axis_segments(self, space_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        # Door projections are legitimate vertices of the medial-axis chain.
        # In long narrow corridors most internal-axis segments run between
        # consecutive projections, while only the two terminal vertices carry
        # the ``axis_endpoint`` role.
        allowed_roles = {"axis_endpoint", "axis_junction", "door_projection"}
        for source, target, key, data in self.graph.edges(keys=True, data=True):
            if data.get("edge_type") not in {
                "internal_axis",
                "component_connector",
            }:
                continue
            source_data = self.graph.nodes[source]
            target_data = self.graph.nodes[target]
            if (
                source_data.get("parent_node_id") != space_id
                or target_data.get("parent_node_id") != space_id
                or source_data.get("node_role") not in allowed_roles
                or target_data.get("node_role") not in allowed_roles
            ):
                continue
            pair = tuple(sorted((source, target))) + (str(data.get("edge_id", key)),)
            reverse_pair = tuple(sorted((source, target)))
            if any(item[:2] == reverse_pair for item in seen):
                continue
            seen.add(pair)
            geometry = data.get("geometry")
            if geometry is None or geometry.is_empty or geometry.length <= 1e-9:
                continue
            result.append({
                "source": source,
                "target": target,
                "geometry": geometry,
                "edge": data,
            })
        return result

    def _door_axis_segment_candidate(
        self,
        space: Any,
        projection_id: str,
    ) -> dict[str, Any] | None:
        if not self.config.repair_door_axis_segments:
            return None
        door_id = self._door_id_for_projection(projection_id)
        if door_id is None:
            return None
        door = self.doors[door_id]
        if (
            door.width is not None
            and float(door.width) + self.config.door_width_tolerance_m
            < self.config.general_min_door_width_m
        ):
            self.door_axis_segment_rejections += 1
            return None

        source_domain = v11.v10.v9.v8.v7.v6.v5.cleaned_clearance_domain(
            space.footprint,
            self.config.medial_axis_min_hole_area_m2,
        )
        general_domain = source_domain.buffer(
            -0.5 * self.config.general_min_route_width_m
        )
        if general_domain.is_empty:
            self.door_axis_segment_rejections += 1
            return None
        projection = self.graph.nodes[projection_id]["geometry"]
        z = float(self.graph.nodes[projection_id].get("z", 0.0))
        options: list[dict[str, Any]] = []
        for part in self._polygon_parts(general_domain):
            entry, _ = nearest_points(part, Point(projection.x, projection.y))
            throat_2d = LineString([
                (float(projection.x), float(projection.y)),
                (float(entry.x), float(entry.y)),
            ])
            if (
                throat_2d.length > self.config.door_approach_max_throat_length_m
                or not source_domain.buffer(
                    self.config.clearance_domain_tolerance_m
                ).covers(throat_2d)
            ):
                continue
            for segment in self._axis_segments(space.space_id):
                line = segment["geometry"]
                junction_2d = line.interpolate(line.project(entry))
                if not part.buffer(
                    self.config.walkable_region_node_tolerance_m
                ).covers(junction_2d):
                    continue
                safe_2d = self._visibility_path(entry, junction_2d, part)
                if (
                    safe_2d is None
                    or safe_2d.length
                    > self.config.door_axis_segment_max_connector_length_m
                ):
                    continue
                safe_path = LineString([
                    (float(x), float(y), z) for x, y, *_ in safe_2d.coords
                ])
                general_ok, wheelchair_ok, route_width = self._shortcut_clearance(
                    safe_path, source_domain
                )
                if not general_ok:
                    continue
                junction = Point(float(junction_2d.x), float(junction_2d.y), z)
                options.append({
                    "door_id": door_id,
                    "door_width": None if door.width is None else float(door.width),
                    "projection": projection_id,
                    "entry": Point(float(entry.x), float(entry.y), z),
                    "throat": LineString([
                        (float(projection.x), float(projection.y), z),
                        (float(entry.x), float(entry.y), z),
                    ]),
                    "safe_path": safe_path,
                    "safe_width": route_width,
                    "wheelchair": bool(
                        door.wheelchair_accessible is not False and wheelchair_ok
                    ),
                    "junction": junction,
                    "axis_source": segment["source"],
                    "axis_target": segment["target"],
                    "axis_geometry": line,
                    "axis_edge": segment["edge"],
                    "length": float(throat_2d.length + safe_path.length),
                })
        if not options:
            self.door_axis_segment_rejections += 1
            return None
        return min(options, key=lambda item: (item["length"], item["door_id"]))

    @staticmethod
    def _segment_parts_at_point(line: LineString, point: Point) -> list[LineString]:
        distance = float(line.project(point))
        parts = [
            substring(line, 0.0, distance),
            substring(line, distance, float(line.length)),
        ]
        return [part for part in parts if part.geom_type == "LineString" and part.length > 1e-9]

    def _add_axis_segment_door_approach(
        self,
        space: Any,
        candidate: dict[str, Any],
    ) -> None:
        subgraph_id = v11.v10.v9.v8.v7.core.stable_id(
            "subgraph", space.ifc_guid, "horizontal"
        )
        junction_id = v11.v10.v9.v8.v7.core.stable_id(
            "axis_junction_v12",
            space.ifc_guid,
            candidate["door_id"],
            *candidate["junction"].coords[0],
        )
        if junction_id not in self.graph:
            self._add_node(
                junction_id,
                candidate["junction"],
                node_type="internal_mobility",
                node_role="axis_junction",
                mobility_type="horizontal",
                parent_node_id=space.space_id,
                subgraph_id=subgraph_id,
                hierarchy_level=2,
                ifc_guid=space.ifc_guid,
                ifc_class=space.ifc_class,
                name=f"Door-axis junction at {space.name}",
                storey_id=space.storey_id,
                accessible_general=True,
                accessible_wheelchair=candidate["wheelchair"],
                metadata={
                    "door_id": candidate["door_id"],
                    "method": "nearest_safe_axis_segment_v12",
                },
            )

        axis_endpoints = {
            candidate["axis_source"],
            candidate["axis_target"],
        }
        for part in self._segment_parts_at_point(
            candidate["axis_geometry"], candidate["junction"]
        ):
            endpoints = [Point(part.coords[0]), Point(part.coords[-1])]
            target = min(
                axis_endpoints,
                key=lambda node_id: min(
                    self.graph.nodes[node_id]["geometry"].distance(endpoint)
                    for endpoint in endpoints
                ),
            )
            target_point = self.graph.nodes[target]["geometry"]
            if min(target_point.distance(endpoint) for endpoint in endpoints) > 0.05:
                continue
            self._add_bidirectional_edge(
                junction_id,
                target,
                # Reuse the protected connector class so the V10/V6 final
                # topology pruning retains this audited V12 attachment.
                edge_type="component_connector",
                mobility_mode="walk",
                subgraph_id=subgraph_id,
                geometry=part,
                accessible_general=True,
                accessible_wheelchair=(
                    candidate["axis_edge"].get("accessible_wheelchair") is not False
                ),
                restriction_reason=candidate["axis_edge"].get("restriction_reason"),
                relation_source="nearest_safe_axis_segment_v12",
                validation_status="valid_axis_segment_attachment_v12",
                confidence=0.99,
                metadata={
                    "door_id": candidate["door_id"],
                    "same_parent_space_required": True,
                    "source_axis_edge_type": candidate["axis_edge"].get("edge_type"),
                },
            )
            axis_endpoints.discard(target)

        candidate = dict(candidate)
        candidate["target"] = junction_id
        super()._add_door_approach(space, candidate)

    def _repair_space_door_approaches(self, space: Any) -> int:
        local, node_ids = self._horizontal_space_graph(space.space_id)
        projections = [
            node_id
            for node_id in node_ids
            if self.graph.nodes[node_id].get("node_role") == "door_projection"
        ]
        added = 0
        segment_repairs = 0
        for projection_id in projections:
            if self._projection_reaches_axis(local, projection_id):
                continue
            candidate = super()._door_approach_candidate(
                space, projection_id, local
            )
            if candidate is not None:
                super()._add_door_approach(space, candidate)
            else:
                candidate = self._door_axis_segment_candidate(space, projection_id)
                if candidate is None:
                    continue
                self._add_axis_segment_door_approach(space, candidate)
                segment_repairs += 1
            local, _ = self._horizontal_space_graph(space.space_id)
            added += 1
        if added:
            self._issue(
                "info",
                "door_approaches_repaired_v12",
                f"V12 connected {added} door approaches in {space.name}",
                "No action required; each connector stays inside its parent space",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        self.door_axis_segment_repairs_added += segment_repairs
        self.door_axis_segment_spaces_repaired += int(segment_repairs > 0)
        return added

    def _validate_horizontal_network_reach(self) -> None:
        graph = nx.Graph()
        for source, target, data in self.graph.edges(data=True):
            if data.get("accessible_general") is not False:
                graph.add_edge(source, target)
        vertical_nodes = {
            node_id
            for node_id, data in self.graph.nodes(data=True)
            if data.get("node_type") == "vertical_mobility"
            or data.get("node_role") in {"landing", "elevator_stop"}
        }
        vertical_components = {
            frozenset(nx.node_connected_component(graph, node_id))
            for node_id in vertical_nodes
            if node_id in graph
        }
        reachable = set().union(*vertical_components) if vertical_components else set()
        missing = 0
        for space in self.spaces.values():
            if space.node_class != "horizontal_mobility":
                continue
            _, node_ids = self._horizontal_space_graph(space.space_id)
            if any(node_id in reachable for node_id in node_ids):
                continue
            missing += 1
            self._issue(
                "warning",
                "horizontal_space_without_vertical_reach_v12",
                f"Horizontal space {space.name} cannot reach a vertical network",
                "Review its doors, openings and neighbouring circulation spaces",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        self.horizontal_spaces_without_network_reach = missing

    def validate_graph(self) -> list[Any]:
        issues = super().validate_graph()
        self._validate_horizontal_network_reach()
        return issues

    def export_geopackage(self, output_path: str | Path) -> Path:
        output = super().export_geopackage(output_path)
        with sqlite3.connect(output) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "door_access_v11" in tables and "door_access_v12" not in tables:
                connection.execute(
                    "ALTER TABLE door_access_v11 RENAME TO door_access_v12"
                )
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v12', "
                    "identifier='door_access_v12', "
                    "description='V12 door-to-axis and circulation validation' "
                    "WHERE table_name='door_access_v11'"
                )
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            connection.execute(
                "UPDATE gpkg_contents SET last_change=? "
                "WHERE table_name IN ('graph_edges','graph_nodes','mobility_axes')",
                (now,),
            )
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        result.update({
            "HSIMG version": 12,
            "Version": 12,
            "Door-axis segment repairs": self.door_axis_segment_repairs_added,
            "Spaces with door-axis segment repairs": (
                self.door_axis_segment_spaces_repaired
            ),
            "Rejected door-axis segment repairs": self.door_axis_segment_rejections,
            "Horizontal spaces without vertical reach": (
                self.horizontal_spaces_without_network_reach
            ),
        })
        return result


__all__ = ["HSIMGBuilder", "HSIMGConfig"]
