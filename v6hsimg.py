"""HSIMG V6: physically pruned pedestrian routing graphs.

V5 annotated narrow edges but retained them in the exported graph.  V6
removes every edge that is not suitable for general pedestrian movement,
rebuilds horizontal mobility axes from the surviving graph, and removes
internal components that no longer reach a door-side access node.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections import Counter, defaultdict, deque
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Optional

import networkx as nx
from shapely.geometry import LineString, MultiLineString
from shapely.ops import substring, unary_union

import v5hsimg as v5


def trimmed_line_2d(line: LineString, trim_ratio: float) -> LineString:
    """Return the useful 2D body of a route edge."""
    line_2d = LineString([(coordinate[0], coordinate[1]) for coordinate in line.coords])
    if line_2d.length <= 1e-9:
        return line_2d
    trim = min(max(float(trim_ratio), 0.0), 0.45) * line_2d.length
    if trim <= 1e-9 or 2.0 * trim >= line_2d.length:
        return line_2d
    candidate = substring(line_2d, trim, line_2d.length - trim)
    return candidate if isinstance(candidate, LineString) else line_2d


def line_supported_by_clearance_domain(
    line: LineString,
    polygon: Any,
    required_width_m: float,
    trim_ratio: float = 0.05,
    tolerance_m: float = 0.01,
) -> bool:
    """Test a route centreline against the eroded walkable domain."""
    radius = 0.5 * float(required_width_m)
    eroded = polygon.buffer(-radius)
    return line_covered_by_eroded_domain(
        line,
        eroded,
        trim_ratio=trim_ratio,
        tolerance_m=tolerance_m,
    )


def line_covered_by_eroded_domain(
    line: LineString,
    eroded_domain: Any,
    trim_ratio: float = 0.05,
    tolerance_m: float = 0.01,
) -> bool:
    """Test a line against a precomputed eroded domain."""
    eroded = eroded_domain
    if eroded.is_empty:
        return False
    useful = trimmed_line_2d(line, trim_ratio)
    return bool(eroded.buffer(max(tolerance_m, 1e-7)).covers(useful))


def connect_stair_flight_lines(
    path: LineString | MultiLineString,
    maximum_connector_length_m: float,
    snap_tolerance_m: float = 0.02,
) -> tuple[LineString | MultiLineString, int, list[float]]:
    """Join the ordered flights of one IfcStair through landing gaps."""
    source_lines = list(path.geoms) if isinstance(path, MultiLineString) else [path]
    if len(source_lines) <= 1:
        return path, 0, []

    flights: list[LineString] = []
    for source in source_lines:
        coordinates = list(source.coords)
        start_z = float(coordinates[0][2]) if len(coordinates[0]) >= 3 else 0.0
        end_z = float(coordinates[-1][2]) if len(coordinates[-1]) >= 3 else 0.0
        if start_z > end_z:
            coordinates.reverse()
        flights.append(LineString(coordinates))
    flights.sort(key=lambda line: float(line.coords[0][2]))

    assembled: list[LineString] = [flights[0]]
    connectors = 0
    unresolved_gaps: list[float] = []
    for upper in flights[1:]:
        lower_endpoint = tuple(assembled[-1].coords[-1])
        upper_endpoint = tuple(upper.coords[0])
        gap = math.dist(lower_endpoint, upper_endpoint)
        if gap > snap_tolerance_m:
            if gap <= maximum_connector_length_m:
                assembled.append(LineString([lower_endpoint, upper_endpoint]))
                connectors += 1
            else:
                unresolved_gaps.append(gap)
        assembled.append(upper)

    return MultiLineString(assembled), connectors, unresolved_gaps


@dataclass(slots=True)
class HSIMGConfig(v5.HSIMGConfig):
    """V6 controls for destructive pruning of non-pedestrian topology."""

    prune_non_pedestrian_edges: bool = True
    prune_accessless_horizontal_components: bool = True
    prune_unprotected_dead_ends: bool = True
    clearance_domain_tolerance_m: float = 0.01
    stair_landing_max_connector_length_m: float = 6.00
    stair_system_max_transition_length_m: float = 3.00

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.clearance_domain_tolerance_m <= 0:
            raise ValueError("clearance_domain_tolerance_m must be positive")
        if self.stair_landing_max_connector_length_m <= 0:
            raise ValueError(
                "stair_landing_max_connector_length_m must be positive"
            )
        if self.stair_system_max_transition_length_m <= 0:
            raise ValueError(
                "stair_system_max_transition_length_m must be positive"
            )


class HSIMGBuilder(v5.HSIMGBuilder):
    """V6 builder exporting only pedestrian-usable graph topology."""

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
            raise TypeError("V6 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.pruned_non_pedestrian_directed_edges = 0
        self.pruned_internal_nodes = 0
        self.pruned_accessless_components = 0
        self.pruned_unprotected_dead_end_nodes = 0
        self.pruned_horizontal_axis_records = 0
        self.stair_landing_connectors_added = 0
        self.stair_system_transitions_added = 0
        self.removed_intermediate_stair_access_edges = 0

    def _door_open_state(self, door: Any) -> tuple[bool, str]:
        """Resolve door state without confusing accessibility with openness."""
        if door.ifc_guid in self.config.closed_door_guids:
            return False, "config:closed_door_guids"
        if door.ifc_guid in self.config.open_door_guids:
            return True, "config:open_door_guids"
        if self._matches_any(door.name, self.config.closed_door_names):
            return False, "config:closed_door_names"
        if self._matches_any(door.name, self.config.open_door_names):
            return True, "config:open_door_names"

        positive_fields = {
            "doorstatus", "status", "state", "open", "isopen", "dooropen",
            "estado", "abierta", "abierto",
        }
        negative_fields = {
            "closed", "isclosed", "doorclosed", "locked", "islocked",
            "cerrada", "cerrado", "bloqueada", "bloqueado",
        }
        if self.config.infer_door_status_from_ifc_properties:
            for key, raw_value in door.properties.items():
                suffix = key.rsplit(".", 1)[-1].casefold()
                field = "".join(character for character in suffix if character.isalnum())
                if field not in positive_fields and field not in negative_fields:
                    continue
                inferred = self._normalise_boolean(raw_value)
                if inferred is None:
                    continue
                if field in negative_fields:
                    inferred = not inferred
                return inferred, f"IFC_property:{key}"
        return bool(self.config.default_door_open), "config:default_door_open"

    @staticmethod
    def _explicit_door_wheelchair_state(
        door: Any,
    ) -> tuple[Optional[bool], Optional[str]]:
        """Read wheelchair semantics independently from door open state."""
        return v5._property_boolean(
            door.properties,
            (
                "HandicapAccessible",
                "WheelchairAccessible",
                "AccessibleForWheelchair",
            ),
        )

    def _classify_door_access(self) -> dict[str, dict[str, Any]]:
        metadata = super()._classify_door_access()
        for door_id, door in self.doors.items():
            explicit, property_key = self._explicit_door_wheelchair_state(door)
            if explicit is not None:
                door.wheelchair_accessible = explicit
            metadata[door_id].update({
                "wheelchair_accessible": door.wheelchair_accessible,
                "wheelchair_access_source": (
                    f"IFC_property:{property_key}"
                    if property_key
                    else "geometry:door_width"
                ),
            })
        self.door_access_metadata = metadata
        return metadata

    def _apply_door_metadata_to_graph(self) -> None:
        """Apply door state to incoming and outgoing edges symmetrically."""
        super()._apply_door_metadata_to_graph()
        for door_id, metadata in self.door_access_metadata.items():
            related_nodes = {door_id}
            related_nodes.update(
                node_id
                for node_id, data in self.graph.nodes(data=True)
                if data.get("parent_node_id") == door_id
            )
            for source, target, _, data in self.graph.edges(keys=True, data=True):
                if source not in related_nodes and target not in related_nodes:
                    continue
                data.update(metadata)
                data["metadata_json"] = self._merge_metadata_json(
                    data.get("metadata_json"),
                    metadata,
                )
                if not metadata["door_open"]:
                    data["accessible_general"] = False
                    data["accessible_wheelchair"] = False
                    data["restriction_reason"] = "door_closed"
                elif metadata.get("wheelchair_accessible") is False:
                    data["accessible_wheelchair"] = False
                    if not data.get("restriction_reason"):
                        data["restriction_reason"] = (
                            "door_not_wheelchair_accessible"
                        )

    def _nonreciprocal_pedestrian_edges(self) -> list[tuple[str, str, str]]:
        """Return directed pedestrian edges without an equivalent reverse edge."""
        signatures = Counter(
            (
                source,
                target,
                str(data.get("edge_type")),
                str(data.get("subgraph_id")),
            )
            for source, target, data in self.graph.edges(data=True)
            if data.get("accessible_general") is not False
        )
        missing: list[tuple[str, str, str]] = []
        for (source, target, edge_type, subgraph_id), count in signatures.items():
            reverse_count = signatures.get(
                (target, source, edge_type, subgraph_id),
                0,
            )
            if count > reverse_count:
                missing.extend(
                    (source, target, edge_type)
                    for _ in range(count - reverse_count)
                )
        return missing

    def extract_stairs(self) -> dict[str, Any]:
        """Extract stairs and join their IFC flights through landing gaps."""
        result = super().extract_stairs()
        for vertical in self.vertical_elements.values():
            if vertical.vertical_type != "stair" or vertical.path is None:
                continue
            path, connector_count, unresolved = connect_stair_flight_lines(
                vertical.path,
                self.config.stair_landing_max_connector_length_m,
                self.config.vector_snap_tolerance_m,
            )
            vertical.path = path
            self.stair_landing_connectors_added += connector_count

            lines = list(path.geoms) if isinstance(path, MultiLineString) else [path]
            coordinates = [coordinate for line in lines for coordinate in line.coords]
            if coordinates:
                lower = min(coordinates, key=lambda coordinate: coordinate[2])
                upper = max(coordinates, key=lambda coordinate: coordinate[2])
                vertical.connected_storeys = list(
                    dict.fromkeys(
                        storey_id
                        for storey_id in (
                            self._nearest_storey_id(float(lower[2])),
                            self._nearest_storey_id(float(upper[2])),
                        )
                        if storey_id is not None
                    )
                )

            parent = self.model.by_guid(vertical.ifc_guid)
            landing_count = sum(
                related.is_a("IfcSlab")
                for relation in (getattr(parent, "IsDecomposedBy", ()) or ())
                for related in relation.RelatedObjects
            )
            vertical.properties.update({
                "HSIMG.StairLandingCount": landing_count,
                "HSIMG.StairLandingConnectorsAdded": connector_count,
                "HSIMG.StairUnresolvedLandingGaps": unresolved,
                "HSIMG.ConnectedStoreyMethod": "nearest_terminal_storeys_v6",
            })
            if unresolved:
                self._issue(
                    "error",
                    "stair_landing_gap_too_long_v6",
                    f"{vertical.name} has unresolved flight gaps: "
                    + ", ".join(f"{gap:.2f} m" for gap in unresolved),
                    "Review the IfcStair decomposition and landing geometry",
                    related_ifc_guid=vertical.ifc_guid,
                    related_node_id=vertical.vertical_id,
                )
        return result

    def _stair_stop_ids(self, vertical_id: str) -> list[str]:
        return [
            node_id
            for node_id, data in self.graph.nodes(data=True)
            if data.get("parent_node_id") == vertical_id
            and data.get("node_type") == "internal_mobility"
        ]

    def _normalise_stair_terminal_access(self) -> None:
        """Keep floor access only at the bottom and top of each stair."""
        for vertical in self.vertical_elements.values():
            if vertical.vertical_type != "stair":
                continue
            stop_ids = self._stair_stop_ids(vertical.vertical_id)
            if not stop_ids:
                continue
            ordered = sorted(
                stop_ids,
                key=lambda node_id: float(self.graph.nodes[node_id].get("z", 0.0)),
            )
            terminals = {ordered[0], ordered[-1]}
            internal = set(stop_ids) - terminals
            remove = [
                (source, target, key)
                for source, target, key, data in self.graph.edges(
                    keys=True,
                    data=True,
                )
                if data.get("edge_type") == "vertical_access"
                and (source in internal or target in internal)
            ]
            for edge in remove:
                self.graph.remove_edge(*edge)
            self.removed_intermediate_stair_access_edges += len(remove)

            lower_storey = (
                vertical.connected_storeys[0]
                if vertical.connected_storeys
                else self._nearest_storey_id(self.graph.nodes[ordered[0]]["z"])
            )
            upper_storey = (
                vertical.connected_storeys[-1]
                if vertical.connected_storeys
                else self._nearest_storey_id(self.graph.nodes[ordered[-1]]["z"])
            )
            for node_id, storey_id in (
                (ordered[0], lower_storey),
                (ordered[-1], upper_storey),
            ):
                self.graph.nodes[node_id]["storey_id"] = storey_id
                self.graph.nodes[node_id]["node_role"] = "landing"
            for node_id in internal:
                self.graph.nodes[node_id]["storey_id"] = None
                self.graph.nodes[node_id]["node_role"] = "intermediate_landing"

    def _connect_consecutive_stair_objects(self) -> int:
        """Join consecutive IfcStair objects across their shared floor landing.

        Revit commonly exports one IfcStair per inter-storey occurrence.  A
        continuous stair core is therefore split at every storey even though
        pedestrians can walk across the landing.  Pair only mutually-nearest
        upper/lower terminals on the same storey and within a conservative
        distance so unrelated nearby stairs are never joined.
        """
        terminals: list[dict[str, Any]] = []
        for vertical in self.vertical_elements.values():
            if vertical.vertical_type != "stair":
                continue
            stop_ids = self._stair_stop_ids(vertical.vertical_id)
            if not stop_ids:
                continue
            ordered = sorted(
                stop_ids,
                key=lambda node_id: float(self.graph.nodes[node_id].get("z", 0.0)),
            )
            for role, node_id in (("lower", ordered[0]), ("upper", ordered[-1])):
                data = self.graph.nodes[node_id]
                terminals.append({
                    "vertical_id": vertical.vertical_id,
                    "ifc_guid": vertical.ifc_guid,
                    "node_id": node_id,
                    "role": role,
                    "storey_id": data.get("storey_id"),
                    "coordinate": (
                        float(data.get("x", 0.0)),
                        float(data.get("y", 0.0)),
                        float(data.get("z", 0.0)),
                    ),
                })

        uppers = [terminal for terminal in terminals if terminal["role"] == "upper"]
        lowers = [terminal for terminal in terminals if terminal["role"] == "lower"]

        def nearest(
            terminal: dict[str, Any], candidates: list[dict[str, Any]]
        ) -> Optional[tuple[float, dict[str, Any]]]:
            compatible = [
                candidate
                for candidate in candidates
                if candidate["vertical_id"] != terminal["vertical_id"]
                and candidate["storey_id"] is not None
                and candidate["storey_id"] == terminal["storey_id"]
            ]
            if not compatible:
                return None
            candidate = min(
                compatible,
                key=lambda item: math.dist(
                    terminal["coordinate"], item["coordinate"]
                ),
            )
            return math.dist(terminal["coordinate"], candidate["coordinate"]), candidate

        lower_to_upper = {
            lower["node_id"]: nearest(lower, uppers)
            for lower in lowers
        }
        added = 0
        for upper in uppers:
            match = nearest(upper, lowers)
            if match is None:
                continue
            distance, lower = match
            reverse_match = lower_to_upper.get(lower["node_id"])
            if (
                reverse_match is None
                or reverse_match[1]["node_id"] != upper["node_id"]
                or distance > self.config.stair_system_max_transition_length_m
            ):
                continue
            existing = self.graph.get_edge_data(
                upper["node_id"], lower["node_id"], default={}
            )
            if any(
                data.get("edge_type") == "stair_landing_transition"
                for data in existing.values()
            ):
                continue
            geometry = LineString([
                upper["coordinate"],
                lower["coordinate"],
            ])
            self._add_bidirectional_edge(
                upper["node_id"],
                lower["node_id"],
                edge_type="stair_landing_transition",
                mobility_mode="stair",
                subgraph_id=None,
                geometry=geometry,
                accessible_general=True,
                accessible_wheelchair=False,
                restriction_reason="stairs_not_wheelchair_accessible",
                relation_source="geometry_inferred_shared_storey",
                confidence=max(
                    0.55,
                    1.0 - distance / self.config.stair_system_max_transition_length_m,
                ),
                metadata={
                    "shared_storey_id": upper["storey_id"],
                    "lower_storey_stair_id": upper["vertical_id"],
                    "upper_storey_stair_id": lower["vertical_id"],
                    "transition_length_m": distance,
                },
            )
            added += 1
        self.stair_system_transitions_added += added
        return added

    def build_vertical_subgraphs(self) -> list[dict[str, Any]]:
        result = super().build_vertical_subgraphs()
        self._normalise_stair_terminal_access()
        self._connect_consecutive_stair_objects()
        self._refresh_subgraph_counts()
        return result

    def _fragmented_stair_subgraphs(self) -> list[str]:
        fragmented: list[str] = []
        for vertical in self.vertical_elements.values():
            if vertical.vertical_type != "stair":
                continue
            subgraph_id = self.graph.nodes.get(vertical.vertical_id, {}).get(
                "subgraph_id"
            )
            local = nx.Graph()
            for source, target, data in self.graph.edges(data=True):
                if (
                    data.get("subgraph_id") == subgraph_id
                    and data.get("edge_type") == "vertical_path"
                ):
                    local.add_edge(source, target)
            if local.number_of_nodes() and not nx.is_connected(local):
                fragmented.append(vertical.vertical_id)
        return fragmented

    def _stairs_missing_terminal_access(self) -> list[tuple[str, str]]:
        missing: list[tuple[str, str]] = []
        for vertical in self.vertical_elements.values():
            if vertical.vertical_type != "stair":
                continue
            stop_ids = self._stair_stop_ids(vertical.vertical_id)
            if not stop_ids:
                continue
            ordered = sorted(
                stop_ids,
                key=lambda node_id: float(self.graph.nodes[node_id].get("z", 0.0)),
            )
            for label, node_id in (("lower", ordered[0]), ("upper", ordered[-1])):
                has_access = any(
                    data.get("edge_type") in {
                        "vertical_access",
                        "stair_landing_transition",
                    }
                    for _, _, data in self.graph.edges(node_id, data=True)
                )
                if not has_access:
                    missing.append((vertical.vertical_id, label))
        return missing

    def _apply_horizontal_clearance(self) -> None:
        """Classify every horizontal edge without a door-node exemption."""
        restricted_by_space: dict[str, int] = {}
        domains: dict[str, tuple[Any, Any, Any]] = {}
        restricted = 0
        for source, target, _, data in self.graph.edges(keys=True, data=True):
            if data.get("edge_type") not in {"internal_axis", "component_connector"}:
                continue
            geometry = data.get("geometry")
            if not isinstance(geometry, LineString) or geometry.is_empty:
                continue
            space = self._space_for_edge(source, target)
            if space is None or space.footprint is None:
                continue
            cached = domains.get(space.space_id)
            if cached is None:
                domain = v5.cleaned_clearance_domain(
                    space.footprint,
                    self.config.medial_axis_min_hole_area_m2,
                )
                cached = (
                    domain,
                    domain.buffer(-0.5 * self.config.general_min_route_width_m),
                    domain.buffer(-0.5 * self.config.wheelchair_min_route_width_m),
                )
                domains[space.space_id] = cached
            domain, general_domain, wheelchair_domain = cached
            width = v5.minimum_route_width(
                geometry,
                domain,
                self.config.route_width_sample_spacing_m,
                self.config.route_width_trim_ratio,
            )
            general_domain_ok = line_covered_by_eroded_domain(
                geometry,
                general_domain,
                self.config.route_width_trim_ratio,
                self.config.clearance_domain_tolerance_m,
            )
            wheelchair_domain_ok = line_covered_by_eroded_domain(
                geometry,
                wheelchair_domain,
                self.config.route_width_trim_ratio,
                self.config.clearance_domain_tolerance_m,
            )
            general_ok = bool(
                width >= self.config.general_min_route_width_m
                and general_domain_ok
            )
            wheelchair_ok = bool(
                width >= self.config.wheelchair_min_route_width_m
                and wheelchair_domain_ok
            )
            previous_general = data.get("accessible_general") is not False
            previous_wheelchair = data.get("accessible_wheelchair") is not False
            data["accessible_general"] = bool(previous_general and general_ok)
            data["accessible_wheelchair"] = bool(
                previous_wheelchair and wheelchair_ok and data["accessible_general"]
            )
            if not data["accessible_general"]:
                data["restriction_reason"] = "insufficient_general_clearance"
                data["validation_status"] = "prune_non_pedestrian_v6"
                restricted += 1
                restricted_by_space[space.space_id] = (
                    restricted_by_space.get(space.space_id, 0) + 1
                )
            elif not data["accessible_wheelchair"]:
                data["restriction_reason"] = "insufficient_wheelchair_clearance"
                data["validation_status"] = "restricted_wheelchair"
            data["metadata_json"] = self._merge_edge_metadata(
                data.get("metadata_json"),
                {
                    "minimum_route_width_m": width,
                    "general_min_route_width_m": self.config.general_min_route_width_m,
                    "wheelchair_min_route_width_m": (
                        self.config.wheelchair_min_route_width_m
                    ),
                    "general_eroded_domain_ok": general_domain_ok,
                    "wheelchair_eroded_domain_ok": wheelchair_domain_ok,
                    "clearance_method": "sampled_width_plus_eroded_domain_v6",
                    "door_transition_exemption": False,
                },
            )
        self.clearance_restricted_directed_edges = restricted
        for space_id, directed_count in restricted_by_space.items():
            space = self.spaces[space_id]
            self._issue(
                "info",
                "narrow_general_routes_pruned_v6",
                f"V6 identified {directed_count // 2} non-pedestrian route "
                f"segments in {space.name}",
                "These edges are removed from graph exports",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space_id,
                geometry=space.interior_point,
            )

    def _remove_inaccessible_edges(self, horizontal_only: bool) -> int:
        horizontal_types = {
            "internal_axis",
            "component_connector",
            "door_axis_projection",
            "space_access",
            "portal_transition",
        }
        remove = []
        for source, target, key, data in self.graph.edges(keys=True, data=True):
            if data.get("accessible_general") is not False:
                continue
            if horizontal_only and data.get("edge_type") not in horizontal_types:
                continue
            remove.append((source, target, key))
        for edge in remove:
            self.graph.remove_edge(*edge)
        self.pruned_non_pedestrian_directed_edges += len(remove)
        return len(remove)

    def _prune_accessless_components(self) -> int:
        if not self.config.prune_accessless_horizontal_components:
            return 0
        removed_components = 0
        nodes_to_remove: set[str] = set()
        horizontal_types = {
            "internal_axis",
            "component_connector",
            "door_axis_projection",
        }
        horizontal_ids = {
            row.get("subgraph_id")
            for row in self.subgraphs
            if row.get("subgraph_type") == "horizontal_mobility"
        }
        local_by_subgraph = {subgraph_id: nx.Graph() for subgraph_id in horizontal_ids}
        for source, target, data in self.graph.edges(data=True):
            subgraph_id = data.get("subgraph_id")
            if subgraph_id not in local_by_subgraph:
                continue
            if data.get("edge_type") in horizontal_types:
                local_by_subgraph[subgraph_id].add_edge(source, target)
        for node_id, data in self.graph.nodes(data=True):
            subgraph_id = data.get("subgraph_id")
            if subgraph_id in local_by_subgraph:
                local_by_subgraph[subgraph_id].add_node(node_id)
        for local in local_by_subgraph.values():
            for component in nx.connected_components(local):
                has_access = any(
                    self.graph.nodes[node_id].get("node_role") == "door_side"
                    or self.graph.nodes[node_id].get("node_type") == "door_access"
                    for node_id in component
                )
                if has_access:
                    continue
                removable = {
                    node_id
                    for node_id in component
                    if int(self.graph.nodes[node_id].get("hierarchy_level", 0)) > 0
                }
                if removable:
                    nodes_to_remove.update(removable)
                    removed_components += 1
        existing = nodes_to_remove.intersection(self.graph.nodes)
        self.graph.remove_nodes_from(existing)
        self.pruned_internal_nodes += len(existing)
        self.pruned_accessless_components += removed_components
        return removed_components

    def _prune_dead_end_branches(self) -> int:
        """Remove internal leaves that do not terminate at a door projection."""
        if not self.config.prune_unprotected_dead_ends:
            return 0
        local_by_subgraph: dict[str, nx.Graph] = defaultdict(nx.Graph)
        protected: set[str] = set()
        for node_id, data in self.graph.nodes(data=True):
            if data.get("node_role") in {"door_projection", "door_side"}:
                protected.add(node_id)
        for source, target, data in self.graph.edges(data=True):
            if data.get("edge_type") not in {"internal_axis", "component_connector"}:
                continue
            subgraph_id = data.get("subgraph_id")
            if subgraph_id:
                local_by_subgraph[subgraph_id].add_edge(source, target)

        remove: set[str] = set()
        for local in local_by_subgraph.values():
            queue = deque(
                node_id
                for node_id, degree in local.degree()
                if degree <= 1 and node_id not in protected
            )
            while queue:
                node_id = queue.popleft()
                if node_id not in local or node_id in protected:
                    continue
                neighbors = list(local.neighbors(node_id))
                local.remove_node(node_id)
                remove.add(node_id)
                for neighbor in neighbors:
                    if (
                        neighbor in local
                        and local.degree(neighbor) <= 1
                        and neighbor not in protected
                    ):
                        queue.append(neighbor)
        existing = remove.intersection(self.graph.nodes)
        self.graph.remove_nodes_from(existing)
        self.pruned_internal_nodes += len(existing)
        self.pruned_unprotected_dead_end_nodes += len(existing)
        return len(existing)

    def _remove_isolated_derived_nodes(self) -> int:
        remove = [
            node_id
            for node_id, data in self.graph.nodes(data=True)
            if self.graph.degree(node_id) == 0
            and int(data.get("hierarchy_level", 0)) > 0
        ]
        self.graph.remove_nodes_from(remove)
        self.pruned_internal_nodes += len(remove)
        return len(remove)

    @staticmethod
    def _canonical_line_key(geometry: LineString) -> tuple[Any, ...]:
        coordinates = tuple(
            tuple(round(float(value), 6) for value in coordinate)
            for coordinate in geometry.coords
        )
        reverse = tuple(reversed(coordinates))
        return min(coordinates, reverse)

    def _rebuild_horizontal_axes_from_graph(self) -> None:
        previous = [
            row
            for row in self.mobility_axes
            if row.get("mobility_type") == "horizontal"
        ]
        self.mobility_axes = [
            row
            for row in self.mobility_axes
            if row.get("mobility_type") != "horizontal"
        ]
        seen: set[tuple[Any, ...]] = set()
        for source, target, data in self.graph.edges(data=True):
            if data.get("edge_type") not in {"internal_axis", "component_connector"}:
                continue
            geometry = data.get("geometry")
            if not isinstance(geometry, LineString) or geometry.is_empty:
                continue
            key = (
                data.get("subgraph_id"),
                data.get("edge_type"),
                self._canonical_line_key(geometry),
            )
            if key in seen:
                continue
            seen.add(key)
            node = self.graph.nodes[source]
            source_method = (
                "bounded_component_connector_v4"
                if data.get("edge_type") == "component_connector"
                else "pedestrian_pruned_medial_axis_v6"
            )
            metadata = self._merge_edge_metadata(
                data.get("metadata_json"),
                {
                    "v6_export_scope": "general_pedestrian_only",
                    "source_edge_type": data.get("edge_type"),
                },
            )
            self.mobility_axes.append({
                "axis_id": v5.v4.v3.stable_id(
                    "axis_v6",
                    data.get("subgraph_id"),
                    source,
                    target,
                    len(seen),
                ),
                "parent_node_id": node.get("parent_node_id"),
                "subgraph_id": data.get("subgraph_id"),
                "mobility_type": "horizontal",
                "extraction_method": source_method,
                "pruning_threshold": self.config.medial_axis_pruning_length_m,
                "metadata_json": metadata,
                "geometry": geometry,
            })
        self.pruned_horizontal_axis_records += max(
            0,
            len(previous)
            - sum(
                row.get("mobility_type") == "horizontal"
                for row in self.mobility_axes
            ),
        )

    def _refresh_subgraph_counts(self) -> None:
        node_counts = Counter(
            data.get("subgraph_id")
            for _, data in self.graph.nodes(data=True)
            if data.get("subgraph_id") is not None
        )
        edge_counts = Counter(
            data.get("subgraph_id")
            for _, _, data in self.graph.edges(data=True)
            if data.get("subgraph_id") is not None
        )
        for subgraph in self.subgraphs:
            subgraph_id = subgraph.get("subgraph_id")
            subgraph["node_count"] = node_counts[subgraph_id]
            subgraph["edge_count"] = edge_counts[subgraph_id]

    def _prune_non_pedestrian_topology(self, horizontal_only: bool) -> None:
        if not self.config.prune_non_pedestrian_edges:
            return
        self._remove_inaccessible_edges(horizontal_only=horizontal_only)
        self._prune_accessless_components()
        self._prune_dead_end_branches()
        self._remove_isolated_derived_nodes()
        self._rebuild_horizontal_axes_from_graph()
        self._refresh_subgraph_counts()

    def build_horizontal_subgraphs(self) -> list[dict[str, Any]]:
        result = super().build_horizontal_subgraphs()
        self._prune_non_pedestrian_topology(horizontal_only=True)
        return result

    def run_all(self) -> "HSIMGBuilder":
        self.inspect_model()
        self.extract_storeys()
        self.extract_spaces()
        self.extract_doors()
        self.analyse_door_space_relationships()
        self.classify_spaces()
        self.assemble_space_and_door_graph()
        self.build_horizontal_subgraphs()
        self.extract_elevators()
        self.extract_stairs()
        self.extract_ramps()
        self.build_vertical_subgraphs()
        self.apply_profile_costs()
        self._prune_non_pedestrian_topology(horizontal_only=False)
        self.validate_graph()
        return self

    def validate_graph(self):
        issues = super().validate_graph()
        remaining = sum(
            data.get("accessible_general") is False
            for _, _, data in self.graph.edges(data=True)
        )
        if remaining:
            self._issue(
                "error",
                "non_pedestrian_edges_remain_in_v6",
                f"V6 graph still contains {remaining} directed non-pedestrian edges",
                "Review V6 pruning order",
            )
        nonreciprocal = self._nonreciprocal_pedestrian_edges()
        for source, target, edge_type in nonreciprocal:
            self._issue(
                "error",
                "nonreciprocal_pedestrian_edge_v6",
                f"V6 edge {source} -> {target} ({edge_type}) has no reverse edge",
                "Restore the reverse edge or declare an explicit one-way policy",
                related_node_id=source,
            )
        for vertical_id in self._fragmented_stair_subgraphs():
            self._issue(
                "error",
                "fragmented_stair_subgraph_v6",
                f"Stair {vertical_id} contains disconnected flight components",
                "Connect flights through their IFC landing",
                related_node_id=vertical_id,
            )
        for vertical_id, terminal in self._stairs_missing_terminal_access():
            vertical = self.vertical_elements[vertical_id]
            self._issue(
                "warning",
                "stair_terminal_without_space_access_v6",
                f"{vertical.name} has no {terminal} landing access to a space",
                "Review the stair landing, nearby IfcSpace and connecting door",
                related_ifc_guid=vertical.ifc_guid,
                related_node_id=vertical_id,
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
            if "door_access_v5" in tables and "door_access_v6" not in tables:
                connection.execute(
                    "ALTER TABLE door_access_v5 RENAME TO door_access_v6"
                )
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v6', "
                    "identifier='door_access_v6', "
                    "description='V6 door exterior/access validation' "
                    "WHERE table_name='door_access_v5'"
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
            "HSIMG version": 6,
            "Version": 6,
            "Graph scope": "general_pedestrian_only",
            "Pruned non-pedestrian route segments": (
                self.pruned_non_pedestrian_directed_edges // 2
            ),
            "Pruned derived nodes": self.pruned_internal_nodes,
            "Pruned accessless components": self.pruned_accessless_components,
            "Pruned unprotected dead-end nodes": (
                self.pruned_unprotected_dead_end_nodes
            ),
            "Remaining non-pedestrian directed edges": sum(
                data.get("accessible_general") is False
                for _, _, data in self.graph.edges(data=True)
            ),
            "Nonreciprocal pedestrian edges": len(
                self._nonreciprocal_pedestrian_edges()
            ),
            "Stair landing connectors added": (
                self.stair_landing_connectors_added
            ),
            "Inter-storey stair transitions added": (
                self.stair_system_transitions_added
            ),
            "Fragmented stair subgraphs": len(
                self._fragmented_stair_subgraphs()
            ),
            "Stair terminals without space access": len(
                self._stairs_missing_terminal_access()
            ),
            "Removed intermediate stair access edges": (
                self.removed_intermediate_stair_access_edges
            ),
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
    "connect_stair_flight_lines",
    "line_covered_by_eroded_domain",
    "line_supported_by_clearance_domain",
    "print_summary",
    "trimmed_line_2d",
]
