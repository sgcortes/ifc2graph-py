"""HSIMG V9: safe recovery of disconnected horizontal space components.

V9 extends V8 in two conservative ways: it joins disconnected medial-axis
components of the same walkable IfcSpace when a fully clearance-supported line
exists, and it recognises wall-free shared boundaries between distinct
horizontal-mobility spaces.  It never bridges a geometric gap between spaces
and never infers an open passage where both spaces reference the same IFC wall.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import networkx as nx
from shapely.geometry import LineString, Point

import v8hsimg as v8


@dataclass(slots=True)
class HSIMGConfig(v8.HSIMGConfig):
    recover_disconnected_space_components: bool = True
    horizontal_component_bridge_max_length_m: float = 12.0
    horizontal_component_bridge_max_per_space: int = 32
    infer_wall_free_open_space_boundaries: bool = True
    open_space_min_boundary_width_m: float = 0.90
    open_space_max_connector_length_m: float = 8.0

    def __post_init__(self) -> None:
        super().__post_init__()
        for field_name in (
            "horizontal_component_bridge_max_length_m",
            "open_space_min_boundary_width_m",
            "open_space_max_connector_length_m",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.horizontal_component_bridge_max_per_space < 0:
            raise ValueError(
                "horizontal_component_bridge_max_per_space must be non-negative"
            )


class HSIMGBuilder(v8.HSIMGBuilder):
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
            raise TypeError("V9 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.horizontal_component_bridges_added = 0
        self.horizontal_component_bridge_spaces = 0
        self.horizontal_component_bridge_candidates_rejected = 0
        self.open_space_transitions_added = 0
        self.open_space_candidates_rejected_wall = 0
        self.elevator_landings_without_horizontal_reach = 0

    def _recover_space_component_bridges(self, space: Any) -> int:
        local, node_ids = self._horizontal_space_graph(space.space_id)
        components = list(nx.connected_components(local))
        if len(components) <= 1:
            return 0
        component_by_node = {
            node_id: index
            for index, component in enumerate(components)
            for node_id in component
        }
        domain = v8.v7.v6.v5.cleaned_clearance_domain(
            space.footprint,
            self.config.medial_axis_min_hole_area_m2,
        )
        candidates: list[dict[str, Any]] = []
        for index, source in enumerate(node_ids):
            source_point = self.graph.nodes[source].get("geometry")
            if source_point is None:
                continue
            for target in node_ids[index + 1:]:
                if component_by_node[source] == component_by_node[target]:
                    continue
                target_point = self.graph.nodes[target].get("geometry")
                if target_point is None:
                    continue
                direct = math.hypot(
                    float(target_point.x) - float(source_point.x),
                    float(target_point.y) - float(source_point.y),
                )
                if (
                    direct <= 1e-6
                    or direct > self.config.horizontal_component_bridge_max_length_m
                ):
                    continue
                z = float(self.graph.nodes[source].get("z", 0.0))
                line = LineString([
                    (float(source_point.x), float(source_point.y), z),
                    (float(target_point.x), float(target_point.y), z),
                ])
                general_ok, wheelchair_ok, width = self._shortcut_clearance(
                    line,
                    domain,
                )
                if not general_ok:
                    self.horizontal_component_bridge_candidates_rejected += 1
                    continue
                candidates.append({
                    "source": source,
                    "target": target,
                    "source_component": component_by_node[source],
                    "target_component": component_by_node[target],
                    "line": line,
                    "length": direct,
                    "width": width,
                    "wheelchair": wheelchair_ok,
                })
        candidates.sort(
            key=lambda item: (
                item["length"],
                -item["width"],
                item["source"],
                item["target"],
            )
        )

        parent = list(range(len(components)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        added = 0
        subgraph_id = v8.v7.core.stable_id(
            "subgraph", space.ifc_guid, "horizontal"
        )
        for candidate in candidates:
            left = candidate["source_component"]
            right = candidate["target_component"]
            if find(left) == find(right):
                continue
            if added >= self.config.horizontal_component_bridge_max_per_space:
                break
            metadata = {
                "bridge_method": "same_space_clearance_component_recovery_v9",
                "components_before": len(components),
                "bridge_length_m": candidate["length"],
                "minimum_route_width_m": candidate["width"],
                "general_min_route_width_m": self.config.general_min_route_width_m,
                "wheelchair_min_route_width_m": self.config.wheelchair_min_route_width_m,
                "same_parent_space_required": True,
            }
            self._add_bidirectional_edge(
                candidate["source"],
                candidate["target"],
                edge_type="component_connector",
                mobility_mode="walk",
                subgraph_id=subgraph_id,
                geometry=candidate["line"],
                accessible_general=True,
                accessible_wheelchair=candidate["wheelchair"],
                restriction_reason=(
                    None if candidate["wheelchair"]
                    else "insufficient_wheelchair_clearance"
                ),
                relation_source="same_space_clearance_component_bridge_v9",
                validation_status="valid_component_bridge_v9",
                confidence=0.95,
                metadata=metadata,
            )
            union(left, right)
            added += 1
        if added:
            self._issue(
                "info",
                "horizontal_components_reconnected_v9",
                f"V9 reconnected {added} separated axis components in {space.name}",
                "No action required; the bridges passed the full clearance test",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        return added

    def _recover_horizontal_visibility_shortcuts(self) -> int:
        total = super()._recover_horizontal_visibility_shortcuts()
        if not self.config.recover_disconnected_space_components:
            return total
        bridges = 0
        spaces = 0
        for space in self.spaces.values():
            if (
                space.node_class != "horizontal_mobility"
                or space.footprint is None
                or space.footprint.is_empty
            ):
                continue
            added = self._recover_space_component_bridges(space)
            bridges += added
            spaces += int(added > 0)
        self.horizontal_component_bridges_added += bridges
        self.horizontal_component_bridge_spaces += spaces
        if bridges:
            self._rebuild_horizontal_axes_from_graph()
            self._refresh_subgraph_counts()
        return total + bridges

    def _spaces_share_ifc_wall(self, left_id: str, right_id: str) -> bool:
        for space_ids in self._boundary_wall_space_index().values():
            if left_id in space_ids and right_id in space_ids:
                return True
        return False

    def _space_target_near(self, space: Any, point: Point) -> str | None:
        targets = [
            node_id
            for node_id, data in self.graph.nodes(data=True)
            if data.get("parent_node_id") == space.space_id
            and data.get("node_type") == "internal_mobility"
            and data.get("mobility_type") == "horizontal"
        ]
        if not targets:
            return None
        return min(
            targets,
            key=lambda node_id: point.distance(Point(
                self.graph.nodes[node_id]["x"],
                self.graph.nodes[node_id]["y"],
            )),
        )

    def _connect_wall_free_open_space_boundaries(self) -> int:
        if not self.config.infer_wall_free_open_space_boundaries:
            return 0
        spaces = [
            space
            for space in self.spaces.values()
            if space.node_class == "horizontal_mobility"
            and space.footprint is not None
            and not space.footprint.is_empty
        ]
        door_pairs = {
            frozenset(door.connected_space_ids[:2])
            for door in self.doors.values()
            if len(door.connected_space_ids) >= 2
        }
        added = 0
        for index, left in enumerate(spaces):
            for right in spaces[index + 1:]:
                if left.storey_id != right.storey_id:
                    continue
                pair = frozenset((left.space_id, right.space_id))
                if pair in door_pairs:
                    continue
                shared = left.footprint.boundary.intersection(
                    right.footprint.boundary
                )
                if shared.is_empty or float(shared.length) < (
                    self.config.open_space_min_boundary_width_m
                ):
                    continue
                if self._spaces_share_ifc_wall(left.space_id, right.space_id):
                    self.open_space_candidates_rejected_wall += 1
                    continue
                lines = (
                    list(shared.geoms)
                    if hasattr(shared, "geoms")
                    else [shared]
                )
                line = max(lines, key=lambda geometry: float(geometry.length))
                portal = line.interpolate(0.5, normalized=True)
                left_target = self._space_target_near(left, portal)
                right_target = self._space_target_near(right, portal)
                if left_target is None or right_target is None:
                    continue
                connector = v8.v7.core.line_between(
                    self.graph.nodes[left_target]["geometry"],
                    self.graph.nodes[right_target]["geometry"],
                )
                if connector.length > self.config.open_space_max_connector_length_m:
                    continue
                union_domain = v8.v7.v6.v5.cleaned_clearance_domain(
                    left.footprint.union(right.footprint),
                    self.config.medial_axis_min_hole_area_m2,
                )
                general_ok, wheelchair_ok, width = self._shortcut_clearance(
                    connector,
                    union_domain,
                )
                if not general_ok:
                    continue
                self._add_bidirectional_edge(
                    left_target,
                    right_target,
                    edge_type="open_space_transition",
                    mobility_mode="walk",
                    subgraph_id=None,
                    geometry=connector,
                    accessible_general=True,
                    accessible_wheelchair=wheelchair_ok,
                    restriction_reason=(
                        None if wheelchair_ok
                        else "insufficient_wheelchair_clearance"
                    ),
                    relation_source="IfcSpace_wall_free_shared_boundary_v9",
                    validation_status="valid_open_space_transition_v9",
                    confidence=0.90,
                    metadata={
                        "left_space_id": left.space_id,
                        "right_space_id": right.space_id,
                        "shared_boundary_width_m": float(shared.length),
                        "minimum_route_width_m": width,
                        "shared_ifc_wall": False,
                    },
                )
                added += 1
        self.open_space_transitions_added += added
        return added

    def build_horizontal_subgraphs(self) -> list[dict[str, Any]]:
        result = super().build_horizontal_subgraphs()
        if self._connect_wall_free_open_space_boundaries():
            self._refresh_subgraph_counts()
        return result

    def _horizontal_component_for(self, node_id: str) -> set[str]:
        graph = nx.Graph()
        for source, target, data in self.graph.edges(data=True):
            if data.get("accessible_general") is False:
                continue
            source_storey = self.graph.nodes[source].get("storey_id")
            target_storey = self.graph.nodes[target].get("storey_id")
            if source_storey is not None and source_storey == target_storey:
                graph.add_edge(source, target)
        if node_id not in graph:
            return {node_id}
        return nx.node_connected_component(graph, node_id)

    def _stop_reaches_horizontal_space(self, stop_id: str) -> bool:
        for node_id in self._horizontal_component_for(stop_id):
            data = self.graph.nodes[node_id]
            space_id = (
                node_id if node_id in self.spaces
                else data.get("parent_node_id")
            )
            space = self.spaces.get(space_id)
            if space is not None and space.node_class == "horizontal_mobility":
                return True
        return False

    def validate_graph(self) -> list[Any]:
        issues = super().validate_graph()
        missing = 0
        for vertical_id in sorted(self.space_derived_elevator_ids):
            record = self.vertical_elements[vertical_id]
            for stop_id in self._derived_elevator_stop_ids(vertical_id):
                has_landing = any(
                    data.get("edge_type") == "elevator_cabin_access"
                    for _, _, data in self.graph.edges(stop_id, data=True)
                )
                if has_landing and not self._stop_reaches_horizontal_space(stop_id):
                    missing += 1
                    self._issue(
                        "error",
                        "elevator_landing_without_horizontal_reach_v9",
                        f"Elevator stop {stop_id} cannot reach a horizontal mobility space",
                        "Repair the open-space/door topology on this landing",
                        related_ifc_guid=record.ifc_guid,
                        related_node_id=stop_id,
                        geometry=self.graph.nodes[stop_id].get("geometry"),
                    )
        self.elevator_landings_without_horizontal_reach = missing
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
            if "door_access_v8" in tables and "door_access_v9" not in tables:
                connection.execute(
                    "ALTER TABLE door_access_v8 RENAME TO door_access_v9"
                )
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v9', "
                    "identifier='door_access_v9', "
                    "description='V9 door, elevator and horizontal connectivity validation' "
                    "WHERE table_name='door_access_v8'"
                )
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            connection.execute(
                "UPDATE gpkg_contents SET last_change=? "
                "WHERE table_name IN "
                "('graph_edges','graph_nodes','mobility_axes','vertical_elements')",
                (now,),
            )
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        result.update({
            "HSIMG version": 9,
            "Version": 9,
            "Same-space component bridges": self.horizontal_component_bridges_added,
            "Spaces with component bridges": self.horizontal_component_bridge_spaces,
            "Rejected component bridges": self.horizontal_component_bridge_candidates_rejected,
            "Wall-free open-space transitions": self.open_space_transitions_added,
            "Open-space candidates rejected by IFC wall": self.open_space_candidates_rejected_wall,
            "Elevator landings without horizontal reach": self.elevator_landings_without_horizontal_reach,
        })
        return result


__all__ = ["HSIMGBuilder", "HSIMGConfig"]
