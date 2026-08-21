"""HSIMG V13: safe access recovery for multi-door finalist spaces.

V13 extends V12.  A non-circulation (``finalist``) space can have several
explicitly related doors.  The base exporter connects every door side to the
single semantic space point only when that straight segment is contained by
the IfcSpace footprint.  In large garages and stores that test can accept one
gate and reject its neighbour, leaving a two-node exterior component.

V13 keeps the rejected straight line rejected.  Instead, it joins the detached
door side to the nearest already anchored door side of the *same explicit IFC
space*.  The path is calculated in an eroded walkable domain, including short
door throats, so it cannot cross a wall, a hole or another space.
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

from hsimg import stable_id
from v5hsimg import cleaned_clearance_domain
import v12hsimg as v12


@dataclass(slots=True)
class HSIMGConfig(v12.HSIMGConfig):
    repair_finalist_door_access: bool = True
    finalist_door_bridge_max_length_m: float = 20.0

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.finalist_door_bridge_max_length_m <= 0:
            raise ValueError("finalist_door_bridge_max_length_m must be positive")


class HSIMGBuilder(v12.HSIMGBuilder):
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
            raise TypeError("V13 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.finalist_door_bridges_added = 0
        self.finalist_spaces_repaired = 0
        self.finalist_door_bridge_rejections = 0

    @staticmethod
    def _merge_path_coordinates(*parts: LineString, z: float) -> LineString:
        coordinates: list[tuple[float, float, float]] = []
        for part in parts:
            for raw in part.coords:
                coordinate = (float(raw[0]), float(raw[1]), z)
                if not coordinates or coordinate != coordinates[-1]:
                    coordinates.append(coordinate)
        return LineString(coordinates)

    def _safe_door_side_bridge(
        self,
        space: Any,
        source_id: str,
        target_id: str,
        width_m: float,
    ) -> LineString | None:
        """Return a wall-safe path between two sides of the same IFC space."""
        source_domain = cleaned_clearance_domain(
            space.footprint,
            self.config.medial_axis_min_hole_area_m2,
        )
        safe_domain = source_domain.buffer(-0.5 * width_m)
        if safe_domain.is_empty:
            return None
        source = self.graph.nodes[source_id]["geometry"]
        target = self.graph.nodes[target_id]["geometry"]
        source_2d = Point(float(source.x), float(source.y))
        target_2d = Point(float(target.x), float(target.y))
        z = float(self.graph.nodes[source_id].get("z", space.elevation))
        candidates: list[LineString] = []
        for part in self._polygon_parts(safe_domain):
            source_entry, _ = nearest_points(part, source_2d)
            target_entry, _ = nearest_points(part, target_2d)
            source_throat = LineString([source_2d, source_entry])
            target_throat = LineString([target_entry, target_2d])
            tolerance = self.config.clearance_domain_tolerance_m
            if not source_domain.buffer(tolerance).covers(source_throat):
                continue
            if not source_domain.buffer(tolerance).covers(target_throat):
                continue
            middle = self._visibility_path(source_entry, target_entry, part)
            if middle is None:
                continue
            path = self._merge_path_coordinates(
                source_throat,
                middle,
                target_throat,
                z=z,
            )
            if (
                path.length <= self.config.finalist_door_bridge_max_length_m
                and source_domain.buffer(tolerance).covers(
                    LineString([(x, y) for x, y, *_ in path.coords])
                )
            ):
                candidates.append(path)
        return min(candidates, key=lambda line: line.length, default=None)

    def _space_door_sides(self, space: Any) -> list[str]:
        result = []
        for door_id in space.connected_door_ids:
            door = self.doors.get(door_id)
            if door is None:
                continue
            side_id = stable_id("door_side", door.ifc_guid, space.ifc_guid)
            if side_id in self.graph:
                result.append(side_id)
        return result

    def _has_direct_space_access(self, side_id: str, space_id: str) -> bool:
        edge_sets = (
            self.graph.get_edge_data(side_id, space_id, default={}),
            self.graph.get_edge_data(space_id, side_id, default={}),
        )
        return any(
            data.get("edge_type") == "space_access"
            for edges in edge_sets
            for data in edges.values()
        )

    def _repair_finalist_space_door_access(self, space: Any) -> int:
        sides = self._space_door_sides(space)
        anchors = [
            side_id
            for side_id in sides
            if self._has_direct_space_access(side_id, space.space_id)
        ]
        detached = [side_id for side_id in sides if side_id not in anchors]
        if not anchors or not detached:
            return 0

        added = 0
        while detached:
            options: list[dict[str, Any]] = []
            for source_id in detached:
                source_door_id = self.graph.nodes[source_id].get("parent_node_id")
                source_door = self.doors.get(source_door_id)
                for target_id in anchors:
                    target_door_id = self.graph.nodes[target_id].get("parent_node_id")
                    target_door = self.doors.get(target_door_id)
                    wheelchair_path = None
                    if (
                        source_door is not None
                        and target_door is not None
                        and source_door.wheelchair_accessible is not False
                        and target_door.wheelchair_accessible is not False
                    ):
                        wheelchair_path = self._safe_door_side_bridge(
                            space,
                            source_id,
                            target_id,
                            self.config.wheelchair_min_route_width_m,
                        )
                    path = wheelchair_path or self._safe_door_side_bridge(
                        space,
                        source_id,
                        target_id,
                        self.config.general_min_route_width_m,
                    )
                    if path is not None:
                        options.append({
                            "source": source_id,
                            "target": target_id,
                            "path": path,
                            "wheelchair": wheelchair_path is not None,
                        })
            if not options:
                self.finalist_door_bridge_rejections += len(detached)
                break
            candidate = min(
                options,
                key=lambda item: (
                    item["path"].length,
                    item["source"],
                    item["target"],
                ),
            )
            self._add_bidirectional_edge(
                candidate["source"],
                candidate["target"],
                edge_type="space_access",
                mobility_mode="walk",
                subgraph_id=None,
                geometry=candidate["path"],
                accessible_general=True,
                accessible_wheelchair=candidate["wheelchair"],
                restriction_reason=(
                    None
                    if candidate["wheelchair"]
                    else "insufficient_wheelchair_clearance"
                ),
                relation_source="same_ifc_space_visibility_bridge_v13",
                validation_status="valid_finalist_space_bridge_v13",
                confidence=0.99,
                metadata={
                    "space_id": space.space_id,
                    "same_explicit_ifc_space_required": True,
                    "general_min_route_width_m": self.config.general_min_route_width_m,
                    "wheelchair_min_route_width_m": self.config.wheelchair_min_route_width_m,
                },
            )
            detached.remove(candidate["source"])
            anchors.append(candidate["source"])
            added += 1
        return added

    def _repair_all_finalist_door_access(self) -> int:
        if not self.config.repair_finalist_door_access:
            return 0
        total = 0
        spaces = 0
        for space in self.spaces.values():
            if (
                space.node_class != "finalist"
                or space.footprint is None
                or space.footprint.is_empty
            ):
                continue
            added = self._repair_finalist_space_door_access(space)
            total += added
            spaces += int(added > 0)
            if added:
                self._issue(
                    "info",
                    "finalist_door_access_repaired_v13",
                    f"V13 connected {added} detached door sides in {space.name}",
                    "No action required; bridges remain inside the explicit IFC space",
                    related_ifc_guid=space.ifc_guid,
                    related_node_id=space.space_id,
                    geometry=space.interior_point,
                )
        self.finalist_door_bridges_added += total
        self.finalist_spaces_repaired += spaces
        return total

    def assemble_space_and_door_graph(self) -> nx.MultiDiGraph:
        result = super().assemble_space_and_door_graph()
        self._repair_all_finalist_door_access()
        return result

    def export_geopackage(self, output_path: str | Path) -> Path:
        output = super().export_geopackage(output_path)
        with sqlite3.connect(output) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "door_access_v12" in tables and "door_access_v13" not in tables:
                connection.execute(
                    "ALTER TABLE door_access_v12 RENAME TO door_access_v13"
                )
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v13', "
                    "identifier='door_access_v13', "
                    "description='V13 safe finalist-space door access' "
                    "WHERE table_name='door_access_v12'"
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
            "HSIMG version": 13,
            "Version": 13,
            "Finalist door bridges": self.finalist_door_bridges_added,
            "Finalist spaces repaired": self.finalist_spaces_repaired,
            "Rejected finalist door bridges": self.finalist_door_bridge_rejections,
        })
        return result


__all__ = ["HSIMGBuilder", "HSIMGConfig"]
