"""Obstacle-aware vector medial axes for HSIMG V4.

V4 fixes the V3 mismatch where small holes were ignored by the Voronoi
calculation and then reintroduced during clipping.  A single cleaned walkable
polygon is now used throughout the pipeline.  Realistic column-sized holes
are retained, triangulation slivers are removed, coordinates are snapped to a
precision grid, and component repair is restricted to short local gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import networkx as nx
from shapely import set_precision
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import nearest_points, unary_union

from v3vector import (
    VectorAxisConfig as V3VectorAxisConfig,
    VectorMedialAxisEngine as V3VectorMedialAxisEngine,
    _force_z,
)


@dataclass(slots=True)
class VectorAxisConfig(V3VectorAxisConfig):
    """V4 vector-axis controls.

    ``minimum_hole_area_m2`` separates plausible fixed obstacles from tiny
    mesh slivers.  The default retains a 0.20 x 0.25 m obstacle while removing
    the millimetric holes observed in projected IfcSpace meshes.
    """

    minimum_hole_area_m2: float = 0.05
    minimum_edge_length_m: float = 0.05
    maximum_component_connector_length_m: float = 1.00
    maximum_component_connectors: int = 32

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.minimum_hole_area_m2 < 0:
            raise ValueError("minimum_hole_area_m2 must be non-negative")
        if self.minimum_edge_length_m <= 0:
            raise ValueError("minimum_edge_length_m must be positive")
        if self.maximum_component_connector_length_m <= 0:
            raise ValueError(
                "maximum_component_connector_length_m must be positive"
            )
        if self.maximum_component_connectors < 0:
            raise ValueError("maximum_component_connectors must be non-negative")


@dataclass(slots=True)
class VectorAxisResult:
    lines: list[LineString]
    line_sources: list[str] = field(default_factory=list)
    method: str = "vector_obstacle_aware_medial_axis_v4"
    boundary_samples: int = 0
    raw_ridges: int = 0
    retained_ridges: int = 0
    pruned_branches: int = 0
    component_connectors: int = 0
    component_connector_lengths_m: list[float] = field(default_factory=list)
    connected_components: int = 0
    input_holes: int = 0
    retained_obstacle_holes: int = 0
    removed_artifact_holes: int = 0
    removed_artifact_hole_area_m2: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "boundary_samples": self.boundary_samples,
            "raw_ridges": self.raw_ridges,
            "retained_ridges": self.retained_ridges,
            "pruned_branches": self.pruned_branches,
            "component_connectors": self.component_connectors,
            "component_connector_lengths_m": self.component_connector_lengths_m,
            "connected_components": self.connected_components,
            "input_holes": self.input_holes,
            "retained_obstacle_holes": self.retained_obstacle_holes,
            "removed_artifact_holes": self.removed_artifact_holes,
            "removed_artifact_hole_area_m2": self.removed_artifact_hole_area_m2,
            "warnings": self.warnings,
        }


class VectorMedialAxisEngine(V3VectorMedialAxisEngine):
    """Build a bounded, obstacle-aware and topology-stable medial axis."""

    AXIS_SOURCE = "vector_obstacle_aware_medial_axis_v4"
    CONNECTOR_SOURCE = "bounded_component_connector_v4"

    def __init__(self, config: VectorAxisConfig | None = None):
        super().__init__(config or VectorAxisConfig())
        self.config: VectorAxisConfig
        self.completed_results: list[VectorAxisResult] = []

    def _clean_part(self, part: Polygon) -> tuple[Polygon | None, dict[str, float | int]]:
        retained = []
        removed_area = 0.0
        for ring in part.interiors:
            area = float(Polygon(ring).area)
            if area >= self.config.minimum_hole_area_m2:
                retained.append(ring.coords)
            else:
                removed_area += area
        cleaned = Polygon(part.exterior.coords, retained)
        if not cleaned.is_valid:
            repaired = cleaned.buffer(0)
            if isinstance(repaired, Polygon):
                cleaned = repaired
            else:
                return None, {
                    "input_holes": len(part.interiors),
                    "retained_holes": len(retained),
                    "removed_holes": len(part.interiors) - len(retained),
                    "removed_area": removed_area,
                }
        if cleaned.is_empty or cleaned.area <= 1e-8:
            return None, {
                "input_holes": len(part.interiors),
                "retained_holes": len(retained),
                "removed_holes": len(part.interiors) - len(retained),
                "removed_area": removed_area,
            }
        return cleaned, {
            "input_holes": len(part.interiors),
            "retained_holes": len(retained),
            "removed_holes": len(part.interiors) - len(retained),
            "removed_area": removed_area,
        }

    def _merge_segments(self, segments: Sequence[LineString]) -> list[LineString]:
        """Snap before noding so centimetric near-duplicates become one node."""
        snapped = []
        for line in segments:
            if line.is_empty:
                continue
            candidate = set_precision(line, self.config.snap_tolerance_m)
            if isinstance(candidate, LineString) and not candidate.is_empty:
                snapped.append(candidate)
            elif hasattr(candidate, "geoms"):
                snapped.extend(
                    part
                    for part in candidate.geoms
                    if isinstance(part, LineString) and not part.is_empty
                )
        merged = super()._merge_segments(snapped)
        return [
            line
            for line in merged
            if line.length >= self.config.minimum_edge_length_m
        ]

    def _component_geometries(
        self,
        lines: Sequence[LineString],
    ) -> list[Any]:
        return [
            unary_union([lines[index] for index in indexes])
            for indexes in self._line_components(lines)
        ]

    def _connect_near_components(
        self,
        lines: list[LineString],
        part: Polygon,
    ) -> tuple[list[LineString], list[LineString]]:
        """Join only the closest local numerical gaps.

        V3 connected every component to the largest component, which could
        introduce long chords across open halls.  V4 repeatedly selects the
        globally shortest visible gap and refuses connectors above a strict
        distance limit.
        """
        connected = list(lines)
        connectors: list[LineString] = []
        domain = part.buffer(1e-7)
        while len(connectors) < self.config.maximum_component_connectors:
            geometries = self._component_geometries(connected)
            if len(geometries) <= 1:
                break
            candidates = []
            for first in range(len(geometries)):
                for second in range(first + 1, len(geometries)):
                    start, end = nearest_points(
                        geometries[first], geometries[second]
                    )
                    distance = float(start.distance(end))
                    if distance <= 1e-9:
                        continue
                    if distance > self.config.maximum_component_connector_length_m:
                        continue
                    connector = LineString([start, end])
                    if domain.covers(connector):
                        candidates.append((distance, first, second, connector))
            if not candidates:
                break
            _, _, _, connector = min(
                candidates,
                key=lambda item: (item[0], item[1], item[2]),
            )
            connector = set_precision(connector, self.config.snap_tolerance_m)
            if not isinstance(connector, LineString) or connector.is_empty:
                break
            connectors.append(connector)
            connected = self._merge_segments([*connected, connector])
        return connected, connectors

    def _classify_lines(
        self,
        lines: Sequence[LineString],
        connectors: Sequence[LineString],
    ) -> list[str]:
        if not connectors:
            return [self.AXIS_SOURCE for _ in lines]
        connector_geometry = unary_union(connectors)
        tolerance = max(self.config.snap_tolerance_m * 0.25, 1e-6)
        buffered = connector_geometry.buffer(tolerance)
        sources = []
        for line in lines:
            overlap = float(line.intersection(buffered).length)
            ratio = overlap / max(float(line.length), 1e-9)
            sources.append(
                self.CONNECTOR_SOURCE if ratio >= 0.50 else self.AXIS_SOURCE
            )
        return sources

    def build(
        self,
        polygon: Polygon | MultiPolygon,
        z: float,
        access_points: Sequence[Point] = (),
    ) -> VectorAxisResult:
        result = VectorAxisResult(lines=[])
        all_lines: list[LineString] = []
        all_sources: list[str] = []
        raw_parts = (
            list(polygon.geoms) if isinstance(polygon, MultiPolygon) else [polygon]
        )
        for original_part in raw_parts:
            part, hole_stats = self._clean_part(original_part)
            result.input_holes += int(hole_stats["input_holes"])
            result.retained_obstacle_holes += int(hole_stats["retained_holes"])
            result.removed_artifact_holes += int(hole_stats["removed_holes"])
            result.removed_artifact_hole_area_m2 += float(
                hole_stats["removed_area"]
            )
            if part is None:
                continue

            samples = self._boundary_samples(part)
            result.boundary_samples += len(samples)
            segments, raw = self._voronoi_segments(part, samples)
            result.raw_ridges += raw
            result.retained_ridges += len(segments)
            merged = self._merge_segments(segments)
            part_access = [
                point for point in access_points if part.buffer(1e-6).covers(point)
            ]
            pruned, count = self._prune_short_leaves(merged, part_access)
            result.pruned_branches += count
            pruned = self._merge_segments(pruned)

            connected, connectors = self._connect_near_components(pruned, part)
            result.component_connectors += len(connectors)
            result.component_connector_lengths_m.extend(
                round(float(line.length), 6) for line in connectors
            )
            sources = self._classify_lines(connected, connectors)
            for line, source in zip(connected, sources):
                simplified = self._safe_simplify(line, part)
                if simplified.length < self.config.minimum_edge_length_m:
                    continue
                all_lines.append(_force_z(simplified, z))
                all_sources.append(source)

        result.lines = all_lines
        result.line_sources = all_sources
        if all_lines:
            graph = nx.Graph()
            for line in all_lines:
                graph.add_edge(
                    self._node_key(line.coords[0]),
                    self._node_key(line.coords[-1]),
                )
            result.connected_components = nx.number_connected_components(graph)
            if result.connected_components > 1:
                result.warnings.append(
                    "V4 retained "
                    f"{result.connected_components} components; long artificial "
                    "connectors were deliberately not created"
                )
        else:
            result.warnings.append("No bounded V4 medial ridges were produced")
        if result.removed_artifact_holes:
            result.warnings.append(
                f"Removed {result.removed_artifact_holes} sub-threshold mesh holes "
                f"({result.removed_artifact_hole_area_m2:.3f} m2)"
            )
        self.completed_results.append(result)
        return result


__all__ = [
    "VectorAxisConfig",
    "VectorAxisResult",
    "VectorMedialAxisEngine",
]
