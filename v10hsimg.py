"""HSIMG V10: clearance-safe preservation of continuous corridor backbones.

V10 keeps V9's conservative door, elevator and cross-space semantics while
fixing a destructive topological side effect of V6 pruning.  A local clearance
rejection can split an otherwise walkable annular corridor; V6 then recursively
removed the resulting long open branches.  V10 only removes bounded short
spurs, preserves substantial accessless components for repair, and reconnects
components through a boundary-aware path wholly contained in the eroded
walkable domain.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import networkx as nx
from shapely.geometry import LineString, Point, Polygon

import v9hsimg as v9


@dataclass(slots=True)
class HSIMGConfig(v9.HSIMGConfig):
    preserve_long_accessless_components: bool = True
    accessless_component_max_prune_length_m: float = 2.0
    dead_end_max_prune_length_m: float = 1.5
    recover_clearance_domain_backbones: bool = True
    clearance_backbone_max_path_length_m: float = 80.0
    clearance_backbone_max_repairs_per_space: int = 32
    clearance_backbone_visibility_vertices: int = 160
    walkable_region_min_area_m2: float = 1.0
    walkable_region_node_tolerance_m: float = 0.02

    def __post_init__(self) -> None:
        super().__post_init__()
        for field_name in (
            "accessless_component_max_prune_length_m",
            "dead_end_max_prune_length_m",
            "clearance_backbone_max_path_length_m",
            "walkable_region_min_area_m2",
            "walkable_region_node_tolerance_m",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.clearance_backbone_max_repairs_per_space < 0:
            raise ValueError(
                "clearance_backbone_max_repairs_per_space must be non-negative"
            )
        if self.clearance_backbone_visibility_vertices < 16:
            raise ValueError(
                "clearance_backbone_visibility_vertices must be at least 16"
            )


class HSIMGBuilder(v9.HSIMGBuilder):
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
            raise TypeError("V10 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.preserved_long_accessless_components = 0
        self.bounded_dead_end_nodes_removed = 0
        self.clearance_backbone_repairs_added = 0
        self.clearance_backbone_spaces_repaired = 0
        self.clearance_backbone_candidates_rejected = 0
        self.walkable_regions_validated = 0
        self.walkable_regions_without_graph = 0
        self.fragmented_walkable_regions = 0
        self._visibility_graph_cache: dict[bytes, tuple[list[tuple[float, float]], nx.Graph, Any]] = {}

    @staticmethod
    def _polygon_parts(geometry: Any) -> list[Polygon]:
        if isinstance(geometry, Polygon):
            return [geometry] if not geometry.is_empty else []
        if hasattr(geometry, "geoms"):
            return [
                part
                for part in geometry.geoms
                if isinstance(part, Polygon) and not part.is_empty
            ]
        return []

    def _general_clearance_domain(self, space: Any) -> Any:
        domain = v9.v8.v7.v6.v5.cleaned_clearance_domain(
            space.footprint,
            self.config.medial_axis_min_hole_area_m2,
        )
        return domain.buffer(-0.5 * self.config.general_min_route_width_m)

    @staticmethod
    def _undirected_edge_length(graph: nx.Graph, source: str, target: str) -> float:
        return float(graph.get_edge_data(source, target, {}).get("weight", 0.0))

    def _prune_accessless_components(self) -> int:
        """Remove only small accessless artifacts; retain long walkable pieces."""
        if not self.config.prune_accessless_horizontal_components:
            return 0
        horizontal_ids = {
            row.get("subgraph_id")
            for row in self.subgraphs
            if row.get("subgraph_type") == "horizontal_mobility"
        }
        local_by_subgraph = {subgraph_id: nx.Graph() for subgraph_id in horizontal_ids}
        for source, target, data in self.graph.edges(data=True):
            subgraph_id = data.get("subgraph_id")
            if subgraph_id not in local_by_subgraph or data.get("edge_type") not in {
                "internal_axis",
                "component_connector",
                "door_axis_projection",
            }:
                continue
            length = self._edge_length(data)
            previous = local_by_subgraph[subgraph_id].get_edge_data(
                source, target, {}
            ).get("weight")
            if previous is None or length < previous:
                local_by_subgraph[subgraph_id].add_edge(
                    source, target, weight=length
                )
        for node_id, data in self.graph.nodes(data=True):
            subgraph_id = data.get("subgraph_id")
            if subgraph_id in local_by_subgraph:
                local_by_subgraph[subgraph_id].add_node(node_id)

        removed_components = 0
        nodes_to_remove: set[str] = set()
        for local in local_by_subgraph.values():
            for component in nx.connected_components(local):
                has_access = any(
                    self.graph.nodes[node_id].get("node_role") in {
                        "door_side",
                        "door_projection",
                        "elevator_opening_hall_side",
                    }
                    or self.graph.nodes[node_id].get("node_type") == "door_access"
                    for node_id in component
                )
                if has_access:
                    continue
                subgraph = local.subgraph(component)
                length = sum(
                    float(data.get("weight", 0.0))
                    for _, _, data in subgraph.edges(data=True)
                )
                if (
                    self.config.preserve_long_accessless_components
                    and length > self.config.accessless_component_max_prune_length_m
                ):
                    self.preserved_long_accessless_components += 1
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

    def _functional_horizontal_targets(self) -> set[str]:
        protected_roles = {
            "door_projection",
            "door_side",
            "elevator_opening_hall_side",
            "landing",
        }
        protected = {
            node_id
            for node_id, data in self.graph.nodes(data=True)
            if data.get("node_role") in protected_roles
        }
        axis_types = {"internal_axis", "component_connector"}
        for source, target, data in self.graph.edges(data=True):
            if data.get("edge_type") not in axis_types:
                protected.update((source, target))
            if (
                data.get("relation_source")
                == "clearance_domain_visibility_backbone_v10"
            ):
                protected.update((source, target))
        return protected

    def _prune_dead_end_branches(self) -> int:
        """Prune a complete spur only when its total length is bounded."""
        if not self.config.prune_unprotected_dead_ends:
            return 0
        protected = self._functional_horizontal_targets()
        local_by_subgraph: dict[str, nx.Graph] = defaultdict(nx.Graph)
        for source, target, data in self.graph.edges(data=True):
            if data.get("edge_type") not in {"internal_axis", "component_connector"}:
                continue
            subgraph_id = data.get("subgraph_id")
            if not subgraph_id:
                continue
            length = self._edge_length(data)
            previous = local_by_subgraph[subgraph_id].get_edge_data(
                source, target, {}
            ).get("weight")
            if previous is None or length < previous:
                local_by_subgraph[subgraph_id].add_edge(
                    source, target, weight=length
                )

        remove: set[str] = set()
        threshold = self.config.dead_end_max_prune_length_m
        for local in local_by_subgraph.values():
            changed = True
            while changed:
                changed = False
                for leaf in sorted(
                    node_id
                    for node_id, degree in local.degree()
                    if degree <= 1 and node_id not in protected
                ):
                    if leaf not in local or local.degree(leaf) > 1:
                        continue
                    chain = [leaf]
                    total = 0.0
                    previous = None
                    current = leaf
                    while local.degree(current) > 0:
                        neighbors = [
                            node_id
                            for node_id in local.neighbors(current)
                            if node_id != previous
                        ]
                        if len(neighbors) != 1:
                            break
                        nxt = neighbors[0]
                        total += self._undirected_edge_length(local, current, nxt)
                        if nxt in protected or local.degree(nxt) != 2:
                            if local.degree(nxt) <= 1 and nxt not in protected:
                                chain.append(nxt)
                            break
                        chain.append(nxt)
                        previous, current = current, nxt
                    if total <= threshold + 1e-9:
                        removable = [
                            node_id
                            for node_id in chain
                            if node_id not in protected and node_id in local
                        ]
                        if removable:
                            local.remove_nodes_from(removable)
                            remove.update(removable)
                            changed = True
                            break
        existing = remove.intersection(self.graph.nodes)
        self.graph.remove_nodes_from(existing)
        self.pruned_internal_nodes += len(existing)
        self.pruned_unprotected_dead_end_nodes += len(existing)
        self.bounded_dead_end_nodes_removed += len(existing)
        return len(existing)

    def _visibility_path(
        self,
        start: Point,
        end: Point,
        part: Polygon,
    ) -> LineString | None:
        """Return the shortest obstacle-aware polyline inside a safe polygon."""
        direct = LineString([start.coords[0][:2], end.coords[0][:2]])
        domain = part.buffer(max(self.config.clearance_domain_tolerance_m, 1e-7))
        if domain.covers(direct):
            return direct
        cache = getattr(self, "_visibility_graph_cache", None)
        if cache is None:
            cache = {}
            self._visibility_graph_cache = cache
        key = part.wkb
        cached = cache.get(key)
        if cached is None:
            simplified = part.simplify(
                max(self.config.vector_simplification_tolerance_m, 0.05),
                preserve_topology=True,
            )
            boundary = list(dict.fromkeys(
                (float(x), float(y))
                for ring in [simplified.exterior, *simplified.interiors]
                for x, y, *_ in ring.coords[:-1]
            ))
            limit = self.config.clearance_backbone_visibility_vertices
            if len(boundary) > limit:
                step = max(1, int(math.ceil(len(boundary) / limit)))
                boundary = boundary[::step]
            base = nx.Graph()
            base.add_nodes_from(range(len(boundary)))
            for first in range(len(boundary)):
                for second in range(first + 1, len(boundary)):
                    segment = LineString([boundary[first], boundary[second]])
                    if domain.covers(segment):
                        base.add_edge(
                            first, second, weight=float(segment.length)
                        )
            cached = (boundary, base, domain)
            cache[key] = cached
        boundary, base, domain = cached
        start_key, end_key = -1, -2
        visibility = base.copy()
        endpoints = {
            start_key: (float(start.x), float(start.y)),
            end_key: (float(end.x), float(end.y)),
        }
        visibility.add_nodes_from(endpoints)
        for endpoint_key, endpoint in endpoints.items():
            for index, coordinate in enumerate(boundary):
                segment = LineString([endpoint, coordinate])
                if domain.covers(segment):
                    visibility.add_edge(
                        endpoint_key, index, weight=float(segment.length)
                    )
        try:
            indexes = nx.shortest_path(
                visibility, start_key, end_key, weight="weight"
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        coordinates = [
            endpoints[index] if index in endpoints else boundary[index]
            for index in indexes
        ]
        return LineString(coordinates)

    def _nearest_component_node_pairs(
        self,
        left: set[str],
        right: set[str],
        part: Polygon,
        limit: int = 6,
    ) -> list[tuple[float, str, str]]:
        tolerance = self.config.walkable_region_node_tolerance_m
        domain = part.buffer(tolerance)
        left_nodes = [
            node_id
            for node_id in left
            if domain.covers(self.graph.nodes[node_id].get("geometry"))
        ]
        right_nodes = [
            node_id
            for node_id in right
            if domain.covers(self.graph.nodes[node_id].get("geometry"))
        ]
        candidates = []
        for source in left_nodes:
            source_point = self.graph.nodes[source]["geometry"]
            for target in right_nodes:
                target_point = self.graph.nodes[target]["geometry"]
                candidates.append(
                    (float(source_point.distance(target_point)), source, target)
                )
        return sorted(candidates)[:limit]

    def _candidate_backbone_repair(
        self,
        left: set[str],
        right: set[str],
        part: Polygon,
        source_domain: Any,
    ) -> dict[str, Any] | None:
        for _, source, target in self._nearest_component_node_pairs(
            left, right, part
        ):
            source_point = self.graph.nodes[source]["geometry"]
            target_point = self.graph.nodes[target]["geometry"]
            path_2d = self._visibility_path(source_point, target_point, part)
            if path_2d is None or path_2d.length <= 1e-6:
                continue
            if path_2d.length > self.config.clearance_backbone_max_path_length_m:
                continue
            z = float(self.graph.nodes[source].get("z", 0.0))
            path = LineString([(x, y, z) for x, y, *_ in path_2d.coords])
            general_ok, wheelchair_ok, width = self._shortcut_clearance(
                path, source_domain
            )
            if not general_ok:
                self.clearance_backbone_candidates_rejected += 1
                continue
            return {
                "source": source,
                "target": target,
                "line": path,
                "length": float(path.length),
                "width": width,
                "wheelchair": wheelchair_ok,
            }
        return None

    def _recover_space_clearance_backbone(self, space: Any) -> int:
        local, _ = self._horizontal_space_graph(space.space_id)
        if local.number_of_nodes() < 2:
            return 0
        source_domain = v9.v8.v7.v6.v5.cleaned_clearance_domain(
            space.footprint,
            self.config.medial_axis_min_hole_area_m2,
        )
        safe_parts = [
            part
            for part in self._polygon_parts(
                source_domain.buffer(-0.5 * self.config.general_min_route_width_m)
            )
            if part.area >= self.config.walkable_region_min_area_m2
        ]
        added = 0
        subgraph_id = v9.v8.v7.core.stable_id(
            "subgraph", space.ifc_guid, "horizontal"
        )
        while added < self.config.clearance_backbone_max_repairs_per_space:
            components = list(nx.connected_components(local))
            if len(components) <= 1:
                break
            candidates = []
            for part in safe_parts:
                relevant = [
                    component
                    for component in components
                    if any(
                        part.buffer(
                            self.config.walkable_region_node_tolerance_m
                        ).covers(self.graph.nodes[node_id].get("geometry"))
                        for node_id in component
                    )
                ]
                for left_index, left in enumerate(relevant):
                    for right in relevant[left_index + 1:]:
                        candidate = self._candidate_backbone_repair(
                            left, right, part, source_domain
                        )
                        if candidate is not None:
                            candidates.append(candidate)
            if not candidates:
                break
            candidate = min(
                candidates,
                key=lambda item: (
                    item["length"],
                    -item["width"],
                    item["source"],
                    item["target"],
                ),
            )
            metadata = {
                "repair_method": "clearance_domain_visibility_backbone_v10",
                "path_length_m": candidate["length"],
                "minimum_route_width_m": candidate["width"],
                "general_min_route_width_m": self.config.general_min_route_width_m,
                "wheelchair_min_route_width_m": self.config.wheelchair_min_route_width_m,
                "same_parent_space_required": True,
                "curved_path_allowed": True,
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
                    None
                    if candidate["wheelchair"]
                    else "insufficient_wheelchair_clearance"
                ),
                relation_source="clearance_domain_visibility_backbone_v10",
                validation_status="valid_backbone_repair_v10",
                confidence=0.97,
                metadata=metadata,
            )
            local.add_edge(
                candidate["source"],
                candidate["target"],
                weight=candidate["length"],
            )
            added += 1
        if added:
            self._issue(
                "info",
                "clearance_backbone_repaired_v10",
                f"V10 added {added} safe backbone repairs to {space.name}",
                "No action required; every repair is contained in the eroded domain",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space.space_id,
                geometry=space.interior_point,
            )
        return added

    def _recover_clearance_domain_backbones(self) -> int:
        if not self.config.recover_clearance_domain_backbones:
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
            added = self._recover_space_clearance_backbone(space)
            total += added
            spaces += int(added > 0)
        self.clearance_backbone_repairs_added += total
        self.clearance_backbone_spaces_repaired += spaces
        return total

    def _prune_non_pedestrian_topology(self, horizontal_only: bool) -> None:
        if not self.config.prune_non_pedestrian_edges:
            return
        self._remove_inaccessible_edges(horizontal_only=horizontal_only)
        self._remove_isolated_derived_nodes()
        if horizontal_only:
            self._recover_clearance_domain_backbones()
        self._prune_accessless_components()
        self._prune_dead_end_branches()
        self._remove_isolated_derived_nodes()
        self._rebuild_horizontal_axes_from_graph()
        self._refresh_subgraph_counts()

    def _validate_walkable_region_coverage(self) -> None:
        missing = 0
        fragmented = 0
        validated = 0
        tolerance = self.config.walkable_region_node_tolerance_m
        for space in self.spaces.values():
            if (
                space.node_class != "horizontal_mobility"
                or space.footprint is None
                or space.footprint.is_empty
            ):
                continue
            local, _ = self._horizontal_space_graph(space.space_id)
            components = list(nx.connected_components(local))
            component_by_node = {
                node_id: index
                for index, component in enumerate(components)
                for node_id in component
            }
            for part in self._polygon_parts(self._general_clearance_domain(space)):
                if part.area < self.config.walkable_region_min_area_m2:
                    continue
                validated += 1
                node_ids = [
                    node_id
                    for node_id in local.nodes
                    if part.buffer(tolerance).covers(
                        self.graph.nodes[node_id].get("geometry")
                    )
                    and local.degree(node_id) > 0
                ]
                if not node_ids:
                    missing += 1
                    self._issue(
                        "error",
                        "walkable_region_without_graph_v10",
                        f"Walkable region in {space.name} has no horizontal graph",
                        "Regenerate a local centreline inside the eroded region",
                        related_ifc_guid=space.ifc_guid,
                        related_node_id=space.space_id,
                        geometry=part.representative_point(),
                    )
                    continue
                memberships = {component_by_node[node_id] for node_id in node_ids}
                if len(memberships) > 1:
                    fragmented += 1
                    self._issue(
                        "error",
                        "fragmented_walkable_region_v10",
                        f"Walkable region in {space.name} contains {len(memberships)} graph components",
                        "Repair the components with a clearance-contained path",
                        related_ifc_guid=space.ifc_guid,
                        related_node_id=space.space_id,
                        geometry=part.representative_point(),
                    )
        self.walkable_regions_validated = validated
        self.walkable_regions_without_graph = missing
        self.fragmented_walkable_regions = fragmented

    def validate_graph(self) -> list[Any]:
        issues = super().validate_graph()
        self._validate_walkable_region_coverage()
        for source, target, data in self.graph.edges(data=True):
            if data.get("relation_source") != "clearance_domain_visibility_backbone_v10":
                continue
            if (
                self.graph.nodes[source].get("parent_node_id")
                != self.graph.nodes[target].get("parent_node_id")
            ):
                self._issue(
                    "error",
                    "cross_space_backbone_repair_v10",
                    f"V10 repair {source} -> {target} crosses parent spaces",
                    "Remove the repair and review V10 candidate filtering",
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
            if "door_access_v9" in tables and "door_access_v10" not in tables:
                connection.execute(
                    "ALTER TABLE door_access_v9 RENAME TO door_access_v10"
                )
                connection.execute(
                    "UPDATE gpkg_contents SET table_name='door_access_v10', "
                    "identifier='door_access_v10', "
                    "description='V10 door, vertical and walkable-region validation' "
                    "WHERE table_name='door_access_v9'"
                )
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            connection.execute(
                "UPDATE gpkg_contents SET last_change=? "
                "WHERE table_name IN "
                "('graph_edges','graph_nodes','mobility_axes','validation_issues')",
                (now,),
            )
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        surviving_repairs = sum(
            data.get("relation_source")
            == "clearance_domain_visibility_backbone_v10"
            for _, _, data in self.graph.edges(data=True)
        ) // 2
        result.update({
            "HSIMG version": 10,
            "Version": 10,
            "Preserved long accessless components": (
                self.preserved_long_accessless_components
            ),
            "Bounded dead-end nodes removed": self.bounded_dead_end_nodes_removed,
            "Clearance backbone repairs": surviving_repairs,
            "Clearance backbone repair attempts": (
                self.clearance_backbone_repairs_added
            ),
            "Spaces with clearance backbone repairs": (
                self.clearance_backbone_spaces_repaired
            ),
            "Rejected clearance backbone candidates": (
                self.clearance_backbone_candidates_rejected
            ),
            "Walkable regions validated": self.walkable_regions_validated,
            "Walkable regions without graph": self.walkable_regions_without_graph,
            "Fragmented walkable regions": self.fragmented_walkable_regions,
        })
        return result


__all__ = ["HSIMGBuilder", "HSIMGConfig"]
