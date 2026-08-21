"""HSIMG V7: elevator systems derived from vertically aligned IFC spaces.

V7 keeps the pedestrian-only topology of V6 and adds a conservative fallback
for models in which elevator cabins are not exported as IfcTransportElement.
Spaces explicitly labelled Elevator/Elevador/Ascensor/Lift are grouped by
storey and XY alignment.  Each group becomes one vertical system with one stop
per served storey.  Landing access is derived exclusively from openings hosted
by walls that are explicit boundaries of the source elevator space; proximity
to an unrelated door is never accepted as evidence.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import unary_union

import v6hsimg as v6


core = v6.v5.v4.v3


def _fold_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def is_elevator_space(space: Any, terms: Sequence[str]) -> bool:
    """Return True only for explicit elevator semantics, not loose substrings."""
    values = [
        getattr(space, "name", None),
        getattr(space, "long_name", None),
        getattr(space, "object_type", None),
    ]
    properties = getattr(space, "properties", {}) or {}
    values.extend(properties.values())
    normalized_terms = {_fold_label(term) for term in terms}
    for value in values:
        label = _fold_label(value)
        if not label:
            continue
        words = set(label.split())
        if any(term == label or term in words for term in normalized_terms):
            return True
    return False


def _walk_ifc_references(root: Any) -> list[Any]:
    """Return the IFC reference subgraph below an entity without looping."""
    seen: set[tuple[str, int]] = set()
    found: list[Any] = []
    pending = [root]
    while pending:
        entity = pending.pop()
        if not hasattr(entity, "is_a") or not hasattr(entity, "id"):
            continue
        key = (str(entity.is_a()), int(entity.id()))
        if key in seen:
            continue
        seen.add(key)
        found.append(entity)
        try:
            values = entity.get_info(
                include_identifier=False,
                recursive=False,
            ).values()
        except Exception:
            continue
        for value in values:
            if hasattr(value, "is_a"):
                pending.append(value)
            elif isinstance(value, (tuple, list)):
                pending.extend(item for item in value if hasattr(item, "is_a"))
    return found


def opening_profile_names(opening: Any) -> list[str]:
    """Collect every named profile referenced by an IfcOpeningElement."""
    return sorted({
        str(name)
        for entity in _walk_ifc_references(opening)
        if entity.is_a("IfcProfileDef")
        for name in (getattr(entity, "ProfileName", None),)
        if name
    })


def opening_profile_dimensions(opening: Any) -> tuple[Optional[float], Optional[float]]:
    """Return opening width/height from its 2D swept profile, in model units."""
    dimensions: list[tuple[float, float]] = []
    for entity in _walk_ifc_references(opening):
        if entity.is_a("IfcRectangleProfileDef"):
            dimensions.append((float(entity.XDim), float(entity.YDim)))
        elif entity.is_a("IfcCartesianPointList2D"):
            coordinates = np.asarray(entity.CoordList, dtype=float)
            if coordinates.ndim == 2 and coordinates.shape[1] >= 2:
                ranges = np.ptp(coordinates[:, :2], axis=0)
                positive = sorted(float(value) for value in ranges if value > 1e-6)
                if len(positive) >= 2:
                    dimensions.append((positive[0], positive[-1]))
        elif entity.is_a("IfcPolyline"):
            coordinates = np.asarray(
                [point.Coordinates[:2] for point in entity.Points],
                dtype=float,
            )
            if len(coordinates):
                ranges = np.ptp(coordinates, axis=0)
                positive = sorted(float(value) for value in ranges if value > 1e-6)
                if len(positive) >= 2:
                    dimensions.append((positive[0], positive[-1]))
    if not dimensions:
        return None, None
    # The smallest complete profile is the actual doorway section; extrusion
    # depth belongs to the void operation and is deliberately ignored.
    width, height = min(dimensions, key=lambda item: item[0] * item[1])
    return min(width, height), max(width, height)


def is_semantic_elevator_opening(
    profile_names: Sequence[str],
    terms: Sequence[str],
) -> bool:
    """Recognise the Revit door-opening family from its IFC profile name."""
    folded_terms = tuple(_fold_label(term) for term in terms)
    return any(
        any(term and term in _fold_label(profile_name) for term in folded_terms)
        for profile_name in profile_names
    )


def cluster_elevator_spaces(
    spaces: Sequence[Any],
    centroid_tolerance_m: float,
    footprint_tolerance_m: float,
) -> list[list[Any]]:
    """Cluster elevator spaces across different storeys by vertical alignment."""
    items = [
        space for space in spaces
        if getattr(space, "storey_id", None) is not None
        and getattr(space, "footprint", None) is not None
        and not space.footprint.is_empty
    ]
    parents = list(range(len(items)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parents[root_right] = root_left

    for left in range(len(items)):
        a = items[left]
        for right in range(left + 1, len(items)):
            b = items[right]
            if a.storey_id == b.storey_id:
                continue
            distance = a.footprint.centroid.distance(b.footprint.centroid)
            footprints_align = a.footprint.buffer(footprint_tolerance_m).intersects(
                b.footprint.buffer(footprint_tolerance_m)
            )
            if distance <= centroid_tolerance_m or footprints_align:
                union(left, right)

    grouped: defaultdict[int, list[Any]] = defaultdict(list)
    for index, item in enumerate(items):
        grouped[find(index)].append(item)
    return sorted(
        grouped.values(),
        key=lambda cluster: min(
            (space.footprint.centroid.x, space.footprint.centroid.y)
            for space in cluster
        ),
    )


@dataclass(slots=True)
class HSIMGConfig(v6.HSIMGConfig):
    """V7 controls for deriving elevators from semantic IfcSpace stacks."""

    elevator_space_terms: tuple[str, ...] = (
        "elevator", "elevador", "ascensor", "lift", "montacargas",
    )
    elevator_space_centroid_tolerance_m: float = 0.85
    elevator_space_footprint_tolerance_m: float = 0.15
    elevator_space_min_storeys: int = 2
    elevator_min_vertical_span_m: float = 1.00
    elevator_boundary_opening_tolerance_m: float = 0.25
    elevator_hall_boundary_tolerance_m: float = 0.25
    elevator_opening_deduplication_tolerance_m: float = 0.05
    elevator_opening_profile_terms: tuple[str, ...] = (
        "puerta ascensor", "puesta ascensor", "elevator door",
        "elevador", "ascensor", "lift",
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        for field_name in (
            "elevator_space_centroid_tolerance_m",
            "elevator_space_footprint_tolerance_m",
            "elevator_min_vertical_span_m",
            "elevator_boundary_opening_tolerance_m",
            "elevator_hall_boundary_tolerance_m",
            "elevator_opening_deduplication_tolerance_m",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.elevator_space_min_storeys < 2:
            raise ValueError("elevator_space_min_storeys must be at least 2")


class HSIMGBuilder(v6.HSIMGBuilder):
    """V7 builder with explicit storey-by-storey elevator topology."""

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
            raise TypeError("V7 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.space_derived_elevator_ids: set[str] = set()
        self.elevator_stop_space_ids: dict[tuple[str, str], list[str]] = {}
        self.elevator_door_connections: list[dict[str, Any]] = []
        self.synthetic_elevator_opening_portals = 0
        self.elevator_boundary_openings_rejected = 0
        self.rejected_transport_elevators = 0
        self._prevertical_component_size_by_node: dict[str, int] = {}
        self._elevator_opening_geometry_cache: dict[int, dict[str, Any]] = {}
        self._space_ids_by_boundary_wall: Optional[dict[int, set[str]]] = None

    @staticmethod
    def _path_vertical_span(path: Any) -> float:
        if path is None or path.is_empty:
            return 0.0
        lines = list(path.geoms) if isinstance(path, MultiLineString) else [path]
        elevations = [
            float(coordinate[2])
            for line in lines
            for coordinate in line.coords
            if len(coordinate) >= 3
        ]
        return max(elevations) - min(elevations) if elevations else 0.0

    def _space_cluster_record(
        self,
        cluster: Sequence[Any],
        cluster_index: int,
    ) -> Optional[Any]:
        by_storey: defaultdict[str, list[Any]] = defaultdict(list)
        for space in cluster:
            by_storey[space.storey_id].append(space)
        ordered_storeys = sorted(
            by_storey,
            key=lambda storey_id: self.storeys[storey_id].elevation,
        )
        if len(ordered_storeys) < self.config.elevator_space_min_storeys:
            representative = cluster[0]
            self._issue(
                "warning",
                "elevator_space_stack_too_short_v7",
                f"Elevator-labelled space {representative.name} occurs on only "
                f"{len(ordered_storeys)} storey/stories",
                "Model the shaft on at least two storeys or correct the space label",
                related_ifc_guid=representative.ifc_guid,
                related_node_id=representative.space_id,
                geometry=representative.interior_point,
            )
            return None

        guids = sorted(space.ifc_guid for space in cluster)
        vertical_id = core.stable_id("elevator_space_system_v7", *guids)
        coordinates: list[tuple[float, float, float]] = []
        stop_space_map: dict[str, list[str]] = {}
        for storey_id in ordered_storeys:
            storey_spaces = by_storey[storey_id]
            merged = unary_union([space.footprint for space in storey_spaces])
            point_2d = merged.representative_point()
            coordinates.append((
                float(point_2d.x),
                float(point_2d.y),
                float(self.storeys[storey_id].elevation),
            ))
            stop_space_map[storey_id] = sorted(space.space_id for space in storey_spaces)
            if len(storey_spaces) > 1:
                self._issue(
                    "warning",
                    "duplicate_elevator_spaces_on_storey_v7",
                    f"Elevator system {vertical_id} has {len(storey_spaces)} aligned "
                    f"spaces on {self.storeys[storey_id].name}; they were consolidated",
                    "Review duplicate or overlapping Revit spaces on this storey",
                    related_ifc_guid=storey_spaces[0].ifc_guid,
                    related_node_id=vertical_id,
                    geometry=core.force_3d_point(point_2d, self.storeys[storey_id].elevation),
                )

        span = coordinates[-1][2] - coordinates[0][2]
        if span < self.config.elevator_min_vertical_span_m:
            return None
        segments = [
            LineString([coordinates[index], coordinates[index + 1]])
            for index in range(len(coordinates) - 1)
        ]
        path = MultiLineString(segments)
        for storey_id, space_ids in stop_space_map.items():
            self.elevator_stop_space_ids[(vertical_id, storey_id)] = space_ids
        representative = cluster[0]
        record = core.VerticalMobilityRecord(
            vertical_id=vertical_id,
            ifc_guid=representative.ifc_guid,
            ifc_entity_id=representative.ifc_entity_id,
            ifc_class="IfcSpaceDerivedElevatorSystem",
            name=f"Space-derived elevator {cluster_index + 1}",
            vertical_type="elevator",
            path=path,
            connected_storeys=ordered_storeys,
            accessible_wheelchair=True,
            properties={
                "HSIMG.SourceEntity": "IfcSpace",
                "HSIMG.SourceSpaceGuids": guids,
                "HSIMG.SourceSpaceIdsByStorey": stop_space_map,
                "HSIMG.StopCoordinates": coordinates,
            },
            extraction_method="semantic_IfcSpace_plus_vertical_footprint_clustering_v7",
            confidence=0.82,
        )
        return record

    def extract_elevators(self) -> dict[str, Any]:
        """Prefer labelled space stacks and retain only credible IFC elevators."""
        super().extract_elevators()
        transport_records: dict[str, Any] = {}
        for vertical_id, record in list(self.vertical_elements.items()):
            if record.vertical_type != "elevator":
                continue
            span = self._path_vertical_span(record.path)
            if (
                len(set(record.connected_storeys)) >= 2
                and span >= self.config.elevator_min_vertical_span_m
            ):
                transport_records[vertical_id] = record
            else:
                self.rejected_transport_elevators += 1
                self._issue(
                    "warning",
                    "invalid_transport_elevator_rejected_v7",
                    f"Rejected {record.name}: it spans {span:.2f} m and "
                    f"{len(set(record.connected_storeys))} storey/stories",
                    "Correct the IfcTransportElement classification or use labelled elevator spaces",
                    related_ifc_guid=record.ifc_guid,
                    related_node_id=vertical_id,
                    geometry=record.path,
                )
        self.vertical_elements = dict(transport_records)

        labelled = [
            space for space in self.spaces.values()
            if is_elevator_space(space, self.config.elevator_space_terms)
        ]
        clusters = cluster_elevator_spaces(
            labelled,
            self.config.elevator_space_centroid_tolerance_m,
            self.config.elevator_space_footprint_tolerance_m,
        )
        derived_records: list[Any] = []
        for cluster_index, cluster in enumerate(clusters):
            record = self._space_cluster_record(cluster, cluster_index)
            if record is not None:
                derived_records.append(record)

        # A space-derived stack has explicit per-storey semantics.  Remove an
        # aligned transport record to avoid two competing elevator systems.
        for record in derived_records:
            center = record.path.centroid
            for transport_id, transport in list(self.vertical_elements.items()):
                if transport.vertical_type != "elevator" or transport.path is None:
                    continue
                if center.distance(transport.path.centroid) <= self.config.elevator_space_centroid_tolerance_m:
                    del self.vertical_elements[transport_id]
                    self._issue(
                        "info",
                        "transport_elevator_superseded_by_spaces_v7",
                        f"Space-derived elevator {record.vertical_id} superseded aligned {transport.name}",
                        "No action required; V7 uses the storey-explicit space stack",
                        related_ifc_guid=transport.ifc_guid,
                        related_node_id=record.vertical_id,
                        geometry=record.path,
                    )
            self.vertical_elements[record.vertical_id] = record
            self.space_derived_elevator_ids.add(record.vertical_id)
        return self.vertical_elements

    def _combined_stop_footprint(self, vertical_id: str, storey_id: str) -> Any:
        spaces = [
            self.spaces[space_id]
            for space_id in self.elevator_stop_space_ids.get((vertical_id, storey_id), [])
            if space_id in self.spaces and self.spaces[space_id].footprint is not None
        ]
        return unary_union([space.footprint for space in spaces]) if spaces else None

    def _door_has_routable_other_side(self, door_id: str) -> bool:
        if door_id not in self.graph:
            return False
        side_nodes = [
            node_id for node_id, data in self.graph.nodes(data=True)
            if data.get("parent_node_id") == door_id and data.get("node_role") == "door_side"
        ]
        return any(
            any(edge.get("accessible_general") is not False for edge in self.graph[node_id].values() for edge in edge.values())
            for node_id in side_nodes
        )

    def _cache_prevertical_component_sizes(self) -> None:
        """Measure each landing-side network before vertical systems join floors."""
        graph = v6.nx.Graph()
        graph.add_nodes_from(self.graph.nodes)
        for source, target, data in self.graph.edges(data=True):
            if data.get("accessible_general") is False:
                continue
            source_storey = self.graph.nodes[source].get("storey_id")
            target_storey = self.graph.nodes[target].get("storey_id")
            if source_storey is not None and source_storey == target_storey:
                graph.add_edge(source, target)
        self._prevertical_component_size_by_node = {
            node_id: len(component)
            for component in v6.nx.connected_components(graph)
            for node_id in component
        }

    def build_vertical_subgraphs(self) -> list[dict[str, Any]]:
        self._cache_prevertical_component_sizes()
        return super().build_vertical_subgraphs()

    def _boundary_wall_space_index(self) -> dict[int, set[str]]:
        if self._space_ids_by_boundary_wall is not None:
            return self._space_ids_by_boundary_wall
        index: defaultdict[int, set[str]] = defaultdict(set)
        for space in self.spaces.values():
            entity = self.model.by_guid(space.ifc_guid)
            for relation in (getattr(entity, "BoundedBy", ()) or ()):
                element = getattr(relation, "RelatedBuildingElement", None)
                if element is not None and element.is_a("IfcWall"):
                    index[element.id()].add(space.space_id)
        self._space_ids_by_boundary_wall = dict(index)
        return self._space_ids_by_boundary_wall

    def _opening_geometry(self, opening: Any) -> dict[str, Any]:
        cached = self._elevator_opening_geometry_cache.get(opening.id())
        if cached is not None:
            return cached
        point, footprint, method = self.geometry.point_and_footprint(opening)
        width, height = opening_profile_dimensions(opening)
        try:
            mesh = self.geometry.mesh(opening)
            if height is None:
                height = float(np.ptp(mesh.vertices[:, 2]))
        except Exception:
            pass
        result = {
            "point": point,
            "footprint": footprint,
            "method": method,
            "width": width,
            "height": height,
            "profile_names": opening_profile_names(opening),
        }
        self._elevator_opening_geometry_cache[opening.id()] = result
        return result

    def _hall_spaces_for_boundary_opening(
        self,
        wall: Any,
        source_space_ids: set[str],
        storey_id: str,
        raw: Point,
    ) -> list[tuple[float, Any]]:
        candidates: list[tuple[float, Any]] = []
        for space_id in self._boundary_wall_space_index().get(wall.id(), set()):
            if space_id in source_space_ids:
                continue
            space = self.spaces.get(space_id)
            if (
                space is None
                or space.storey_id != storey_id
                or space.footprint is None
                or is_elevator_space(space, self.config.elevator_space_terms)
            ):
                continue
            distance = float(space.footprint.boundary.distance(raw))
            if distance <= self.config.elevator_hall_boundary_tolerance_m:
                candidates.append((distance, space))
        return sorted(candidates, key=lambda item: (item[0], item[1].space_id))

    def _candidate_boundary_openings(
        self,
        vertical_id: str,
        storey_id: str,
        footprint: Any,
    ) -> list[dict[str, Any]]:
        """Find openings only on the actual boundary walls of source spaces."""
        source_space_ids = set(
            self.elevator_stop_space_ids.get((vertical_id, storey_id), [])
        )
        candidates: list[dict[str, Any]] = []
        seen_pairs: set[tuple[int, int]] = set()
        for space_id in sorted(source_space_ids):
            space = self.spaces.get(space_id)
            if space is None:
                continue
            entity = self.model.by_guid(space.ifc_guid)
            for relation in (getattr(entity, "BoundedBy", ()) or ()):
                wall = getattr(relation, "RelatedBuildingElement", None)
                if wall is None or not wall.is_a("IfcWall"):
                    continue
                for void_relation in (getattr(wall, "HasOpenings", ()) or ()):
                    opening = void_relation.RelatedOpeningElement
                    pair = (space.ifc_entity_id, opening.id())
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    geometry = self._opening_geometry(opening)
                    point = geometry["point"]
                    if point is None:
                        self.elevator_boundary_openings_rejected += 1
                        continue
                    raw = Point(point.x, point.y)
                    boundary_distance = float(space.footprint.boundary.distance(raw))
                    if boundary_distance > self.config.elevator_boundary_opening_tolerance_m:
                        # The wall can bound a long run of rooms.  This check
                        # proves that the void lies on this space's own segment.
                        continue
                    filled_doors = [
                        fill.RelatedBuildingElement
                        for fill in (getattr(opening, "HasFillings", ()) or ())
                        if fill.RelatedBuildingElement.is_a("IfcDoor")
                    ]
                    semantic_void = is_semantic_elevator_opening(
                        geometry["profile_names"],
                        self.config.elevator_opening_profile_terms,
                    )
                    if not filled_doors and not semantic_void:
                        continue
                    hall_spaces = self._hall_spaces_for_boundary_opening(
                        wall, source_space_ids, storey_id, raw
                    )
                    candidates.append({
                        **geometry,
                        "priority": 0 if filled_doors else 1,
                        "opening": opening,
                        "wall": wall,
                        "source_space": space,
                        "point": raw,
                        "geometry_point_3d": point,
                        "boundary_distance": boundary_distance,
                        "filled_doors": filled_doors,
                        "hall_spaces": hall_spaces,
                    })

        # Duplicate Revit walls can carry coincident voids.  Preserve one
        # deterministic portal per physical opening position, preferring a
        # filled IfcDoor over a semantic unfilled void.
        unique: list[dict[str, Any]] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (
                item["priority"],
                item["opening"].GlobalId,
            ),
        ):
            if any(
                candidate["point"].distance(existing["point"])
                <= self.config.elevator_opening_deduplication_tolerance_m
                for existing in unique
            ):
                continue
            unique.append(candidate)
        return unique

    def _hall_network_target(
        self,
        space: Any,
        raw: Point,
        storey_id: str,
    ) -> Optional[tuple[str, Point, LineString]]:
        side_2d = core.local_interior_point_at_boundary(
            space.footprint,
            raw,
            max(self.config.spatial_tolerance_m, 0.05),
        )
        if side_2d is None:
            return None
        side_point = core.force_3d_point(
            side_2d, self.storeys[storey_id].elevation
        )
        target_ids = [
            node_id
            for node_id, data in self.graph.nodes(data=True)
            if data.get("storey_id") == storey_id
            and (
                node_id == space.space_id
                or data.get("parent_node_id") == space.space_id
            )
        ]
        if not target_ids:
            return None
        target_id = min(
            target_ids,
            key=lambda node_id: raw.distance(Point(
                self.graph.nodes[node_id]["x"],
                self.graph.nodes[node_id]["y"],
            )),
        )
        connector = core.line_between(
            side_point, self.graph.nodes[target_id]["geometry"]
        )
        connector_2d = LineString([
            (coordinate[0], coordinate[1])
            for coordinate in connector.coords
        ])
        if not space.footprint.buffer(
            max(0.15, self.config.spatial_tolerance_m)
        ).covers(connector_2d):
            return None
        return target_id, side_point, connector

    def _connect_boundary_opening_candidate(
        self,
        stop_id: str,
        stop_point: Point,
        storey_id: str,
        subgraph_id: str,
        vertical: Any,
        elevator_footprint: Any,
        candidate: dict[str, Any],
    ) -> bool:
        opening = candidate["opening"]
        wall = candidate["wall"]
        raw: Point = candidate["point"]
        hall_target = None
        hall_space = None
        hall_boundary_distance = None
        for distance, possible_hall in candidate["hall_spaces"]:
            resolved = self._hall_network_target(possible_hall, raw, storey_id)
            if resolved is not None:
                hall_boundary_distance = distance
                hall_space = possible_hall
                hall_target = resolved
                break
        if hall_target is None or hall_space is None:
            return False

        inside_2d = core.local_interior_point_at_boundary(
            elevator_footprint,
            raw,
            max(self.config.spatial_tolerance_m, 0.05),
        )
        if inside_2d is None:
            return False
        elevation = self.storeys[storey_id].elevation
        inside_point = core.force_3d_point(inside_2d, elevation)
        portal_point = Point(raw.x, raw.y, elevation)
        inside_line = core.line_between(stop_point, inside_point)
        inside_line_2d = LineString([
            (coordinate[0], coordinate[1])
            for coordinate in inside_line.coords
        ])
        if not elevator_footprint.buffer(
            max(0.10, self.config.spatial_tolerance_m)
        ).covers(inside_line_2d):
            return False

        filled_door = candidate["filled_doors"][0] if candidate["filled_doors"] else None
        door = next((
            record for record in self.doors.values()
            if filled_door is not None and record.ifc_guid == filled_door.GlobalId
        ), None)
        width = door.width if door is not None else candidate.get("width")
        height = door.height if door is not None else candidate.get("height")
        wheelchair = (
            door.wheelchair_accessible is not False
            if door is not None
            else width is not None
            and float(width) >= self.config.wheelchair_min_door_width_m
        )
        relation_source = (
            "IfcSpaceBoundary_wall_void_filled_IfcDoor_v7"
            if door is not None
            else "IfcSpaceBoundary_wall_semantic_IfcOpeningElement_v7"
        )
        confidence = 0.98 if door is not None else 0.92
        reason = None if wheelchair else "elevator_opening_below_wheelchair_width"
        portal_id = core.stable_id("elevator_opening_portal_v7", opening.GlobalId)
        inside_id = core.stable_id(
            "elevator_opening_inside_v7", vertical.vertical_id,
            storey_id, opening.GlobalId,
        )
        outside_id = core.stable_id(
            "elevator_opening_outside_v7", vertical.vertical_id,
            storey_id, opening.GlobalId, hall_space.ifc_guid,
        )
        metadata = {
            "elevator_id": vertical.vertical_id,
            "opening_guid": opening.GlobalId,
            "wall_guid": wall.GlobalId,
            "filled_door_guid": None if door is None else door.ifc_guid,
            "source_space_id": candidate["source_space"].space_id,
            "hall_space_id": hall_space.space_id,
            "profile_names": candidate.get("profile_names", []),
            "opening_width_m": width,
            "opening_height_m": height,
            "elevator_boundary_distance_m": candidate["boundary_distance"],
            "hall_boundary_distance_m": hall_boundary_distance,
            "relation_source": relation_source,
        }
        self._add_node(
            portal_id, portal_point,
            node_type="door",
            node_role="elevator_opening_portal",
            mobility_type="access",
            parent_node_id=None,
            subgraph_id=subgraph_id,
            hierarchy_level=1,
            ifc_guid=opening.GlobalId,
            ifc_class="IfcOpeningElement",
            name=f"Elevator opening {opening.GlobalId}",
            storey_id=storey_id,
            accessible_wheelchair=wheelchair,
            metadata=metadata,
        )
        self._add_node(
            inside_id, inside_point,
            node_type="door_access",
            node_role="elevator_opening_inside",
            mobility_type="access",
            parent_node_id=portal_id,
            subgraph_id=subgraph_id,
            hierarchy_level=2,
            ifc_guid=opening.GlobalId,
            ifc_class="IfcOpeningElement",
            name=f"Elevator-side access {opening.GlobalId}",
            storey_id=storey_id,
            accessible_wheelchair=wheelchair,
            metadata=metadata,
        )
        target_id, outside_point, hall_connector = hall_target
        self._add_node(
            outside_id, outside_point,
            node_type="door_access",
            node_role="elevator_opening_hall_side",
            mobility_type="access",
            parent_node_id=portal_id,
            subgraph_id=subgraph_id,
            hierarchy_level=2,
            ifc_guid=opening.GlobalId,
            ifc_class="IfcOpeningElement",
            name=f"Hall-side access {opening.GlobalId}",
            storey_id=storey_id,
            accessible_wheelchair=wheelchair,
            metadata=metadata,
        )
        self._add_bidirectional_edge(
            stop_id, inside_id,
            edge_type="elevator_cabin_access",
            mobility_mode="walk",
            subgraph_id=subgraph_id,
            geometry=inside_line,
            accessible_wheelchair=wheelchair,
            restriction_reason=reason,
            relation_source=relation_source,
            confidence=confidence,
            metadata=metadata,
        )
        self._add_bidirectional_edge(
            inside_id, portal_id,
            edge_type="elevator_opening_transition",
            mobility_mode="door",
            subgraph_id=subgraph_id,
            geometry=core.line_between(inside_point, portal_point),
            accessible_wheelchair=wheelchair,
            restriction_reason=reason,
            relation_source=relation_source,
            confidence=confidence,
            metadata=metadata,
        )
        self._add_bidirectional_edge(
            portal_id, outside_id,
            edge_type="elevator_opening_transition",
            mobility_mode="door",
            subgraph_id=subgraph_id,
            geometry=core.line_between(portal_point, outside_point),
            accessible_wheelchair=wheelchair,
            restriction_reason=reason,
            relation_source=relation_source,
            confidence=confidence,
            metadata=metadata,
        )
        self._add_bidirectional_edge(
            outside_id, target_id,
            edge_type="elevator_landing_access",
            mobility_mode="walk",
            subgraph_id=subgraph_id,
            geometry=hall_connector,
            accessible_wheelchair=wheelchair,
            restriction_reason=reason,
            relation_source=relation_source,
            confidence=confidence,
            metadata=metadata,
        )
        self.synthetic_elevator_opening_portals += int(door is None)
        self.elevator_door_connections.append({
            "vertical_id": vertical.vertical_id,
            "storey_id": storey_id,
            "stop_id": stop_id,
            "portal_id": portal_id,
            "opening_guid": opening.GlobalId,
            "wall_guid": wall.GlobalId,
            "door_id": None if door is None else door.door_id,
            "hall_space_id": hall_space.space_id,
            "width_m": width,
            "height_m": height,
            "wheelchair_accessible": wheelchair,
            "relation_source": relation_source,
            "confidence": confidence,
        })
        return True

    def _connect_space_derived_elevator_stop(
        self,
        stop_id: str,
        point: Point,
        storey_id: str,
        subgraph_id: str,
        vertical: Any,
    ) -> None:
        footprint = self._combined_stop_footprint(vertical.vertical_id, storey_id)
        if footprint is None or footprint.is_empty:
            return
        candidates = self._candidate_boundary_openings(
            vertical.vertical_id, storey_id, footprint
        )
        if not candidates:
            self._issue(
                "warning",
                "elevator_stop_without_boundary_opening_v7",
                f"No valid opening exists on the boundary walls of {vertical.name} "
                f"on {self.storeys[storey_id].name}",
                "Model an elevator-door opening in a wall that bounds the elevator space",
                related_ifc_guid=vertical.ifc_guid,
                related_node_id=stop_id,
                geometry=point,
            )
            return
        connected = 0
        for candidate in candidates:
            connected += int(self._connect_boundary_opening_candidate(
                stop_id, point, storey_id, subgraph_id, vertical,
                footprint, candidate,
            ))
        if not connected:
            self._issue(
                "warning",
                "elevator_boundary_opening_without_hall_access_v7",
                f"Boundary opening(s) exist for {vertical.name} on "
                f"{self.storeys[storey_id].name}, but none reaches a bounded hall space",
                "Add or repair the IfcSpace boundary on the landing side of the opening",
                related_ifc_guid=vertical.ifc_guid,
                related_node_id=stop_id,
                geometry=point,
            )

    def _connect_vertical_stop_to_space(
        self,
        stop_id: str,
        point: Point,
        storey_id: Optional[str],
        subgraph_id: str,
        vertical: Any,
    ) -> None:
        if vertical.vertical_id in self.space_derived_elevator_ids and storey_id is not None:
            self._connect_space_derived_elevator_stop(
                stop_id, point, storey_id, subgraph_id, vertical
            )
            return
        super()._connect_vertical_stop_to_space(
            stop_id, point, storey_id, subgraph_id, vertical
        )

    def _derived_elevator_stop_ids(self, vertical_id: str) -> list[str]:
        return [
            node_id for node_id, data in self.graph.nodes(data=True)
            if data.get("parent_node_id") == vertical_id
            and data.get("node_role") == "elevator_stop"
        ]

    def validate_graph(self) -> list[Any]:
        issues = super().validate_graph()
        for vertical_id in sorted(self.space_derived_elevator_ids):
            record = self.vertical_elements[vertical_id]
            stops = self._derived_elevator_stop_ids(vertical_id)
            if len(stops) != len(record.connected_storeys):
                self._issue(
                    "error",
                    "elevator_stop_count_mismatch_v7",
                    f"{record.name} has {len(stops)} stops for "
                    f"{len(record.connected_storeys)} served storeys",
                    "Review the V7 consecutive-segment construction",
                    related_ifc_guid=record.ifc_guid,
                    related_node_id=vertical_id,
                )
            for stop_id in stops:
                cabin_edges = [
                    edge for _, _, edge in self.graph.edges(stop_id, data=True)
                    if edge.get("edge_type") == "elevator_cabin_access"
                ]
                has_access = bool(cabin_edges)
                if not has_access:
                    self._issue(
                        "warning",
                        "elevator_stop_unconnected_v7",
                        f"Elevator stop {stop_id} has no validated landing-door access",
                        "Model or verify an elevator opening in a boundary wall of the source space",
                        related_ifc_guid=record.ifc_guid,
                        related_node_id=stop_id,
                        geometry=self.graph.nodes[stop_id].get("geometry"),
                    )
                elif not any(edge.get("accessible_wheelchair") for edge in cabin_edges):
                    self._issue(
                        "warning",
                        "elevator_stop_without_accessible_door_v7",
                        f"Elevator stop {stop_id} is connected, but its selected door is not "
                        "wide enough for the wheelchair profile",
                        "Verify the landing-door width in Revit/IFC or model the actual elevator door",
                        related_ifc_guid=record.ifc_guid,
                        related_node_id=stop_id,
                        geometry=self.graph.nodes[stop_id].get("geometry"),
                    )
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
            if "door_access_v6" in tables and "door_access_v7" not in tables:
                connection.execute("ALTER TABLE door_access_v6 RENAME TO door_access_v7")
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v7', "
                    "identifier='door_access_v7', "
                    "description='V7 door and space-derived elevator access validation' "
                    "WHERE table_name='door_access_v6'"
                )
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            connection.execute(
                "UPDATE gpkg_contents SET last_change=? "
                "WHERE table_name IN ('graph_edges','graph_nodes','mobility_axes','vertical_elements')",
                (now,),
            )
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        result.update({
            "HSIMG version": 7,
            "Version": 7,
            "Space-derived elevator systems": len(self.space_derived_elevator_ids),
            "Space-derived elevator stops": sum(
                len(self._derived_elevator_stop_ids(vertical_id))
                for vertical_id in self.space_derived_elevator_ids
            ),
            "Elevator door connections": len(self.elevator_door_connections),
            "Synthetic elevator opening portals": (
                self.synthetic_elevator_opening_portals
            ),
            "Rejected boundary openings": self.elevator_boundary_openings_rejected,
            "Wheelchair-accessible elevator door connections": sum(
                connection["stop_id"] in self.graph
                and any(
                    edge.get("edge_type") == "elevator_cabin_access"
                    and edge.get("accessible_wheelchair")
                    for _, _, edge in self.graph.edges(connection["stop_id"], data=True)
                )
                for connection in self.elevator_door_connections
            ),
            "Rejected invalid IfcTransportElement elevators": self.rejected_transport_elevators,
        })
        return result


def print_summary(
    builder: HSIMGBuilder,
    geopackage_path: Optional[str | Path] = None,
) -> None:
    summary = builder.summary()
    if geopackage_path is not None:
        summary["GeoPackage output path"] = str(geopackage_path)
    print("\n".join(f"{key}: {value}" for key, value in summary.items()))


__all__ = [
    "HSIMGBuilder",
    "HSIMGConfig",
    "cluster_elevator_spaces",
    "is_semantic_elevator_opening",
    "is_elevator_space",
    "opening_profile_names",
    "opening_profile_dimensions",
    "print_summary",
]
