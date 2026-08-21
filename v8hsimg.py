"""HSIMG V8: clearance-validated shortcuts for simplified medial axes.

V8 preserves the V7 IFC/elevator semantics and repairs excessive detours that
can be introduced when an obstacle-aware medial axis is simplified.  A
shortcut is accepted only between nodes of the same horizontal mobility space,
when the existing graph route is substantially longer than the direct line and
the complete line is supported by the configured pedestrian clearance domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import networkx as nx
from shapely.geometry import LineString

import v7hsimg as v7


@dataclass(slots=True)
class HSIMGConfig(v7.HSIMGConfig):
    """V8 controls for conservative medial-axis shortcut recovery."""

    recover_medial_axis_shortcuts: bool = True
    horizontal_shortcut_max_length_m: float = 12.0
    horizontal_shortcut_min_stretch_ratio: float = 1.75
    horizontal_shortcut_min_saving_m: float = 3.0
    horizontal_shortcut_max_per_space: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        for field_name in (
            "horizontal_shortcut_max_length_m",
            "horizontal_shortcut_min_stretch_ratio",
            "horizontal_shortcut_min_saving_m",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.horizontal_shortcut_min_stretch_ratio <= 1.0:
            raise ValueError(
                "horizontal_shortcut_min_stretch_ratio must be greater than 1"
            )
        if self.horizontal_shortcut_max_per_space < 0:
            raise ValueError(
                "horizontal_shortcut_max_per_space must be non-negative"
            )


class HSIMGBuilder(v7.HSIMGBuilder):
    """V8 builder with auditable, clearance-safe horizontal shortcuts."""

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
            raise TypeError("V8 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.horizontal_shortcuts_added = 0
        self.horizontal_shortcut_spaces = 0
        self.horizontal_shortcut_candidates_rejected_clearance = 0
        self.horizontal_shortcut_total_saving_m = 0.0

    @staticmethod
    def _edge_length(data: Mapping[str, Any]) -> float:
        value = data.get("length_3d")
        if value is not None:
            return float(value)
        geometry = data.get("geometry")
        return float(geometry.length) if geometry is not None else 1.0

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
            and data.get("node_role") in {"axis_endpoint", "door_projection"}
        ]
        node_set = set(node_ids)
        local = nx.Graph()
        local.add_nodes_from(node_ids)
        for source, target, data in self.graph.edges(data=True):
            if source not in node_set or target not in node_set:
                continue
            if data.get("edge_type") not in {"internal_axis", "component_connector"}:
                continue
            length = self._edge_length(data)
            previous = local.get_edge_data(source, target, {}).get("weight")
            if previous is None or length < previous:
                local.add_edge(source, target, weight=length)
        return local, node_ids

    def _shortcut_clearance(
        self,
        line: LineString,
        domain: Any,
    ) -> tuple[bool, bool, float]:
        width = v7.v6.v5.minimum_route_width(
            line,
            domain,
            self.config.route_width_sample_spacing_m,
            self.config.route_width_trim_ratio,
        )
        general_domain = domain.buffer(
            -0.5 * self.config.general_min_route_width_m
        )
        wheelchair_domain = domain.buffer(
            -0.5 * self.config.wheelchair_min_route_width_m
        )
        general_ok = bool(
            width >= self.config.general_min_route_width_m
            and v7.v6.line_covered_by_eroded_domain(
                line,
                general_domain,
                self.config.route_width_trim_ratio,
                self.config.clearance_domain_tolerance_m,
            )
        )
        wheelchair_ok = bool(
            general_ok
            and width >= self.config.wheelchair_min_route_width_m
            and v7.v6.line_covered_by_eroded_domain(
                line,
                wheelchair_domain,
                self.config.route_width_trim_ratio,
                self.config.clearance_domain_tolerance_m,
            )
        )
        return general_ok, wheelchair_ok, float(width)

    def _candidate_shortcuts(
        self,
        local: nx.Graph,
        node_ids: list[str],
        domain: Any,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        distances_by_source: dict[str, dict[str, float]] = {}
        for index, source in enumerate(node_ids):
            source_point = self.graph.nodes[source].get("geometry")
            if source_point is None:
                continue
            for target in node_ids[index + 1:]:
                if local.has_edge(source, target):
                    continue
                target_point = self.graph.nodes[target].get("geometry")
                if target_point is None:
                    continue
                direct = math.hypot(
                    float(target_point.x) - float(source_point.x),
                    float(target_point.y) - float(source_point.y),
                )
                if direct <= 1e-6 or direct > self.config.horizontal_shortcut_max_length_m:
                    continue
                if source not in distances_by_source:
                    distances_by_source[source] = nx.single_source_dijkstra_path_length(
                        local,
                        source,
                        weight="weight",
                    )
                existing = distances_by_source[source].get(target)
                if existing is None:
                    # V8 repairs excessive detours, not disconnected components.
                    continue
                saving = existing - direct
                stretch = existing / direct
                if (
                    saving < self.config.horizontal_shortcut_min_saving_m
                    or stretch < self.config.horizontal_shortcut_min_stretch_ratio
                ):
                    continue
                z = float(self.graph.nodes[source].get("z", source_point.z or 0.0))
                line = LineString([
                    (float(source_point.x), float(source_point.y), z),
                    (float(target_point.x), float(target_point.y), z),
                ])
                general_ok, wheelchair_ok, width = self._shortcut_clearance(
                    line,
                    domain,
                )
                if not general_ok:
                    self.horizontal_shortcut_candidates_rejected_clearance += 1
                    continue
                candidates.append({
                    "source": source,
                    "target": target,
                    "line": line,
                    "direct": direct,
                    "existing": existing,
                    "saving": saving,
                    "stretch": stretch,
                    "width": width,
                    "wheelchair": wheelchair_ok,
                })
        return sorted(
            candidates,
            key=lambda item: (-item["saving"], -item["stretch"], item["source"], item["target"]),
        )

    def _recover_space_shortcuts(self, space: Any) -> int:
        local, node_ids = self._horizontal_space_graph(space.space_id)
        if len(node_ids) < 2 or local.number_of_edges() == 0:
            return 0
        domain = v7.v6.v5.cleaned_clearance_domain(
            space.footprint,
            self.config.medial_axis_min_hole_area_m2,
        )
        candidates = self._candidate_shortcuts(local, node_ids, domain)
        added = 0
        subgraph_id = v7.core.stable_id("subgraph", space.ifc_guid, "horizontal")
        for candidate in candidates:
            if added >= self.config.horizontal_shortcut_max_per_space:
                break
            source, target = candidate["source"], candidate["target"]
            try:
                current = nx.shortest_path_length(
                    local,
                    source,
                    target,
                    weight="weight",
                )
            except nx.NetworkXNoPath:
                continue
            direct = candidate["direct"]
            saving = current - direct
            if (
                saving < self.config.horizontal_shortcut_min_saving_m
                or current / direct < self.config.horizontal_shortcut_min_stretch_ratio
            ):
                continue
            metadata = {
                "shortcut_method": "clearance_validated_medial_axis_stretch_v8",
                "pre_shortcut_distance_m": current,
                "direct_distance_m": direct,
                "distance_saving_m": saving,
                "pre_shortcut_stretch_ratio": current / direct,
                "minimum_route_width_m": candidate["width"],
                "general_min_route_width_m": self.config.general_min_route_width_m,
                "wheelchair_min_route_width_m": self.config.wheelchair_min_route_width_m,
                "same_parent_space_required": True,
            }
            self._add_bidirectional_edge(
                source,
                target,
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
                relation_source="clearance_validated_visibility_shortcut_v8",
                validation_status="valid_shortcut_v8",
                confidence=0.90,
                metadata=metadata,
            )
            local.add_edge(source, target, weight=direct)
            added += 1
            self.horizontal_shortcut_total_saving_m += saving
        if added:
            self._issue(
                "info",
                "medial_axis_shortcuts_added_v8",
                f"V8 added {added} clearance-validated shortcuts to {space.name}",
                "No action required; inspect shortcut provenance for routing QA",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        return added

    def _recover_horizontal_visibility_shortcuts(self) -> int:
        if not self.config.recover_medial_axis_shortcuts:
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
            added = self._recover_space_shortcuts(space)
            total += added
            spaces += int(added > 0)
        self.horizontal_shortcuts_added += total
        self.horizontal_shortcut_spaces += spaces
        if total:
            self._rebuild_horizontal_axes_from_graph()
            self._refresh_subgraph_counts()
        return total

    def build_horizontal_subgraphs(self) -> list[dict[str, Any]]:
        result = super().build_horizontal_subgraphs()
        self._recover_horizontal_visibility_shortcuts()
        return result

    def validate_graph(self) -> list[Any]:
        issues = super().validate_graph()
        for source, target, data in self.graph.edges(data=True):
            if data.get("relation_source") != "clearance_validated_visibility_shortcut_v8":
                continue
            if self.graph.nodes[source].get("parent_node_id") != self.graph.nodes[target].get("parent_node_id"):
                self._issue(
                    "error",
                    "cross_space_shortcut_v8",
                    f"V8 shortcut {source} -> {target} crosses parent spaces",
                    "Remove the shortcut and review V8 candidate filtering",
                    related_node_id=source,
                    geometry=data.get("geometry"),
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
            if "door_access_v7" in tables and "door_access_v8" not in tables:
                connection.execute(
                    "ALTER TABLE door_access_v7 RENAME TO door_access_v8"
                )
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v8', "
                    "identifier='door_access_v8', "
                    "description='V8 door, elevator and shortcut validation' "
                    "WHERE table_name='door_access_v7'"
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
            "HSIMG version": 8,
            "Version": 8,
            "Clearance-validated medial-axis shortcuts": self.horizontal_shortcuts_added,
            "Spaces with recovered medial-axis shortcuts": self.horizontal_shortcut_spaces,
            "Shortcut candidates rejected by clearance": self.horizontal_shortcut_candidates_rejected_clearance,
            "Estimated shortcut distance saving (m)": round(self.horizontal_shortcut_total_saving_m, 3),
        })
        return result


def print_summary(builder: HSIMGBuilder, geopackage_path: Any = None) -> None:
    summary = builder.summary()
    if geopackage_path is not None:
        summary["GeoPackage output path"] = str(geopackage_path)
    print("\n".join(f"{key}: {value}" for key, value in summary.items()))


__all__ = ["HSIMGBuilder", "HSIMGConfig", "print_summary"]
