"""HSIMG V11: width-aware door approaches for entrance vestibules.

V11 extends V10 with a bounded transition between a door-axis projection and
the fully eroded corridor domain.  Clearance at a door is governed by the
actual opening width, not by the projection's inevitable proximity to the host
wall.  Beyond this short throat, the connector remains subject to the complete
V10 clearance and obstacle checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import networkx as nx
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

import v10hsimg as v10


@dataclass(slots=True)
class HSIMGConfig(v10.HSIMGConfig):
    repair_door_approaches: bool = True
    door_approach_max_throat_length_m: float = 2.0
    door_approach_max_safe_connector_length_m: float = 8.0
    door_width_tolerance_m: float = 0.01

    def __post_init__(self) -> None:
        super().__post_init__()
        for field_name in (
            "door_approach_max_throat_length_m",
            "door_approach_max_safe_connector_length_m",
            "door_width_tolerance_m",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")


class HSIMGBuilder(v10.HSIMGBuilder):
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
            raise TypeError("V11 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.door_approach_repairs_added = 0
        self.door_approach_spaces_repaired = 0
        self.door_approach_rejections = 0
        self.door_projections_without_axis_reach = 0
        self.exterior_entrances_without_interior_reach = 0

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

    def _door_id_for_projection(self, projection_id: str) -> str | None:
        for source, target, data in self.graph.edges(projection_id, data=True):
            if data.get("edge_type") != "door_axis_projection":
                continue
            other = target if source == projection_id else source
            other_data = self.graph.nodes.get(other, {})
            if other_data.get("node_role") != "door_side":
                continue
            door_id = other_data.get("parent_node_id")
            if door_id in self.doors:
                return door_id
        return None

    def _projection_reaches_axis(
        self,
        local: nx.Graph,
        projection_id: str,
    ) -> bool:
        if projection_id not in local:
            return False
        component = nx.node_connected_component(local, projection_id)
        return any(
            node_id != projection_id
            and self.graph.nodes[node_id].get("node_role") == "axis_endpoint"
            for node_id in component
        )

    def _door_approach_candidate(
        self,
        space: Any,
        projection_id: str,
        local: nx.Graph,
    ) -> dict[str, Any] | None:
        door_id = self._door_id_for_projection(projection_id)
        if door_id is None:
            return None
        door = self.doors[door_id]
        width = door.width
        if (
            width is None
            or float(width) + self.config.door_width_tolerance_m
            < self.config.general_min_route_width_m
        ):
            self.door_approach_rejections += 1
            return None

        source_domain = v10.v9.v8.v7.v6.v5.cleaned_clearance_domain(
            space.footprint,
            self.config.medial_axis_min_hole_area_m2,
        )
        general_domain = source_domain.buffer(
            -0.5 * self.config.general_min_route_width_m
        )
        if general_domain.is_empty:
            self.door_approach_rejections += 1
            return None
        projection = self.graph.nodes[projection_id]["geometry"]
        candidates = [
            node_id
            for node_id in local.nodes
            if node_id != projection_id
            and local.degree(node_id) > 0
            and self.graph.nodes[node_id].get("node_role") == "axis_endpoint"
        ]
        options = []
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
            part_targets = [
                node_id
                for node_id in candidates
                if part.buffer(
                    self.config.walkable_region_node_tolerance_m
                ).covers(self.graph.nodes[node_id]["geometry"])
            ]
            for target in sorted(
                part_targets,
                key=lambda node_id: entry.distance(
                    self.graph.nodes[node_id]["geometry"]
                ),
            )[:8]:
                target_point = self.graph.nodes[target]["geometry"]
                path_2d = self._visibility_path(entry, target_point, part)
                if (
                    path_2d is None
                    or path_2d.length
                    > self.config.door_approach_max_safe_connector_length_m
                ):
                    continue
                z = float(self.graph.nodes[projection_id].get("z", 0.0))
                safe_path = LineString([
                    (float(x), float(y), z) for x, y, *_ in path_2d.coords
                ])
                general_ok, wheelchair_ok, route_width = self._shortcut_clearance(
                    safe_path, source_domain
                )
                if not general_ok:
                    continue
                options.append({
                    "door_id": door_id,
                    "door_width": float(width),
                    "projection": projection_id,
                    "target": target,
                    "entry": Point(float(entry.x), float(entry.y), z),
                    "throat": LineString([
                        (float(projection.x), float(projection.y), z),
                        (float(entry.x), float(entry.y), z),
                    ]),
                    "safe_path": safe_path,
                    "safe_width": route_width,
                    "wheelchair": bool(
                        door.wheelchair_accessible and wheelchair_ok
                    ),
                    "length": float(throat_2d.length + safe_path.length),
                })
                break
        return min(options, key=lambda item: item["length"]) if options else None

    def _add_door_approach(
        self,
        space: Any,
        candidate: dict[str, Any],
    ) -> None:
        subgraph_id = v10.v9.v8.v7.core.stable_id(
            "subgraph", space.ifc_guid, "horizontal"
        )
        entry_id = v10.v9.v8.v7.core.stable_id(
            "door_entry_v11",
            space.ifc_guid,
            candidate["door_id"],
            *candidate["entry"].coords[0],
        )
        if entry_id not in self.graph:
            self._add_node(
                entry_id,
                candidate["entry"],
                node_type="internal_mobility",
                node_role="door_clearance_entry",
                mobility_type="horizontal",
                parent_node_id=space.space_id,
                subgraph_id=subgraph_id,
                hierarchy_level=2,
                ifc_guid=space.ifc_guid,
                ifc_class=space.ifc_class,
                name=f"Door clearance entry at {space.name}",
                storey_id=space.storey_id,
                accessible_general=True,
                accessible_wheelchair=candidate["wheelchair"],
                metadata={
                    "door_id": candidate["door_id"],
                    "door_width_m": candidate["door_width"],
                    "method": "door_width_validated_throat_v11",
                },
            )
        throat_metadata = {
            "door_id": candidate["door_id"],
            "door_width_m": candidate["door_width"],
            "minimum_door_width_m": self.config.general_min_route_width_m,
            "throat_length_m": float(candidate["throat"].length),
            "clearance_basis": "actual_door_opening_width",
            "same_parent_space_required": True,
        }
        self._add_bidirectional_edge(
            candidate["projection"],
            entry_id,
            edge_type="door_throat_transition",
            mobility_mode="walk",
            subgraph_id=subgraph_id,
            geometry=candidate["throat"],
            accessible_general=True,
            accessible_wheelchair=candidate["wheelchair"],
            restriction_reason=(
                None
                if candidate["wheelchair"]
                else "insufficient_wheelchair_clearance"
            ),
            relation_source="door_width_validated_throat_v11",
            validation_status="valid_door_throat_v11",
            confidence=0.98,
            metadata=throat_metadata,
        )
        self._add_bidirectional_edge(
            entry_id,
            candidate["target"],
            edge_type="component_connector",
            mobility_mode="walk",
            subgraph_id=subgraph_id,
            geometry=candidate["safe_path"],
            accessible_general=True,
            accessible_wheelchair=candidate["wheelchair"],
            restriction_reason=(
                None
                if candidate["wheelchair"]
                else "insufficient_wheelchair_clearance"
            ),
            relation_source="door_approach_safe_connector_v11",
            validation_status="valid_door_approach_v11",
            confidence=0.98,
            metadata={
                "door_id": candidate["door_id"],
                "minimum_route_width_m": candidate["safe_width"],
                "general_min_route_width_m": self.config.general_min_route_width_m,
                "same_parent_space_required": True,
            },
        )

    def _repair_space_door_approaches(self, space: Any) -> int:
        local, node_ids = self._horizontal_space_graph(space.space_id)
        projections = [
            node_id
            for node_id in node_ids
            if self.graph.nodes[node_id].get("node_role") == "door_projection"
        ]
        added = 0
        for projection_id in projections:
            if self._projection_reaches_axis(local, projection_id):
                continue
            candidate = self._door_approach_candidate(space, projection_id, local)
            if candidate is None:
                continue
            self._add_door_approach(space, candidate)
            local, _ = self._horizontal_space_graph(space.space_id)
            added += 1
        if added:
            self._issue(
                "info",
                "door_approaches_repaired_v11",
                f"V11 connected {added} door approaches in {space.name}",
                "No action required; door throats use actual opening widths",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        return added

    def _repair_all_door_approaches(self) -> int:
        if not self.config.repair_door_approaches:
            return 0
        total = 0
        spaces = 0
        for space in self.spaces.values():
            if (
                space.node_class != "horizontal_mobility"
                or space.footprint is None
                or space.footprint.is_empty
            ):
                continue
            added = self._repair_space_door_approaches(space)
            total += added
            spaces += int(added > 0)
        self.door_approach_repairs_added += total
        self.door_approach_spaces_repaired += spaces
        if total:
            self._rebuild_horizontal_axes_from_graph()
            self._refresh_subgraph_counts()
        return total

    def build_horizontal_subgraphs(self) -> list[dict[str, Any]]:
        result = super().build_horizontal_subgraphs()
        self._repair_all_door_approaches()
        return result

    def _validate_door_projection_reach(self) -> None:
        missing = 0
        for space in self.spaces.values():
            if space.node_class != "horizontal_mobility":
                continue
            local, node_ids = self._horizontal_space_graph(space.space_id)
            for projection_id in node_ids:
                if (
                    self.graph.nodes[projection_id].get("node_role")
                    != "door_projection"
                    or self._projection_reaches_axis(local, projection_id)
                ):
                    continue
                missing += 1
                self._issue(
                    "error",
                    "door_projection_without_axis_reach_v11",
                    f"Door projection {projection_id} cannot reach the axis in {space.name}",
                    "Review door width, space footprint and throat geometry",
                    related_ifc_guid=space.ifc_guid,
                    related_node_id=projection_id,
                    geometry=self.graph.nodes[projection_id].get("geometry"),
                )
        self.door_projections_without_axis_reach = missing

    def _validate_exterior_entrance_reach(self) -> None:
        graph = nx.Graph()
        for source, target, data in self.graph.edges(data=True):
            if data.get("accessible_general") is not False:
                graph.add_edge(source, target)
        component_by_node = {
            node_id: index
            for index, component in enumerate(nx.connected_components(graph))
            for node_id in component
        }
        interior_components = {
            component_by_node[door_id]
            for door_id, metadata in self.door_access_metadata.items()
            if metadata.get("connected_space_count", 0) >= 2
            and door_id in component_by_node
        }
        missing = 0
        for door_id, metadata in self.door_access_metadata.items():
            if not metadata.get("entrance_exit_eligible"):
                continue
            if component_by_node.get(door_id) in interior_components:
                continue
            missing += 1
            door = self.doors[door_id]
            self._issue(
                "error",
                "exterior_entrance_without_interior_reach_v11",
                f"Exterior entrance {door.name} cannot reach an interior door",
                "Repair the door approach in its entrance vestibule",
                related_ifc_guid=door.ifc_guid,
                related_node_id=door_id,
                geometry=door.point,
            )
        self.exterior_entrances_without_interior_reach = missing

    def validate_graph(self) -> list[Any]:
        issues = super().validate_graph()
        self._validate_door_projection_reach()
        self._validate_exterior_entrance_reach()
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
            if "door_access_v10" in tables and "door_access_v11" not in tables:
                connection.execute(
                    "ALTER TABLE door_access_v10 RENAME TO door_access_v11"
                )
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v11', "
                    "identifier='door_access_v11', "
                    "description='V11 door throat and entrance reach validation' "
                    "WHERE table_name='door_access_v10'"
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
            "HSIMG version": 11,
            "Version": 11,
            "Door approach repairs": self.door_approach_repairs_added,
            "Spaces with door approach repairs": self.door_approach_spaces_repaired,
            "Rejected door approaches": self.door_approach_rejections,
            "Door projections without axis reach": (
                self.door_projections_without_axis_reach
            ),
            "Exterior entrances without interior reach": (
                self.exterior_entrances_without_interior_reach
            ),
        })
        return result
__all__ = ["HSIMGBuilder", "HSIMGConfig"]
