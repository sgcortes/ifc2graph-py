"""Vector medial-axis extraction for walkable IfcSpace footprints.

The implementation samples polygon boundaries, builds a bounded Voronoi
diagram and retains only ridges fully covered by the walkable polygon.  Unlike
the legacy raster skeleton, its accuracy is not tied to a pixel grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Sequence

import networkx as nx
import numpy as np
from scipy.spatial import QhullError, Voronoi
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import linemerge, nearest_points, unary_union


@dataclass(slots=True)
class VectorAxisConfig:
    boundary_sample_spacing_m: float = 0.30
    minimum_branch_length_m: float = 0.50
    simplification_tolerance_m: float = 0.05
    containment_tolerance_m: float = 0.05
    minimum_hole_area_m2: float = 1.00
    snap_tolerance_m: float = 0.02
    maximum_boundary_samples: int = 20_000

    def __post_init__(self) -> None:
        for name in (
            "boundary_sample_spacing_m",
            "minimum_branch_length_m",
            "containment_tolerance_m",
            "snap_tolerance_m",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(slots=True)
class VectorAxisResult:
    lines: list[LineString]
    method: str = "vector_boundary_voronoi_medial_axis"
    boundary_samples: int = 0
    raw_ridges: int = 0
    retained_ridges: int = 0
    pruned_branches: int = 0
    component_connectors: int = 0
    connected_components: int = 0
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
            "connected_components": self.connected_components,
            "warnings": self.warnings,
        }


def _iter_lines(geometry: Any) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _iter_lines(part)


def _force_z(line: LineString, z: float) -> LineString:
    return LineString([(float(x), float(y), float(z)) for x, y, *_ in line.coords])


class VectorMedialAxisEngine:
    """Construct a connected vector centreline graph inside a polygon."""

    def __init__(self, config: VectorAxisConfig | None = None):
        self.config = config or VectorAxisConfig()

    def _clean_parts(self, polygon: Polygon | MultiPolygon) -> list[Polygon]:
        raw_parts = list(polygon.geoms) if isinstance(polygon, MultiPolygon) else [polygon]
        parts: list[Polygon] = []
        for part in raw_parts:
            holes = [
                ring.coords
                for ring in part.interiors
                if Polygon(ring).area >= self.config.minimum_hole_area_m2
            ]
            cleaned = Polygon(part.exterior.coords, holes)
            if cleaned.is_valid and cleaned.area > 1e-8:
                parts.append(cleaned)
        return parts

    def _sample_ring(self, ring: Any) -> list[tuple[float, float]]:
        line = LineString(ring.coords)
        spacing = self.config.boundary_sample_spacing_m
        distances = np.arange(0.0, line.length, spacing)
        points = [(float(line.interpolate(d).x), float(line.interpolate(d).y)) for d in distances]
        points.extend((float(x), float(y)) for x, y, *_ in ring.coords[:-1])
        return points

    def _boundary_samples(self, part: Polygon) -> np.ndarray:
        samples = self._sample_ring(part.exterior)
        for ring in part.interiors:
            samples.extend(self._sample_ring(ring))
        precision = max(0, int(math.ceil(-math.log10(self.config.snap_tolerance_m))) + 2)
        unique = list(dict.fromkeys((round(x, precision), round(y, precision)) for x, y in samples))
        if len(unique) > self.config.maximum_boundary_samples:
            step = int(math.ceil(len(unique) / self.config.maximum_boundary_samples))
            unique = unique[::step]
        return np.asarray(unique, dtype=float)

    def _voronoi_segments(
        self,
        part: Polygon,
        samples: np.ndarray,
    ) -> tuple[list[LineString], int]:
        if len(samples) < 4:
            return [], 0
        try:
            diagram = Voronoi(samples, qhull_options="Qbb Qc Qz")
        except QhullError:
            diagram = Voronoi(samples, qhull_options="Qbb Qc Qz QJ")
        domain = part.buffer(1e-7)
        segments: list[LineString] = []
        raw = 0
        for ridge in diagram.ridge_vertices:
            if len(ridge) != 2 or -1 in ridge:
                continue
            raw += 1
            a, b = diagram.vertices[ridge]
            segment = LineString([(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))])
            if segment.length <= self.config.snap_tolerance_m * 0.25:
                continue
            if domain.covers(segment) and part.covers(segment.interpolate(0.5, normalized=True)):
                segments.append(segment)
        return segments, raw

    def _merge_segments(self, segments: Sequence[LineString]) -> list[LineString]:
        if not segments:
            return []
        merged = linemerge(unary_union(list(segments)))
        return [line for line in _iter_lines(merged) if line.length > 1e-8]

    def _node_key(self, coordinate: Sequence[float]) -> tuple[int, int]:
        snap = self.config.snap_tolerance_m
        return (round(float(coordinate[0]) / snap), round(float(coordinate[1]) / snap))

    def _protected_lines(
        self,
        lines: Sequence[LineString],
        access_points: Sequence[Point],
    ) -> set[int]:
        protected: set[int] = set()
        for point in access_points:
            if not lines:
                break
            protected.add(min(range(len(lines)), key=lambda index: point.distance(lines[index])))
        return protected

    def _prune_short_leaves(
        self,
        lines: list[LineString],
        access_points: Sequence[Point],
    ) -> tuple[list[LineString], int]:
        active = list(lines)
        pruned = 0
        while active:
            graph = nx.Graph()
            for index, line in enumerate(active):
                graph.add_edge(
                    self._node_key(line.coords[0]),
                    self._node_key(line.coords[-1]),
                    index=index,
                )
            protected = self._protected_lines(active, access_points)
            remove = {
                data["index"]
                for source, target, data in graph.edges(data=True)
                if data["index"] not in protected
                and active[data["index"]].length < self.config.minimum_branch_length_m
                and (graph.degree[source] == 1 or graph.degree[target] == 1)
            }
            if not remove:
                break
            pruned += len(remove)
            active = [line for index, line in enumerate(active) if index not in remove]
        return active, pruned

    def _safe_simplify(self, line: LineString, part: Polygon) -> LineString:
        simplified = line.simplify(self.config.simplification_tolerance_m)
        domain = part.buffer(1e-7)
        return simplified if domain.covers(simplified) else line

    def _visibility_path(
        self,
        start: Point,
        end: Point,
        part: Polygon,
    ) -> list[LineString]:
        """Return a shortest boundary-aware connector inside ``part``."""
        simplified = part.simplify(
            max(self.config.simplification_tolerance_m, 0.10),
            preserve_topology=True,
        )
        rings = [simplified.exterior, *simplified.interiors]
        coordinates = [
            (float(x), float(y))
            for ring in rings
            for x, y, *_ in ring.coords[:-1]
        ]
        if len(coordinates) > 300:
            step = int(math.ceil(len(coordinates) / 300))
            coordinates = coordinates[::step]
        coordinates = [
            (float(start.x), float(start.y)),
            (float(end.x), float(end.y)),
            *coordinates,
        ]
        coordinates = list(dict.fromkeys(coordinates))
        graph = nx.Graph()
        domain = part.buffer(1e-7)
        for first in range(len(coordinates)):
            for second in range(first + 1, len(coordinates)):
                segment = LineString([coordinates[first], coordinates[second]])
                if domain.covers(segment):
                    graph.add_edge(first, second, weight=segment.length)
        try:
            path = nx.shortest_path(graph, 0, 1, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []
        return [
            LineString([coordinates[source], coordinates[target]])
            for source, target in zip(path, path[1:])
        ]

    def _line_components(self, lines: Sequence[LineString]) -> list[set[int]]:
        graph = nx.Graph()
        for index, line in enumerate(lines):
            graph.add_edge(
                self._node_key(line.coords[0]),
                self._node_key(line.coords[-1]),
                line_index=index,
            )
        components: list[set[int]] = []
        for nodes in nx.connected_components(graph):
            components.append({
                data["line_index"]
                for source, target, data in graph.edges(nodes, data=True)
                if source in nodes and target in nodes
            })
        return components

    def _connect_visible_components(
        self,
        lines: list[LineString],
        part: Polygon,
    ) -> tuple[list[LineString], int]:
        """Bridge numerical gaps without creating shortcuts through boundaries."""
        connected = list(lines)
        added = 0
        # A very small numerical buffer is sufficient here. Using the general
        # containment tolerance would shrink small holes and could permit a
        # connector to cut through a column.
        domain = part.buffer(1e-7)
        # Join every visible component to the largest one in a small number of
        # batches. This is linear per pass and avoids an O(k³) all-pairs loop in
        # spaces containing many small voids.
        for _ in range(4):
            components = self._line_components(connected)
            if len(components) <= 1 or added >= 64:
                break
            component_geometries = [
                unary_union([connected[index] for index in indexes])
                for indexes in components
            ]
            anchor_index = max(
                range(len(components)),
                key=lambda index: component_geometries[index].length,
            )
            anchor = component_geometries[anchor_index]
            connectors: list[LineString] = []
            for index, geometry in enumerate(component_geometries):
                if index == anchor_index:
                    continue
                start, end = nearest_points(anchor, geometry)
                connector = LineString([start, end])
                if (
                    connector.length > 1e-9
                    and domain.covers(connector)
                ):
                    connectors.append(connector)
                elif connector.length > 1e-9:
                    connectors.extend(self._visibility_path(start, end, part))
                if added + len(connectors) >= 64:
                        break
            if not connectors:
                break
            connected = self._merge_segments([*connected, *connectors])
            added += len(connectors)
        return connected, added

    def build(
        self,
        polygon: Polygon | MultiPolygon,
        z: float,
        access_points: Sequence[Point] = (),
    ) -> VectorAxisResult:
        result = VectorAxisResult(lines=[])
        all_lines: list[LineString] = []
        raw_parts = list(polygon.geoms) if isinstance(polygon, MultiPolygon) else [polygon]
        for original_part in raw_parts:
            cleaned_parts = self._clean_parts(original_part)
            if not cleaned_parts:
                continue
            part = cleaned_parts[0]
            samples = self._boundary_samples(part)
            result.boundary_samples += len(samples)
            segments, raw = self._voronoi_segments(part, samples)
            result.raw_ridges += raw
            result.retained_ridges += len(segments)
            merged = self._merge_segments(segments)
            part_access = [point for point in access_points if part.buffer(1e-6).covers(point)]
            pruned, count = self._prune_short_leaves(merged, part_access)
            result.pruned_branches += count
            walkable_part = original_part if original_part.is_valid else original_part.buffer(0)
            clipped: list[LineString] = []
            for line in pruned:
                clipped.extend(
                    candidate
                    for candidate in _iter_lines(line.intersection(walkable_part))
                    if candidate.length > 1e-8
                )
            clipped = self._merge_segments(clipped)
            connected, connector_count = self._connect_visible_components(
                clipped,
                walkable_part,
            )
            result.component_connectors += connector_count
            all_lines.extend(
                _force_z(self._safe_simplify(line, walkable_part), z)
                for line in connected
            )

        result.lines = all_lines
        if all_lines:
            graph = nx.Graph()
            for line in all_lines:
                graph.add_edge(self._node_key(line.coords[0]), self._node_key(line.coords[-1]))
            result.connected_components = nx.number_connected_components(graph)
            if result.connected_components > 1:
                result.warnings.append(
                    f"Medial axis retains {result.connected_components} bounded components"
                )
        else:
            result.warnings.append("No bounded Voronoi medial ridges were produced")
        return result


__all__ = [
    "VectorAxisConfig",
    "VectorAxisResult",
    "VectorMedialAxisEngine",
]
