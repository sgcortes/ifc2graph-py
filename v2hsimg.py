"""Version 2 of the IFC-to-HSIMG processing module.

This module extends :mod:`hsimg` with two door-level concepts:

* ``inout``: ``1`` for a confirmed exterior door. The explicit IFC
  ``Pset_DoorCommon.IsExternal`` value takes precedence; the single-space
  rule is only used when the IFC does not declare that property.
* ``door_status``: ``"open"`` or ``"closed"``. Closed doors remain in the
  graph for traceability, but their incident transitions are disabled for
  general and wheelchair routing.

The original ``hsimg.py`` remains unchanged. Existing code can migrate by
changing only the import::

    from v2hsimg import HSIMGBuilder, HSIMGConfig

Door state is conservative and reproducible: explicit configuration takes
precedence, then recognised IFC properties, and finally ``default_door_open``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Optional

import networkx as nx

from hsimg import (
    DoorRecord,
    GeometryEngine,
    HSIMGBuilder as _HSIMGBuilderV1,
    HSIMGConfig as _HSIMGConfigV1,
    SpaceRecord,
    StoreyRecord,
    ValidationIssue,
    VerticalMobilityRecord,
    _json,
)


@dataclass(slots=True)
class HSIMGConfig(_HSIMGConfigV1):
    """V2 configuration, including door state and public-access controls.

    ``closed_door_guids`` and ``closed_door_names`` provide explicit overrides.
    Name entries are case-insensitive regular expressions, allowing a group of
    private/service doors to be closed without editing the IFC.
    """

    default_door_open: bool = True
    closed_door_guids: tuple[str, ...] = ()
    closed_door_names: tuple[str, ...] = ()
    open_door_guids: tuple[str, ...] = ()
    open_door_names: tuple[str, ...] = ()
    infer_door_status_from_ifc_properties: bool = True

    def __post_init__(self) -> None:
        _HSIMGConfigV1.__post_init__(self)
        overlap = set(self.closed_door_guids) & set(self.open_door_guids)
        if overlap:
            raise ValueError(
                "A door GUID cannot appear in both closed_door_guids and "
                f"open_door_guids: {sorted(overlap)}"
            )
        for field_name in ("closed_door_names", "open_door_names"):
            for pattern in getattr(self, field_name):
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    raise ValueError(
                        f"Invalid regular expression in {field_name}: {pattern!r}"
                    ) from exc


class HSIMGBuilder(_HSIMGBuilderV1):
    """V2 builder with exterior-door and open/closed-door semantics."""

    config: HSIMGConfig

    def __init__(
        self,
        ifc_model: Any,
        config: HSIMGConfig | Mapping[str, Any] | None = None,
    ) -> None:
        resolved = config if isinstance(config, HSIMGConfig) else HSIMGConfig(**(config or {}))
        super().__init__(ifc_model, resolved)
        self.door_access_metadata: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _normalise_state_value(value: Any) -> Optional[bool]:
        """Return True=open, False=closed, or None when the value is unknown."""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        text = str(value).strip().casefold()
        open_values = {
            "open", "opened", "abierta", "abierto", "accessible", "accesible",
            "public", "publica", "pública", "unlocked", "desbloqueada", "yes", "si", "sí", "true", "1",
        }
        closed_values = {
            "closed", "cerrada", "cerrado", "locked", "bloqueada", "bloqueado",
            "private", "privada", "privado", "restricted", "restringida", "restringido",
            "prohibited", "prohibida", "prohibido", "no", "false", "0",
        }
        if text in open_values:
            return True
        if text in closed_values:
            return False
        return None

    def _door_open_state(self, door: DoorRecord) -> tuple[bool, str]:
        """Resolve door state using explicit overrides before IFC properties."""
        if door.ifc_guid in self.config.closed_door_guids:
            return False, "config:closed_door_guids"
        if door.ifc_guid in self.config.open_door_guids:
            return True, "config:open_door_guids"
        if self._matches_any(door.name, self.config.closed_door_names):
            return False, "config:closed_door_names"
        if self._matches_any(door.name, self.config.open_door_names):
            return True, "config:open_door_names"

        if self.config.infer_door_status_from_ifc_properties:
            for key, raw_value in door.properties.items():
                field_name = key.rsplit(".", 1)[-1].casefold()
                if not re.search(
                    r"doorstatus|status|state|open|closed|locked|access|"
                    r"accessibility|publicaccess|securitylevel|estado|abierta|"
                    r"cerrada|bloqueada|acceso|accesopublico",
                    field_name,
                ):
                    continue
                inferred = self._normalise_state_value(raw_value)
                if inferred is None:
                    continue
                # Boolean fields named Closed/Locked express the inverse of
                # openness; textual Status/State values already express it.
                if isinstance(raw_value, (bool, int, float)) and re.search(
                    r"closed|locked|cerrad|bloquead", field_name
                ):
                    inferred = not inferred
                return inferred, f"IFC_property:{key}"

        return bool(self.config.default_door_open), "config:default_door_open"

    def _classify_door_access(self) -> dict[str, dict[str, Any]]:
        """Create V2 metadata after door-space relationships are available."""
        metadata: dict[str, dict[str, Any]] = {}
        for door_id, door in self.doors.items():
            explicit_external: Optional[bool] = None
            explicit_source: Optional[str] = None
            for key, value in door.properties.items():
                if key.casefold().endswith(".isexternal"):
                    normalised = self._normalise_state_value(value)
                    if normalised is not None:
                        explicit_external = normalised
                        explicit_source = f"IFC_property:{key}"
                        break

            # The IFC semantic property is authoritative. Geometry inference
            # can miss the space on one side of an internal door (especially
            # when IfcRelSpaceBoundary is absent), so a single associated space
            # is only a fallback when IsExternal is not declared.
            if explicit_external is not None:
                inout = explicit_external
                inout_source = explicit_source
            else:
                inout = len(door.connected_space_ids) == 1
                inout_source = "geometry:single_connected_space"
            is_open, status_source = self._door_open_state(door)
            metadata[door_id] = {
                "inout": int(inout),
                "inout_source": inout_source,
                "door_status": "open" if is_open else "closed",
                "door_open": bool(is_open),
                "public_access": bool(is_open),
                "door_status_source": status_source,
                "connected_space_count": len(door.connected_space_ids),
            }
        self.door_access_metadata = metadata
        return metadata

    def analyse_door_space_relationships(self) -> dict[str, list[str]]:
        relationships = super().analyse_door_space_relationships()
        self._classify_door_access()
        return relationships

    @staticmethod
    def _merge_metadata_json(serialized: Any, additions: Mapping[str, Any]) -> str:
        try:
            current = json.loads(serialized) if serialized else {}
        except (TypeError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {"legacy_metadata": current}
        current.update(additions)
        return _json(current)

    def _apply_door_metadata_to_graph(self) -> None:
        """Annotate door nodes/edges and disable transitions through closed doors."""
        for door_id, metadata in self.door_access_metadata.items():
            if door_id in self.graph:
                node = self.graph.nodes[door_id]
                node.update(metadata)
                node["accessible_general"] = metadata["door_open"]
                node["accessible_wheelchair"] = bool(
                    node.get("accessible_wheelchair") is not False
                    and metadata["door_open"]
                )
                node["metadata_json"] = self._merge_metadata_json(
                    node.get("metadata_json"), metadata
                )

            related_nodes = {door_id}
            related_nodes.update(
                node_id
                for node_id, data in self.graph.nodes(data=True)
                if data.get("parent_node_id") == door_id
            )
            for node_id in related_nodes - {door_id}:
                node = self.graph.nodes[node_id]
                node.update(metadata)
                node["accessible_general"] = metadata["door_open"]
                node["accessible_wheelchair"] = bool(
                    node.get("accessible_wheelchair") is not False
                    and metadata["door_open"]
                )
                node["metadata_json"] = self._merge_metadata_json(
                    node.get("metadata_json"), metadata
                )

            for source, target, key, data in self.graph.edges(
                related_nodes, keys=True, data=True
            ):
                data.update(
                    inout=metadata["inout"],
                    inout_source=metadata["inout_source"],
                    door_status=metadata["door_status"],
                    door_open=metadata["door_open"],
                    door_status_source=metadata["door_status_source"],
                )
                data["metadata_json"] = self._merge_metadata_json(
                    data.get("metadata_json"), metadata
                )
                if not metadata["door_open"]:
                    data["accessible_general"] = False
                    data["accessible_wheelchair"] = False
                    data["restriction_reason"] = "door_closed"

    def assemble_space_and_door_graph(self) -> nx.MultiDiGraph:
        if not self.door_access_metadata:
            self._classify_door_access()
        graph = super().assemble_space_and_door_graph()
        self._apply_door_metadata_to_graph()
        return graph

    def apply_profile_costs(self) -> None:
        # Horizontal subgraphs are created after the initial space/door graph;
        # reapply V2 semantics so their door-side edges receive the same state.
        self._apply_door_metadata_to_graph()
        super().apply_profile_costs()
        for _, _, _, data in self.graph.edges(keys=True, data=True):
            if not data.get("accessible_general", True):
                data["cost_general"] = float("inf")
            if not data.get("accessible_wheelchair", False):
                data["cost_wheelchair"] = float("inf")

    def compute_route(
        self,
        source_node: str,
        target_node: str,
        user_profile: str = "general",
        cost_attribute: Optional[str] = None,
    ) -> dict[str, Any]:
        """Route while enforcing closed-door restrictions for every cost mode."""
        if source_node not in self.graph or target_node not in self.graph:
            raise KeyError("Source and target must be existing graph node IDs")
        profile = user_profile.casefold()
        if profile not in {"general", "wheelchair"}:
            raise ValueError("user_profile must be 'general' or 'wheelchair'")
        attribute = cost_attribute or f"cost_{profile}"
        access_attribute = f"accessible_{profile}"
        filtered = nx.MultiDiGraph()
        filtered.add_nodes_from(self.graph.nodes(data=True))
        for source, target, key, data in self.graph.edges(keys=True, data=True):
            if not data.get(access_attribute, profile == "general"):
                continue
            cost = float(data.get(attribute, data.get("estimated_time", 1.0)))
            if math.isfinite(cost):
                filtered.add_edge(
                    source, target, key=key, **data, _route_cost=cost
                )
        nodes = nx.shortest_path(
            filtered, source_node, target_node, weight="_route_cost"
        )
        edges: list[str] = []
        total = 0.0
        for source, target in zip(nodes, nodes[1:]):
            _, data = min(
                filtered[source][target].items(),
                key=lambda item: item[1]["_route_cost"],
            )
            edges.append(data["edge_id"])
            total += data["_route_cost"]
        return {
            "source": source_node,
            "target": target_node,
            "profile": profile,
            "node_ids": nodes,
            "edge_ids": edges,
            "total_cost": total,
        }

    def validate_graph(self) -> list[ValidationIssue]:
        issues_before = len(self.issues)
        issues = super().validate_graph()
        # Closed doors are intentional restrictions, not connectivity errors.
        # Emit an informational trace so downstream reviewers can distinguish
        # policy closures from missing or invalid IFC geometry.
        existing = {
            (issue.issue_type, issue.related_node_id) for issue in self.issues
        }
        for door_id, metadata in self.door_access_metadata.items():
            if metadata["door_status"] != "closed":
                continue
            key = ("door_closed_for_routing", door_id)
            if key in existing:
                continue
            door = self.doors[door_id]
            self._issue(
                "info",
                "door_closed_for_routing",
                f"Door {door.name} is closed and excluded from public routes",
                "Open the door in V2 configuration or IFC properties when access is allowed",
                related_ifc_guid=door.ifc_guid,
                related_node_id=door_id,
                geometry=door.point,
            )
        return self.issues if len(self.issues) >= issues_before else issues

    def export_geopackage(self, output_path: str | Path) -> Path:
        """Export V1 layers and add queryable V2 columns to the doors layer."""
        if not self.door_access_metadata:
            self._classify_door_access()
        for door_id, metadata in self.door_access_metadata.items():
            self.doors[door_id].properties.update(
                {
                    "HSIMG.inout": metadata["inout"],
                    "HSIMG.inout_source": metadata["inout_source"],
                    "HSIMG.door_status": metadata["door_status"],
                    "HSIMG.public_access": metadata["public_access"],
                    "HSIMG.door_status_source": metadata["door_status_source"],
                }
            )
        output = super().export_geopackage(output_path)
        with sqlite3.connect(output) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(doors)")
            }
        expected = {
            "inout", "door_status", "public_access", "door_status_source"
        }
        missing = expected - columns
        if missing:
            raise RuntimeError(
                "V2 door columns were not written during GeoPackage creation: "
                f"{sorted(missing)}"
            )
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        result.update(
            {
                "Exterior entrance/exit doors": sum(
                    metadata["inout"] == 1
                    for metadata in self.door_access_metadata.values()
                ),
                "Closed doors": sum(
                    metadata["door_status"] == "closed"
                    for metadata in self.door_access_metadata.values()
                ),
            }
        )
        return result


def print_summary(
    builder: HSIMGBuilder, geopackage_path: Optional[str | Path] = None
) -> None:
    summary = builder.summary()
    if geopackage_path is not None:
        summary["GeoPackage output path"] = str(geopackage_path)
    print("\n".join(f"{key}: {value}" for key, value in summary.items()))


__all__ = [
    "DoorRecord",
    "GeometryEngine",
    "HSIMGBuilder",
    "HSIMGConfig",
    "SpaceRecord",
    "StoreyRecord",
    "ValidationIssue",
    "VerticalMobilityRecord",
    "print_summary",
]
