"""Version 3 unified IFC-to-HSIMG processing module.

V3 merges the base graph pipeline and V2 door/access semantics behind one public
builder. Horizontal mobility subgraphs use a bounded vector Voronoi medial axis
instead of a raster skeleton, with an explicitly diagnosed raster fallback.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from itertools import combinations
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Iterator, Literal, Mapping, Optional, Sequence

import numpy as np

try:
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element
    import ifcopenshell.util.placement
except ImportError as exc:  # pragma: no cover - reported clearly in Colab
    raise ImportError("Install ifcopenshell before importing hsimg") from exc

try:
    import networkx as nx
except ImportError as exc:  # pragma: no cover
    raise ImportError("Install networkx before importing hsimg") from exc

try:
    from shapely.geometry import (
        GeometryCollection, LineString, MultiLineString, MultiPolygon,
        Point, Polygon, mapping,
    )
    from shapely.ops import nearest_points, substring, unary_union
    from shapely.strtree import STRtree
    from shapely.validation import make_valid
except ImportError as exc:  # pragma: no cover
    raise ImportError("Install shapely>=2 before importing hsimg") from exc

from v3vector import VectorAxisConfig, VectorMedialAxisEngine


LOGGER = logging.getLogger("hsimg")
if not LOGGER.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _json_compatible(value: Any) -> Any:
    """Return a recursively JSON-compatible value with finite numbers only."""
    if isinstance(value, np.generic):
        return _json_compatible(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return _json_compatible(asdict(value))
    return value


def _json(value: Any) -> str:
    """Serialize numpy, dataclasses, IFC values and paths deterministically."""
    def default(obj: Any) -> Any:
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, Path):
            return str(obj)
        if hasattr(obj, "is_a") and callable(obj.is_a):
            return {"ifc_class": obj.is_a(), "ifc_id": obj.id()}
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        return str(obj)
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        default=default,
        allow_nan=False,
    )


def stable_id(prefix: str, *parts: Any) -> str:
    """Return a readable, stable identifier from source identifiers and roles."""
    payload = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def safe_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def entity_name(entity: Any) -> str:
    return safe_text(getattr(entity, "Name", None)) or safe_text(
        getattr(entity, "LongName", None)
    ) or f"{entity.is_a()} #{entity.id()}"


def length_3d(line: LineString) -> float:
    coords = np.asarray(line.coords, dtype=float)
    if len(coords) < 2:
        return 0.0
    if coords.shape[1] == 2:
        coords = np.column_stack([coords, np.zeros(len(coords))])
    return float(np.linalg.norm(np.diff(coords[:, :3], axis=0), axis=1).sum())


def force_3d_point(point: Point, z: float) -> Point:
    return Point(float(point.x), float(point.y), float(z))


def line_between(a: Point, b: Point) -> LineString:
    az = float(a.z) if a.has_z else 0.0
    bz = float(b.z) if b.has_z else 0.0
    return LineString([(a.x, a.y, az), (b.x, b.y, bz)])


def estimated_corridor_width(
    line: LineString,
    polygon: Polygon | MultiPolygon,
) -> float:
    """Estimate usable width from medial-line clearance to the space boundary."""
    line_2d = LineString([(coord[0], coord[1]) for coord in line.coords])
    samples = [
        line_2d.interpolate(fraction, normalized=True)
        for fraction in np.linspace(0.10, 0.90, 9)
    ]
    clearances = [point.distance(polygon.boundary) for point in samples]
    return 2.0 * float(np.percentile(clearances, 20)) if clearances else 0.0


def local_interior_point_at_boundary(
    polygon: Polygon | MultiPolygon,
    raw_point: Point,
    inset: float,
) -> Optional[Point]:
    """Return a nearby point just inside ``polygon`` at ``raw_point``.

    A direction towards a polygon's global representative point is unsafe for
    concave spaces and circulation rings: it can cross a courtyard and collapse
    many door-side nodes onto the same remote fallback point.  The inward
    offset is therefore derived from a locally nearest point of an eroded
    walkable domain.
    """
    if polygon is None or polygon.is_empty:
        return None
    raw_2d = Point(float(raw_point.x), float(raw_point.y))
    boundary_point = nearest_points(polygon.boundary, raw_2d)[0]
    inset = max(float(inset), 1e-4)

    eroded = polygon.buffer(-inset)
    if not eroded.is_empty:
        candidate = nearest_points(eroded, boundary_point)[0]
        if polygon.covers(candidate):
            return Point(float(candidate.x), float(candidate.y))

    # Very narrow or locally degenerate spaces can disappear after erosion.
    # Keep the fallback local to the door instead of using one global point for
    # the complete space.
    for radius in (inset * 4.0, inset * 8.0):
        local_domain = polygon.intersection(boundary_point.buffer(radius))
        if local_domain.is_empty:
            continue
        candidate = local_domain.representative_point()
        if polygon.covers(candidate):
            return Point(float(candidate.x), float(candidate.y))
    return None


def clean_polygon(geometry: Any) -> Optional[Polygon | MultiPolygon]:
    """Repair and retain polygonal components only."""
    if geometry is None or geometry.is_empty:
        return None
    try:
        geometry = make_valid(geometry) if not geometry.is_valid else geometry
    except Exception:
        geometry = geometry.buffer(0)
    if isinstance(geometry, (Polygon, MultiPolygon)):
        result = geometry
    elif isinstance(geometry, GeometryCollection):
        polys = [g for g in geometry.geoms if isinstance(g, (Polygon, MultiPolygon))]
        result = unary_union(polys) if polys else None
    else:
        result = None
    if result is None or result.is_empty:
        return None
    if isinstance(result, MultiPolygon):
        result = max(result.geoms, key=lambda g: g.area)
    return result


def representative_point_3d(polygon: Polygon | MultiPolygon, z: float) -> Point:
    """Return centroid when navigable, otherwise a guaranteed interior point."""
    centroid = polygon.centroid
    point = centroid if polygon.covers(centroid) else polygon.representative_point()
    return force_3d_point(point, z)


def _iter_product_containers(entity: Any) -> Iterator[Any]:
    for rel in getattr(entity, "ContainedInStructure", []) or []:
        yield rel.RelatingStructure
    for rel in getattr(entity, "ReferencedInStructures", []) or []:
        yield rel.RelatingStructure


def containing_storey(entity: Any) -> Optional[Any]:
    """Resolve a product's storey through containment or ancestor decomposition."""
    if entity is not None and entity.is_a("IfcBuildingStorey"):
        return entity
    seen: set[int] = set()
    queue = list(_iter_product_containers(entity))
    while queue:
        candidate = queue.pop(0)
        if candidate.id() in seen:
            continue
        seen.add(candidate.id())
        if candidate.is_a("IfcBuildingStorey"):
            return candidate
        queue.extend(_iter_product_containers(candidate))
        for rel in getattr(candidate, "Decomposes", []) or []:
            queue.append(rel.RelatingObject)
    for rel in getattr(entity, "Decomposes", []) or []:
        parent = rel.RelatingObject
        result = containing_storey(parent)
        if result is not None:
            return result
    return None


def flatten_psets(entity: Any) -> dict[str, Any]:
    """Flatten property sets while preserving their source names."""
    try:
        psets = ifcopenshell.util.element.get_psets(entity, psets_only=False)
    except Exception:
        return {}
    flat: dict[str, Any] = {}
    for pset_name, values in psets.items():
        if not isinstance(values, Mapping):
            continue
        for key, value in values.items():
            if key != "id" and value not in (None, ""):
                flat[f"{pset_name}.{key}"] = value
    return flat


def property_value(properties: Mapping[str, Any], *patterns: str) -> Any:
    regexes = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for key, value in properties.items():
        if any(regex.search(key) for regex in regexes):
            return value
    return None


@dataclass(slots=True)
class HSIMGConfig:
    horizontal_mobility_min_doors: int = 3
    spatial_tolerance_m: float = 0.05
    vertical_alignment_tolerance_m: float = 0.25
    elevator_grouping_tolerance_m: float = 1.50
    door_projection_max_distance_m: float = 5.0
    door_space_search_distance_m: float = 0.60
    minimum_walkable_width_m: float = 0.90
    wheelchair_min_door_width_m: float = 0.80
    maximum_accessible_ramp_slope: float = 0.08
    medial_axis_pruning_length_m: float = 0.50
    # V3 preserves practically every geometric void. The legacy 1 m² threshold
    # could make an axis cross columns or other small, non-walkable boundaries.
    medial_axis_min_hole_area_m2: float = 1.00
    skeleton_resolution_m: float = 0.15
    vector_boundary_sample_spacing_m: float = 0.30
    vector_simplification_tolerance_m: float = 0.05
    vector_snap_tolerance_m: float = 0.02
    prefer_vector_medial_axis: bool = True
    max_skeleton_cells: int = 2_000_000
    geometry_workers: int = 1
    extract_door_footprints: bool = False
    manual_crs: Optional[str] = None
    generate_diagnostics: bool = True
    export_debug_layers: bool = True
    use_geometry_cache: bool = True
    cache_directory: str = ".hsimg_cache"
    semantic_mobility_keywords: tuple[str, ...] = (
        "corridor", "pasillo", "hall", "lobby", "vestibule", "vestíbulo",
        "circulation", "circulación", "distributor", "distribuidor", "foyer",
        "rellano", "landing", "galería", "gallery",
    )
    semantic_elevator_keywords: tuple[str, ...] = (
        "elevator", "lift", "ascensor", "elevador", "montacargas",
    )
    semantic_space_weight: float = 0.55
    door_count_weight: float = 0.25
    elongation_weight: float = 0.12
    area_weight: float = 0.08
    horizontal_classification_threshold: float = 0.50
    walking_speed_m_s: float = 1.35
    stair_speed_m_s: float = 0.55
    ramp_speed_m_s: float = 0.90
    elevator_speed_m_s: float = 1.20
    elevator_wait_s: float = 20.0
    default_door_open: bool = True
    closed_door_guids: tuple[str, ...] = ()
    closed_door_names: tuple[str, ...] = ()
    open_door_guids: tuple[str, ...] = ()
    open_door_names: tuple[str, ...] = ()
    infer_door_status_from_ifc_properties: bool = True

    def __post_init__(self) -> None:
        if self.horizontal_mobility_min_doors < 1:
            raise ValueError("horizontal_mobility_min_doors must be >= 1")
        for name in (
            "spatial_tolerance_m", "door_projection_max_distance_m",
            "skeleton_resolution_m", "minimum_walkable_width_m",
            "medial_axis_min_hole_area_m2",
            "vector_boundary_sample_spacing_m",
            "vector_simplification_tolerance_m",
            "vector_snap_tolerance_m",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        overlap = set(self.closed_door_guids) & set(self.open_door_guids)
        if overlap:
            raise ValueError(
                "A door GUID cannot be both explicitly open and closed: "
                f"{sorted(overlap)}"
            )
        for field_name in ("closed_door_names", "open_door_names"):
            for pattern in getattr(self, field_name):
                re.compile(pattern, re.IGNORECASE)


@dataclass(slots=True)
class StoreyRecord:
    storey_id: str
    ifc_guid: str
    ifc_entity_id: int
    name: str
    elevation: float


@dataclass(slots=True)
class SpaceRecord:
    space_id: str
    ifc_guid: str
    ifc_entity_id: int
    ifc_class: str
    name: str
    long_name: Optional[str]
    object_type: Optional[str]
    storey_id: Optional[str]
    elevation: float
    footprint: Optional[Polygon | MultiPolygon]
    interior_point: Optional[Point]
    area: Optional[float]
    aspect_ratio: Optional[float]
    number_of_doors: int = 0
    connected_door_ids: list[str] = field(default_factory=list)
    node_class: str = "finalist"
    classification_source: str = "default"
    classification_score: float = 0.0
    extraction_method: str = "ifc_geometry"
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DoorRecord:
    door_id: str
    ifc_guid: str
    ifc_entity_id: int
    ifc_class: str
    name: str
    storey_id: Optional[str]
    point: Optional[Point]
    footprint: Optional[Polygon | MultiPolygon]
    width: Optional[float]
    height: Optional[float]
    threshold_height: Optional[float]
    operation_type: Optional[str]
    connected_space_ids: list[str] = field(default_factory=list)
    relation_source: str = "unresolved"
    confidence: float = 0.0
    wheelchair_accessible: Optional[bool] = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VerticalMobilityRecord:
    vertical_id: str
    ifc_guid: str
    ifc_entity_id: int
    ifc_class: str
    name: str
    vertical_type: Literal["elevator", "stair", "ramp"]
    path: Optional[LineString | MultiLineString]
    connected_storeys: list[str]
    accessible_wheelchair: bool
    width: Optional[float] = None
    slope: Optional[float] = None
    properties: dict[str, Any] = field(default_factory=dict)
    extraction_method: str = "ifc_geometry"
    confidence: float = 1.0


@dataclass(slots=True)
class ValidationIssue:
    issue_id: str
    severity: Literal["info", "warning", "error"]
    issue_type: str
    message: str
    suggested_action: str
    related_ifc_guid: Optional[str] = None
    related_node_id: Optional[str] = None
    geometry: Optional[Any] = None


@dataclass(slots=True)
class GeometryResult:
    vertices: np.ndarray
    faces: np.ndarray
    footprint: Optional[Polygon | MultiPolygon]
    z_min: float
    z_max: float
    method: str


class GeometryEngine:
    """IFC mesh extraction and conservative 2D footprint derivation."""

    def __init__(self, config: HSIMGConfig):
        self.config = config
        self.settings = ifcopenshell.geom.settings()
        self.settings.set(self.settings.USE_WORLD_COORDS, True)

    def mesh(self, entity: Any) -> GeometryResult:
        shape = ifcopenshell.geom.create_shape(self.settings, entity)
        vertices = np.asarray(shape.geometry.verts, dtype=float).reshape((-1, 3))
        faces = np.asarray(shape.geometry.faces, dtype=int).reshape((-1, 3))
        if not len(vertices):
            raise ValueError("empty geometry")
        footprint = self._project_triangles(vertices, faces)
        return GeometryResult(
            vertices=vertices,
            faces=faces,
            footprint=footprint,
            z_min=float(vertices[:, 2].min()),
            z_max=float(vertices[:, 2].max()),
            method="ifcopenshell_world_mesh_projected_triangles",
        )

    @staticmethod
    def _project_triangles(vertices: np.ndarray, faces: np.ndarray) -> Optional[Polygon | MultiPolygon]:
        polygons: list[Polygon] = []
        for face in faces:
            xy = vertices[face, :2]
            polygon = Polygon(xy)
            if polygon.is_valid and polygon.area > 1e-8:
                polygons.append(polygon)
        return clean_polygon(unary_union(polygons)) if polygons else None

    def point_and_footprint(self, entity: Any) -> tuple[Optional[Point], Optional[Any], str]:
        try:
            result = self.mesh(entity)
            centroid = np.mean(result.vertices, axis=0)
            return Point(*map(float, centroid[:3])), result.footprint, result.method
        except Exception as exc:
            LOGGER.debug("Geometry failed for %s #%s: %s", entity.is_a(), entity.id(), exc)
            try:
                matrix = ifcopenshell.util.placement.get_local_placement(entity.ObjectPlacement)
                xyz = matrix[:3, 3]
                return Point(*map(float, xyz)), None, "object_placement_fallback"
            except Exception:
                return None, None, "geometry_unavailable"

    def trajectory(self, entity: Any) -> tuple[Optional[LineString], str]:
        """Approximate the principal 3D pedestrian trajectory through a flight."""
        try:
            result = self.mesh(entity)
            vertices = result.vertices
            low_cut = np.quantile(vertices[:, 2], 0.12)
            high_cut = np.quantile(vertices[:, 2], 0.88)
            low = vertices[vertices[:, 2] <= low_cut].mean(axis=0)
            high = vertices[vertices[:, 2] >= high_cut].mean(axis=0)
            if np.linalg.norm(high - low) < 1e-6:
                raise ValueError("degenerate flight trajectory")
            return LineString([tuple(low), tuple(high)]), "flight_mesh_z_quantile_axis"
        except Exception:
            point, _, method = self.point_and_footprint(entity)
            return (None if point is None else LineString([point.coords[0], point.coords[0]]), method)


def _pixel_skeleton_lines(
    polygon: Polygon | MultiPolygon,
    z: float,
    config: HSIMGConfig,
) -> list[LineString]:
    """Rasterise a polygon, skeletonise it and vectorise pixel chains.

    The optional scikit-image dependency is isolated here.  If it is unavailable,
    or the raster would be unsafe in memory, callers use a visibility-graph fallback.
    """
    try:
        from skimage.morphology import skeletonize
        try:
            from shapely import contains_xy
        except ImportError:  # Shapely 1 fallback
            contains_xy = None
    except ImportError:
        return []

    def filtered_part(part: Polygon) -> Polygon:
        retained_holes = [
            ring.coords
            for ring in part.interiors
            if Polygon(ring).area >= config.medial_axis_min_hole_area_m2
        ]
        return Polygon(part.exterior.coords, retained_holes)

    if isinstance(polygon, MultiPolygon):
        polygon = MultiPolygon([filtered_part(part) for part in polygon.geoms])
    else:
        polygon = filtered_part(polygon)

    minx, miny, maxx, maxy = polygon.bounds
    resolution = config.skeleton_resolution_m
    nx_cells = max(3, int(math.ceil((maxx - minx) / resolution)) + 2)
    ny_cells = max(3, int(math.ceil((maxy - miny) / resolution)) + 2)
    if nx_cells * ny_cells > config.max_skeleton_cells:
        LOGGER.warning("Skeleton raster skipped: %,d cells exceed limit", nx_cells * ny_cells)
        return []
    xs = minx + (np.arange(nx_cells) + 0.5) * resolution
    ys = miny + (np.arange(ny_cells) + 0.5) * resolution
    xx, yy = np.meshgrid(xs, ys)
    if contains_xy is not None:
        mask = contains_xy(polygon, xx, yy)
    else:
        mask = np.array([polygon.covers(Point(x, y)) for x, y in zip(xx.ravel(), yy.ravel())]).reshape(xx.shape)
    skeleton = skeletonize(mask)
    pixels = {tuple(map(int, pixel)) for pixel in np.argwhere(skeleton)}
    if not pixels:
        return []
    offsets = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)]

    def build_adjacency(active_pixels: set[tuple[int, int]]) -> dict[tuple[int, int], list[tuple[int, int]]]:
        result: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for pixel in active_pixels:
            neighbors = []
            for dy, dx in offsets:
                candidate = (pixel[0] + dy, pixel[1] + dx)
                if candidate not in active_pixels:
                    continue
                # Avoid triangular shortcuts at raster corners while retaining
                # genuinely diagonal skeleton segments.
                if dy and dx and (
                    (pixel[0], candidate[1]) in active_pixels
                    or (candidate[0], pixel[1]) in active_pixels
                ):
                    continue
                neighbors.append(candidate)
            result[pixel] = neighbors
        return result

    adjacency = build_adjacency(pixels)
    pixel_graph = nx.Graph()
    pixel_graph.add_nodes_from(pixels)
    pixel_graph.add_edges_from(
        (pixel, neighbor)
        for pixel, neighbors in adjacency.items()
        for neighbor in neighbors
    )
    cycle_core = nx.k_core(pixel_graph, k=2)
    minimum_core_pixels = max(
        4,
        int(math.ceil(config.medial_axis_pruning_length_m / resolution)),
    )
    core_components = [
        component
        for component in nx.connected_components(cycle_core)
        if len(component) >= minimum_core_pixels
    ]
    if core_components:
        # A circulation space around a courtyard has a closed medial loop plus
        # many doorway spurs. Its 2-core is the stable navigable centreline.
        pixels = set().union(*core_components)
        adjacency = build_adjacency(pixels)
    critical = {p for p, neighbors in adjacency.items() if len(neighbors) != 2}
    if not critical:
        critical = {next(iter(pixels))}
    visited: set[frozenset[tuple[int, int]]] = set()
    chains: list[list[tuple[int, int]]] = []
    for start in critical:
        for neighbor in adjacency[start]:
            edge = frozenset((start, neighbor))
            if edge in visited:
                continue
            chain = [start, neighbor]
            visited.add(edge)
            previous, current = start, neighbor
            while current not in critical:
                candidates = [p for p in adjacency[current] if p != previous]
                if not candidates:
                    break
                nxt = candidates[0]
                visited.add(frozenset((current, nxt)))
                chain.append(nxt)
                previous, current = current, nxt
            chains.append(chain)
    lines = []
    walkable_domain = polygon.buffer(config.spatial_tolerance_m)
    for chain in chains:
        coords = [(xs[col], ys[row], z) for row, col in chain]
        line = LineString(coords)
        if length_3d(line) >= config.medial_axis_pruning_length_m:
            simplified = line.simplify(resolution * 1.5)
            simplified_2d = LineString([(coord[0], coord[1]) for coord in simplified.coords])
            # Douglas-Peucker simplification can replace an L-shaped medial
            # chain with a chord crossing a courtyard, wall or concave corner.
            # Keep the original raster chain whenever the shortcut leaves the
            # walkable IfcSpace footprint.
            lines.append(simplified if walkable_domain.covers(simplified_2d) else line)
    return lines


def _horizontal_substrings(
    line: LineString,
    distances: Sequence[float],
    z: float,
    tolerance: float,
) -> list[LineString]:
    """Split a horizontal axis at ordered access projections."""
    line_2d = LineString([(coord[0], coord[1]) for coord in line.coords])
    length = float(line_2d.length)
    if length <= tolerance:
        return []
    cuts = [0.0, length]
    cuts.extend(max(0.0, min(length, float(value))) for value in distances)
    cuts = sorted(cuts)
    unique = [cuts[0]]
    for value in cuts[1:]:
        if value - unique[-1] > tolerance:
            unique.append(value)
        else:
            unique[-1] = max(unique[-1], value)
    pieces: list[LineString] = []
    for start, end in zip(unique, unique[1:]):
        if end - start <= tolerance:
            continue
        piece = substring(line_2d, start, end)
        if not isinstance(piece, LineString) or len(piece.coords) < 2:
            continue
        pieces.append(LineString([(x, y, z) for x, y, *_ in piece.coords]))
    return pieces


def _fallback_visibility_axis(
    polygon: Polygon | MultiPolygon,
    access_points: Sequence[Point],
    z: float,
    tolerance: float,
) -> list[LineString]:
    """Create a valid local visibility structure when skeletonisation is unavailable."""
    interior = representative_point_3d(polygon, z)
    candidates = [interior]
    candidates.extend(force_3d_point(p, z) for p in access_points)
    lines: list[LineString] = []
    domain = polygon.buffer(tolerance)
    for point in candidates[1:]:
        line = line_between(interior, point)
        if domain.covers(LineString([(x, y) for x, y, *_ in line.coords])):
            lines.append(line)
    if not lines:
        rectangle = polygon.minimum_rotated_rectangle
        coords = list(rectangle.exterior.coords)[:4]
        distances = [(math.dist(coords[i], coords[(i + 1) % 4]), i) for i in range(4)]
        _, index = max(distances)
        a, b = Point(coords[index]), Point(coords[(index + 1) % 4])
        axis = LineString([
            (0.5 * (a.x + interior.x), 0.5 * (a.y + interior.y), z),
            (0.5 * (b.x + interior.x), 0.5 * (b.y + interior.y), z),
        ])
        if axis.length > tolerance:
            lines.append(axis)
    return lines


class _HSIMGBuilderCore:
    """Build, validate, route and export a flattened hierarchical mobility graph."""

    def __init__(self, ifc_model: Any, config: HSIMGConfig | Mapping[str, Any] | None = None):
        self.model = ifc_model
        self.config = config if isinstance(config, HSIMGConfig) else HSIMGConfig(**(config or {}))
        self.geometry = GeometryEngine(self.config)
        self.vector_axis = VectorMedialAxisEngine(VectorAxisConfig(
            boundary_sample_spacing_m=self.config.vector_boundary_sample_spacing_m,
            minimum_branch_length_m=self.config.medial_axis_pruning_length_m,
            simplification_tolerance_m=self.config.vector_simplification_tolerance_m,
            containment_tolerance_m=self.config.spatial_tolerance_m,
            minimum_hole_area_m2=self.config.medial_axis_min_hole_area_m2,
            snap_tolerance_m=self.config.vector_snap_tolerance_m,
        ))
        self.graph = nx.MultiDiGraph(model="HSIMG", hierarchy="flattened_explicit")
        self.storeys: dict[str, StoreyRecord] = {}
        self.spaces: dict[str, SpaceRecord] = {}
        self.doors: dict[str, DoorRecord] = {}
        self.vertical_elements: dict[str, VerticalMobilityRecord] = {}
        self.mobility_axes: list[dict[str, Any]] = []
        self.subgraphs: list[dict[str, Any]] = []
        self.issues: list[ValidationIssue] = []
        self.model_metadata: dict[str, Any] = {}
        self.user_profiles = self._default_profiles()
        self._space_entities: dict[str, Any] = {}
        self._door_entities: dict[str, Any] = {}
        self._space_tree: Optional[STRtree] = None
        self._tree_spaces: list[SpaceRecord] = []

    @classmethod
    def from_file(cls, ifc_path: str | Path, config: HSIMGConfig | Mapping[str, Any] | None = None) -> "HSIMGBuilder":
        path = Path(ifc_path)
        LOGGER.info("Opening IFC: %s", path)
        builder = cls(ifcopenshell.open(str(path)), config)
        builder.model_metadata["source_ifc_filename"] = path.name
        builder.model_metadata["source_ifc_path"] = str(path)
        return builder

    @staticmethod
    def _default_profiles() -> list[dict[str, Any]]:
        return [
            {
                "profile_id": "general", "profile_name": "General pedestrian",
                "constraints_json": _json({"stairs": True, "ramps": True, "elevators": True}),
                "cost_weights_json": _json({"time": 1.0, "effort": 0.15}),
            },
            {
                "profile_id": "wheelchair", "profile_name": "Wheelchair user",
                "constraints_json": _json({"stairs": False, "minimum_door_width": 0.80, "maximum_ramp_slope": 0.08}),
                "cost_weights_json": _json({"time": 1.0, "effort": 0.30}),
            },
        ]

    def _issue(
        self, severity: str, issue_type: str, message: str, suggested_action: str,
        *, related_ifc_guid: Optional[str] = None, related_node_id: Optional[str] = None,
        geometry: Optional[Any] = None,
    ) -> None:
        issue_id = stable_id("issue", issue_type, related_ifc_guid, related_node_id, message)
        self.issues.append(ValidationIssue(
            issue_id=issue_id, severity=severity, issue_type=issue_type,
            message=message, suggested_action=suggested_action,
            related_ifc_guid=related_ifc_guid, related_node_id=related_node_id,
            geometry=geometry,
        ))

    def inspect_model(self) -> dict[str, Any]:
        """Inspect schema, units, georeferencing and relevant entity counts."""
        classes = [
            "IfcBuildingStorey", "IfcSpace", "IfcDoor", "IfcStair", "IfcStairFlight",
            "IfcRamp", "IfcRampFlight", "IfcTransportElement", "IfcRelSpaceBoundary",
        ]
        counts: dict[str, int | str] = {}
        for name in classes:
            try:
                counts[name] = len(self.model.by_type(name))
            except RuntimeError:
                counts[name] = "not_in_schema"
        georef = self._inspect_georeferencing()
        units = self._inspect_units()
        self.model_metadata.update({
            "ifc_schema": self.model.schema,
            "creation_datetime": datetime.now(timezone.utc).isoformat(),
            "coordinate_reference_system": georef.get("crs") or self.config.manual_crs or "LOCAL_ENGINEERING_CRS",
            "georeferenced": bool(georef.get("georeferenced")),
            "georeferencing_json": _json(georef),
            "units": units,
            "entity_counts_json": _json(counts),
            "processing_parameters_json": _json(asdict(self.config)),
            "software_versions_json": _json(self._software_versions()),
        })
        LOGGER.info("IFC %s | %s", self.model.schema, counts)
        return {"schema": self.model.schema, "counts": counts, "georeferencing": georef, "units": units}

    def _inspect_units(self) -> str:
        try:
            project = self.model.by_type("IfcProject")[0]
            units = list(project.UnitsInContext.Units)
            length_units = [u for u in units if getattr(u, "UnitType", None) == "LENGTHUNIT"]
            return "; ".join(str(u) for u in length_units) or "unknown"
        except Exception:
            return "unknown"

    def _inspect_georeferencing(self) -> dict[str, Any]:
        result: dict[str, Any] = {"georeferenced": False, "crs": None, "method": "none"}
        try:
            conversions = self.model.by_type("IfcMapConversion")
        except RuntimeError:
            conversions = []
        if conversions:
            conversion = conversions[0]
            target = getattr(conversion, "TargetCRS", None)
            result.update({
                "georeferenced": True,
                "crs": safe_text(getattr(target, "Name", None)),
                "method": "IfcMapConversion",
                "eastings": getattr(conversion, "Eastings", None),
                "northings": getattr(conversion, "Northings", None),
                "orthogonal_height": getattr(conversion, "OrthogonalHeight", None),
            })
        elif self.config.manual_crs:
            result.update({"georeferenced": True, "crs": self.config.manual_crs, "method": "manual_assignment"})
        return result

    @staticmethod
    def _software_versions() -> dict[str, str]:
        packages = ["ifcopenshell", "networkx", "numpy", "shapely", "geopandas", "scikit-image"]
        versions = {}
        for package in packages:
            try:
                versions[package] = importlib_metadata.version(package)
            except importlib_metadata.PackageNotFoundError:
                versions[package] = "not-installed"
        return versions

    def extract_storeys(self) -> dict[str, StoreyRecord]:
        for entity in self.model.by_type("IfcBuildingStorey"):
            guid = entity.GlobalId
            elevation = getattr(entity, "Elevation", None)
            if elevation is None:
                try:
                    elevation = float(ifcopenshell.util.placement.get_local_placement(entity.ObjectPlacement)[2, 3])
                except Exception:
                    elevation = 0.0
                    self._issue("warning", "missing_storey_elevation", f"Storey {entity_name(entity)} has no elevation", "Assign a reliable storey elevation", related_ifc_guid=guid)
            record = StoreyRecord(stable_id("storey", guid), guid, entity.id(), entity_name(entity), float(elevation))
            self.storeys[record.storey_id] = record
        LOGGER.info("Extracted %d storeys", len(self.storeys))
        return self.storeys

    def _storey_id(self, entity: Any) -> Optional[str]:
        storey = containing_storey(entity)
        if storey is None:
            return None
        return stable_id("storey", storey.GlobalId)

    def extract_spaces(self) -> dict[str, SpaceRecord]:
        if not self.storeys:
            self.extract_storeys()
        for entity in self.model.by_type("IfcSpace"):
            guid = entity.GlobalId
            space_id = stable_id("space", guid)
            storey_id = self._storey_id(entity)
            point, footprint, method = self.geometry.point_and_footprint(entity)
            if storey_id is None and point is not None:
                storey_id = self._nearest_storey_id(float(point.z))
            elevation = self.storeys[storey_id].elevation if storey_id in self.storeys else 0.0
            footprint = clean_polygon(footprint)
            if footprint is None:
                self._issue("error", "space_without_geometry", f"Space {entity_name(entity)} has no usable footprint", "Repair/export IfcSpace geometry", related_ifc_guid=guid)
                interior = None
                area = aspect = None
            else:
                interior = representative_point_3d(footprint, elevation)
                area = float(footprint.area)
                rectangle = footprint.minimum_rotated_rectangle
                coords = list(rectangle.exterior.coords)
                lengths = [math.dist(coords[i], coords[i + 1]) for i in range(4)]
                nonzero = [v for v in lengths if v > 1e-9]
                aspect = max(nonzero) / min(nonzero) if nonzero else None
            record = SpaceRecord(
                space_id=space_id, ifc_guid=guid, ifc_entity_id=entity.id(), ifc_class=entity.is_a(),
                name=entity_name(entity), long_name=safe_text(getattr(entity, "LongName", None)),
                object_type=safe_text(getattr(entity, "ObjectType", None)), storey_id=storey_id,
                elevation=elevation, footprint=footprint, interior_point=interior, area=area,
                aspect_ratio=aspect, extraction_method=method, properties=flatten_psets(entity),
            )
            self.spaces[space_id] = record
            self._space_entities[space_id] = entity
        self._build_space_index()
        LOGGER.info("Extracted %d spaces (%d with footprints)", len(self.spaces), sum(s.footprint is not None for s in self.spaces.values()))
        return self.spaces

    def _build_space_index(self) -> None:
        self._tree_spaces = [space for space in self.spaces.values() if space.footprint is not None]
        self._space_tree = STRtree([space.footprint for space in self._tree_spaces]) if self._tree_spaces else None

    def extract_doors(self) -> dict[str, DoorRecord]:
        for entity in self.model.by_type("IfcDoor"):
            guid = entity.GlobalId
            door_id = stable_id("door", guid)
            properties = flatten_psets(entity)
            width = getattr(entity, "OverallWidth", None) or property_value(properties, r"width", r"anchura", r"ancho")
            height = getattr(entity, "OverallHeight", None) or property_value(properties, r"height", r"altura")
            threshold = property_value(properties, r"threshold", r"umbral")
            operation = safe_text(getattr(entity, "OperationType", None)) or safe_text(property_value(properties, r"operationtype", r"operation"))
            try:
                width = float(width) if width is not None else None
            except (TypeError, ValueError):
                width = None
            try:
                height = float(height) if height is not None else None
            except (TypeError, ValueError):
                height = None
            try:
                threshold = float(threshold) if threshold is not None else None
            except (TypeError, ValueError):
                threshold = None
            # Door meshes from detailed Revit families can dominate processing time.
            # The global placement plus half the nominal width is a reproducible,
            # schema-independent portal point; full footprints remain opt-in.
            point = None
            footprint = None
            try:
                matrix = ifcopenshell.util.placement.get_local_placement(entity.ObjectPlacement)
                xyz = matrix[:3, 3].astype(float)
                if width:
                    xyz = xyz + matrix[:3, 0].astype(float) * (0.5 * width)
                point = Point(*map(float, xyz))
            except Exception:
                point, _, _ = self.geometry.point_and_footprint(entity)
            if self.config.extract_door_footprints:
                mesh_point, footprint, _ = self.geometry.point_and_footprint(entity)
                point = mesh_point or point
            accessible = None if width is None else width >= self.config.wheelchair_min_door_width_m
            record = DoorRecord(
                door_id=door_id, ifc_guid=guid, ifc_entity_id=entity.id(), ifc_class=entity.is_a(),
                name=entity_name(entity), storey_id=self._storey_id(entity), point=point,
                footprint=clean_polygon(footprint), width=width, height=height,
                threshold_height=threshold, operation_type=operation,
                wheelchair_accessible=accessible, properties=properties,
            )
            self.doors[door_id] = record
            self._door_entities[door_id] = entity
        LOGGER.info("Extracted %d doors", len(self.doors))
        return self.doors

    def analyse_door_space_relationships(self) -> dict[str, list[str]]:
        """Combine IfcRelSpaceBoundary evidence with storey-aware geometry inference."""
        if not self.spaces:
            self.extract_spaces()
        if not self.doors:
            self.extract_doors()
        semantic: defaultdict[str, set[str]] = defaultdict(set)
        try:
            boundaries = self.model.by_type("IfcRelSpaceBoundary")
        except RuntimeError:
            boundaries = []
        door_guid_lookup = {door.ifc_guid: door_id for door_id, door in self.doors.items()}
        space_guid_lookup = {space.ifc_guid: space_id for space_id, space in self.spaces.items()}
        for boundary in boundaries:
            space = getattr(boundary, "RelatingSpace", None)
            element = getattr(boundary, "RelatedBuildingElement", None)
            if space is None or element is None:
                continue
            candidates = [element]
            if element.is_a("IfcOpeningElement"):
                for rel in getattr(element, "HasFillings", []) or []:
                    candidates.append(rel.RelatedBuildingElement)
            for candidate in candidates:
                door_id = door_guid_lookup.get(getattr(candidate, "GlobalId", None))
                space_id = space_guid_lookup.get(getattr(space, "GlobalId", None))
                if door_id and space_id:
                    semantic[door_id].add(space_id)

        for door_id, door in self.doors.items():
            semantic_spaces = semantic.get(door_id, set())
            inferred = self._infer_spaces_for_door(door)
            if semantic_spaces and inferred:
                connected = list(semantic_spaces | set(inferred))
                source, confidence = "hybrid", 0.95
            elif semantic_spaces:
                connected = list(semantic_spaces)
                source, confidence = "IFC_semantic", 1.0
            else:
                connected = inferred
                source, confidence = ("geometry_inferred", 0.70) if inferred else ("unresolved", 0.0)
            if len(connected) > 2:
                connected = sorted(connected, key=lambda sid: self._door_space_distance(door, self.spaces[sid]))[:2]
                confidence = min(confidence, 0.65)
            door.connected_space_ids = connected
            door.relation_source = source
            door.confidence = confidence
            for space_id in connected:
                space = self.spaces[space_id]
                if door_id not in space.connected_door_ids:
                    space.connected_door_ids.append(door_id)
        for space in self.spaces.values():
            space.number_of_doors = len(space.connected_door_ids)
        LOGGER.info("Resolved %d/%d doors to spaces", sum(bool(d.connected_space_ids) for d in self.doors.values()), len(self.doors))
        return {door_id: door.connected_space_ids for door_id, door in self.doors.items()}

    def _door_space_distance(self, door: DoorRecord, space: SpaceRecord) -> float:
        if door.point is None or space.footprint is None:
            return float("inf")
        return float(space.footprint.distance(Point(door.point.x, door.point.y)))

    def _infer_spaces_for_door(self, door: DoorRecord) -> list[str]:
        if door.point is None or self._space_tree is None:
            return []
        query_geometry = Point(door.point.x, door.point.y).buffer(self.config.door_space_search_distance_m)
        try:
            indices = self._space_tree.query(query_geometry)
            candidates = [self._tree_spaces[int(i)] for i in indices]
        except (TypeError, ValueError):  # Shapely 1 returns geometries
            geometry_to_space = {id(space.footprint): space for space in self._tree_spaces}
            candidates = [geometry_to_space[id(g)] for g in self._space_tree.query(query_geometry)]
        same_storey = [
            space for space in candidates
            if door.storey_id is None or space.storey_id is None or door.storey_id == space.storey_id
        ]
        ranked = sorted(
            (space for space in same_storey if space.footprint and space.footprint.buffer(self.config.door_space_search_distance_m).intersects(query_geometry)),
            key=lambda space: self._door_space_distance(door, space),
        )
        return [space.space_id for space in ranked[:2]]

    def classify_spaces(self) -> dict[str, SpaceRecord]:
        """Hybrid semantic, connectivity and morphology classification."""
        keywords = tuple(k.casefold() for k in self.config.semantic_mobility_keywords)
        areas = [space.area for space in self.spaces.values() if space.area]
        area_reference = float(np.median(areas)) if areas else 1.0
        for space in self.spaces.values():
            corpus = " ".join(filter(None, [space.name, space.long_name, space.object_type, " ".join(space.properties.keys()), " ".join(map(str, space.properties.values()))])).casefold()
            semantic_hit = any(keyword in corpus for keyword in keywords)
            door_score = min(1.0, space.number_of_doors / max(1, self.config.horizontal_mobility_min_doors))
            elongation_score = min(1.0, max(0.0, ((space.aspect_ratio or 1.0) - 1.5) / 4.0))
            area_score = min(1.0, (space.area or 0.0) / max(area_reference * 3.0, 1.0))
            score = (
                self.config.semantic_space_weight * float(semantic_hit)
                + self.config.door_count_weight * door_score
                + self.config.elongation_weight * elongation_score
                + self.config.area_weight * area_score
            )
            space.classification_score = float(score)
            if score >= self.config.horizontal_classification_threshold:
                space.node_class = "horizontal_mobility"
                components = []
                if semantic_hit:
                    components.append("semantic_keyword")
                if space.number_of_doors >= self.config.horizontal_mobility_min_doors:
                    components.append("door_connectivity")
                if elongation_score >= 0.5:
                    components.append("elongation")
                if area_score >= 0.5:
                    components.append("area")
                space.classification_source = "hybrid:" + "+".join(components or ["weighted_score"])
            else:
                space.node_class = "finalist"
                space.classification_source = "hybrid:below_threshold"
        LOGGER.info("Classified %d horizontal mobility spaces", sum(s.node_class == "horizontal_mobility" for s in self.spaces.values()))
        return self.spaces

    def _add_node(
        self, node_id: str, point: Point, *, node_type: str, node_role: str,
        mobility_type: Optional[str], parent_node_id: Optional[str], subgraph_id: Optional[str],
        hierarchy_level: int, ifc_guid: Optional[str], ifc_class: Optional[str], name: str,
        storey_id: Optional[str], accessible_general: bool = True,
        accessible_wheelchair: Optional[bool] = True, metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        z = float(point.z) if point.has_z else 0.0
        self.graph.add_node(node_id, node_id=node_id, node_type=node_type, node_role=node_role,
            mobility_type=mobility_type, parent_node_id=parent_node_id, subgraph_id=subgraph_id,
            hierarchy_level=hierarchy_level, ifc_guid=ifc_guid, ifc_class=ifc_class, name=name,
            storey_id=storey_id, x=float(point.x), y=float(point.y), z=z,
            geometry_wkt=point.wkt, geometry=point, accessible_general=bool(accessible_general),
            accessible_wheelchair=accessible_wheelchair, metadata_json=_json(metadata or {}))

    def _add_directed_edge(
        self, source: str, target: str, *, edge_type: str, mobility_mode: str,
        subgraph_id: Optional[str], geometry: Optional[LineString] = None,
        accessible_general: bool = True, accessible_wheelchair: bool = True,
        restriction_reason: Optional[str] = None, relation_source: str = "IFC_semantic",
        validation_status: str = "valid", confidence: float = 1.0,
        metadata: Optional[Mapping[str, Any]] = None, estimated_time: Optional[float] = None,
        effort_cost: Optional[float] = None,
    ) -> Optional[str]:
        if source not in self.graph or target not in self.graph:
            return None
        if geometry is None:
            geometry = line_between(self.graph.nodes[source]["geometry"], self.graph.nodes[target]["geometry"])
        distance = length_3d(geometry)
        if distance <= 1e-9:
            LOGGER.debug("Skipped zero-length edge %s -> %s (%s)", source, target, edge_type)
            return None
        if estimated_time is None:
            speed = self.config.walking_speed_m_s
            estimated_time = distance / max(speed, 1e-6)
        if effort_cost is None:
            effort_cost = distance
        coords = np.asarray(geometry.coords)
        horizontal = float(np.linalg.norm(np.diff(coords[:, :2], axis=0), axis=1).sum()) if len(coords) > 1 else 0.0
        dz = float(coords[-1, 2] - coords[0, 2]) if coords.shape[1] >= 3 else 0.0
        edge_id = stable_id("edge", source, target, edge_type, subgraph_id, self.graph.number_of_edges(source, target))
        self.graph.add_edge(source, target, key=edge_id, edge_id=edge_id, source=source, target=target,
            edge_type=edge_type, mobility_mode=mobility_mode, subgraph_id=subgraph_id,
            geometry_wkt=geometry.wkt, geometry=geometry, length_3d=distance,
            horizontal_length=horizontal, vertical_displacement=dz,
            estimated_time=float(estimated_time), effort_cost=float(effort_cost),
            accessible_general=bool(accessible_general), accessible_wheelchair=bool(accessible_wheelchair),
            restriction_reason=restriction_reason, relation_source=relation_source,
            validation_status=validation_status, confidence=float(confidence),
            metadata_json=_json(metadata or {}))
        return edge_id

    def _add_bidirectional_edge(self, source: str, target: str, **kwargs: Any) -> None:
        self._add_directed_edge(source, target, **kwargs)
        geometry = kwargs.get("geometry")
        reverse = dict(kwargs)
        if geometry is not None:
            reverse["geometry"] = LineString(list(geometry.coords)[::-1])
        self._add_directed_edge(target, source, **reverse)

    def assemble_space_and_door_graph(self) -> nx.MultiDiGraph:
        """Create semantic space nodes, central portals and side-specific access nodes."""
        for space in self.spaces.values():
            if space.interior_point is None:
                continue
            self._add_node(space.space_id, space.interior_point, node_type="space", node_role="semantic_parent",
                mobility_type="horizontal" if space.node_class == "horizontal_mobility" else None,
                parent_node_id=None, subgraph_id=None, hierarchy_level=0, ifc_guid=space.ifc_guid,
                ifc_class=space.ifc_class, name=space.name, storey_id=space.storey_id,
                metadata={"classification_score": space.classification_score, "classification_source": space.classification_source})
        for door in self.doors.values():
            if door.point is None:
                continue
            self._add_node(door.door_id, door.point, node_type="door", node_role="portal",
                mobility_type="access", parent_node_id=None, subgraph_id=None, hierarchy_level=0,
                ifc_guid=door.ifc_guid, ifc_class=door.ifc_class, name=door.name,
                storey_id=door.storey_id, accessible_wheelchair=door.wheelchair_accessible,
                metadata={"width": door.width, "operation_type": door.operation_type, "relation_source": door.relation_source})
            for side_index, space_id in enumerate(door.connected_space_ids):
                space = self.spaces.get(space_id)
                if space is None or space.footprint is None or space_id not in self.graph:
                    continue
                raw = Point(door.point.x, door.point.y)
                inset = max(self.config.spatial_tolerance_m, 0.05)
                side_2d = local_interior_point_at_boundary(space.footprint, raw, inset)
                maximum_side_distance = max(
                    1.0,
                    self.config.door_space_search_distance_m + 4.0 * inset,
                )
                if side_2d is None or raw.distance(side_2d) > maximum_side_distance:
                    self._issue(
                        "error",
                        "door_side_localisation_failed",
                        f"Could not place a local access point for {door.name} in {space.name}",
                        "Review the door-space association or the local space boundary",
                        related_ifc_guid=door.ifc_guid,
                        related_node_id=door.door_id,
                        geometry=door.point,
                    )
                    continue
                side_point = force_3d_point(side_2d, space.elevation)
                side_id = stable_id("door_side", door.ifc_guid, space.ifc_guid)
                self._add_node(side_id, side_point, node_type="door_access", node_role="door_side",
                    mobility_type="access", parent_node_id=door.door_id, subgraph_id=None,
                    hierarchy_level=1, ifc_guid=door.ifc_guid, ifc_class="IfcDoor",
                    name=f"{door.name} / {space.name}", storey_id=space.storey_id,
                    accessible_wheelchair=door.wheelchair_accessible,
                    metadata={"space_id": space_id, "side_index": side_index})
                wheelchair = door.wheelchair_accessible is not False
                reason = None if wheelchair else "door_width_below_threshold"
                self._add_bidirectional_edge(side_id, door.door_id, edge_type="portal_transition",
                    mobility_mode="door", subgraph_id=None, accessible_wheelchair=wheelchair,
                    restriction_reason=reason, relation_source=door.relation_source, confidence=door.confidence)
                if space.node_class != "horizontal_mobility":
                    line = line_between(space.interior_point, side_point)
                    valid = space.footprint.buffer(self.config.spatial_tolerance_m).covers(
                        LineString([(c[0], c[1]) for c in line.coords])
                    )
                    if valid:
                        self._add_bidirectional_edge(space_id, side_id, edge_type="space_access",
                            mobility_mode="walk", subgraph_id=None, geometry=line,
                            accessible_wheelchair=wheelchair, restriction_reason=reason,
                            relation_source="geometry_inferred", validation_status="valid", confidence=0.85)
                    else:
                        self._issue("error", "edge_crosses_walkable_boundary",
                            f"Rejected interior-to-door edge for {space.name}",
                            "Review door-space inference and walkable polygon",
                            related_ifc_guid=space.ifc_guid, related_node_id=space_id, geometry=line)
        return self.graph

    def build_horizontal_subgraphs(self) -> list[dict[str, Any]]:
        """Build simplified medial axes split exactly at door projections."""
        for space in self.spaces.values():
            if space.node_class != "horizontal_mobility" or space.footprint is None:
                continue
            subgraph_id = stable_id("subgraph", space.ifc_guid, "horizontal")
            access_nodes = []
            access_points = []
            for door_id in space.connected_door_ids:
                side_id = stable_id("door_side", self.doors[door_id].ifc_guid, space.ifc_guid)
                if side_id in self.graph:
                    access_nodes.append(side_id)
                    access_points.append(self.graph.nodes[side_id]["geometry"])
                    self.graph.nodes[side_id]["subgraph_id"] = subgraph_id
            vector_result = None
            axes: list[LineString] = []
            extraction_method = "vector_boundary_voronoi_medial_axis"
            if self.config.prefer_vector_medial_axis:
                vector_result = self.vector_axis.build(
                    space.footprint,
                    space.elevation,
                    access_points,
                )
                axes = vector_result.lines
            if not axes:
                axes = _pixel_skeleton_lines(space.footprint, space.elevation, self.config)
                extraction_method = "raster_medial_axis_fallback"
                self._issue(
                    "warning",
                    "vector_axis_fallback",
                    f"Vector medial axis was unavailable for {space.name}; used raster fallback",
                    "Review the IfcSpace polygon and vector-axis diagnostics",
                    related_ifc_guid=space.ifc_guid,
                    related_node_id=space.space_id,
                    geometry=space.interior_point,
                )
            if not axes:
                axes = _fallback_visibility_axis(
                    space.footprint,
                    access_points,
                    space.elevation,
                    self.config.spatial_tolerance_m,
                )
                extraction_method = "visibility_graph_fallback"
                self._issue(
                    "warning",
                    "visibility_axis_fallback",
                    f"Used visibility fallback for {space.name}",
                    "Review the IfcSpace footprint",
                    related_ifc_guid=space.ifc_guid,
                    related_node_id=space.space_id,
                    geometry=space.interior_point,
                )
            local_nodes: dict[tuple[float, float, float], str] = {}
            local_edges_before = self.graph.number_of_edges()
            projection_distances: dict[int, list[float]] = defaultdict(list)
            for axis_index, axis in enumerate(axes):
                axis_id = stable_id("axis", subgraph_id, axis_index)
                self.mobility_axes.append({
                    "axis_id": axis_id, "parent_node_id": space.space_id, "subgraph_id": subgraph_id,
                    "mobility_type": "horizontal", "extraction_method": extraction_method,
                    "pruning_threshold": self.config.medial_axis_pruning_length_m,
                    "metadata_json": _json(
                        vector_result.diagnostics if vector_result is not None else {
                            "resolution_m": self.config.skeleton_resolution_m,
                        }
                    ),
                    "geometry": axis,
                })

            # The closest point on a line is the orthogonal 2D projection except
            # at its ends. Collect those points first so the axis can be split
            # there instead of drawing a shortcut to an arbitrary endpoint.
            for side_id in access_nodes:
                if not axes:
                    continue
                side_point = self.graph.nodes[side_id]["geometry"]
                side_2d = Point(side_point.x, side_point.y)
                candidates = []
                for axis_index, axis in enumerate(axes):
                    axis_2d = LineString([(coord[0], coord[1]) for coord in axis.coords])
                    station = float(axis_2d.project(side_2d))
                    nearest = axis_2d.interpolate(station)
                    candidates.append((side_2d.distance(nearest), axis_index, station, nearest))
                distance, axis_index, station, nearest = min(candidates, key=lambda item: item[0])
                projection = force_3d_point(nearest, space.elevation)
                if distance > self.config.door_projection_max_distance_m:
                    self._issue("warning", "door_axis_too_far", f"Door access is {distance:.2f} m from internal axis", "Review mobility-space footprint or increase projection limit", related_node_id=side_id, geometry=side_point)
                    continue
                connector = line_between(side_point, projection)
                connector_2d = LineString([(c[0], c[1]) for c in connector.coords])
                valid = space.footprint.buffer(self.config.spatial_tolerance_m).covers(connector_2d)
                if not valid:
                    self._issue("error", "edge_crosses_space_boundary", "Door-to-axis connector leaves walkable polygon", "Review door association and space geometry", related_node_id=side_id, geometry=connector)
                    continue
                key = tuple(round(v, 5) for v in projection.coords[0])
                projection_id = local_nodes.get(key)
                if projection_id is None:
                    projection_id = stable_id("projection", subgraph_id, *key)
                    local_nodes[key] = projection_id
                    self._add_node(projection_id, projection, node_type="internal_mobility", node_role="door_projection",
                        mobility_type="horizontal", parent_node_id=space.space_id, subgraph_id=subgraph_id,
                        hierarchy_level=2, ifc_guid=space.ifc_guid, ifc_class=space.ifc_class,
                        name=f"Axis projection at {space.name}", storey_id=space.storey_id,
                        metadata={"projection_distance": distance})
                projection_distances[axis_index].append(station)
                self._add_bidirectional_edge(side_id, projection_id, edge_type="door_axis_projection",
                    mobility_mode="walk", subgraph_id=subgraph_id, geometry=connector,
                    relation_source="geometry_inferred", confidence=max(0.4, 1.0 - distance / max(self.config.door_projection_max_distance_m, 1e-6)))

            for axis_index, axis in enumerate(axes):
                pieces = _horizontal_substrings(
                    axis,
                    projection_distances.get(axis_index, ()),
                    space.elevation,
                    max(self.config.spatial_tolerance_m * 0.1, 1e-5),
                )
                for piece in pieces:
                    piece_2d = LineString([(coord[0], coord[1]) for coord in piece.coords])
                    if not space.footprint.buffer(self.config.spatial_tolerance_m).covers(piece_2d):
                        self._issue(
                            "error",
                            "internal_axis_crosses_space_boundary",
                            "Rejected an internal-axis segment outside its mobility space",
                            "Review the medial-axis extraction and IfcSpace footprint",
                            related_ifc_guid=space.ifc_guid,
                            related_node_id=space.space_id,
                            geometry=piece,
                        )
                        continue
                    endpoint_ids = []
                    for coordinate in (piece.coords[0], piece.coords[-1]):
                        point = Point(coordinate)
                        key = tuple(round(v, 5) for v in coordinate)
                        node_id = local_nodes.get(key)
                        if node_id is None:
                            node_id = stable_id("internal", subgraph_id, *key)
                            local_nodes[key] = node_id
                            self._add_node(node_id, point, node_type="internal_mobility", node_role="axis_endpoint",
                                mobility_type="horizontal", parent_node_id=space.space_id, subgraph_id=subgraph_id,
                                hierarchy_level=2, ifc_guid=space.ifc_guid, ifc_class=space.ifc_class,
                                name=f"{space.name} axis node", storey_id=space.storey_id,
                                metadata={"extraction_method": extraction_method})
                        endpoint_ids.append(node_id)
                    estimated_width = estimated_corridor_width(piece, space.footprint)
                    narrow_penalty = max(
                        0.0,
                        (self.config.minimum_walkable_width_m - estimated_width)
                        / self.config.minimum_walkable_width_m,
                    )
                    self._add_bidirectional_edge(
                        endpoint_ids[0],
                        endpoint_ids[1],
                        edge_type="internal_axis",
                        mobility_mode="walk",
                        subgraph_id=subgraph_id,
                        geometry=piece,
                        accessible_wheelchair=(
                            estimated_width >= self.config.minimum_walkable_width_m
                        ),
                        effort_cost=length_3d(piece) * (1.0 + narrow_penalty),
                        relation_source="geometry_inferred",
                        confidence=0.95 if extraction_method.startswith("vector") else 0.85,
                        metadata={
                            "estimated_width_m": estimated_width,
                            "narrowness_penalty": narrow_penalty,
                            "axis_method": extraction_method,
                        },
                    )

            internal_nodes = [
                node_id
                for node_id, data in self.graph.nodes(data=True)
                if data.get("subgraph_id") == subgraph_id
                and data.get("node_type") == "internal_mobility"
            ]
            for node_id in internal_nodes:
                has_axis_edge = any(
                    data.get("edge_type") == "internal_axis"
                    for _, _, data in self.graph.edges(node_id, data=True)
                )
                if has_axis_edge:
                    continue
                role = self.graph.nodes[node_id].get("node_role")
                self._issue(
                    "warning",
                    "orphan_horizontal_node_removed",
                    f"Removed disconnected {role or 'internal'} node from {space.name}",
                    "Review the door projection and connected medial-axis component",
                    related_ifc_guid=space.ifc_guid,
                    related_node_id=node_id,
                    geometry=self.graph.nodes[node_id].get("geometry"),
                )
                self.graph.remove_node(node_id)
            self.subgraphs.append({
                "subgraph_id": subgraph_id, "parent_node_id": space.space_id,
                "subgraph_type": "horizontal_mobility", "hierarchy_level": 1,
                "extraction_method": extraction_method,
                "parameters_json": _json({
                    "axis_method": extraction_method,
                    "boundary_sample_spacing_m": self.config.vector_boundary_sample_spacing_m,
                    "simplification_tolerance_m": self.config.vector_simplification_tolerance_m,
                    "pruning_m": self.config.medial_axis_pruning_length_m,
                }),
                "node_count": sum(data.get("subgraph_id") == subgraph_id for _, data in self.graph.nodes(data=True)),
                "edge_count": self.graph.number_of_edges() - local_edges_before,
            })
        LOGGER.info("Built %d horizontal subgraphs", sum(s["subgraph_type"] == "horizontal_mobility" for s in self.subgraphs))
        return self.subgraphs

    def _is_elevator(self, entity: Any) -> bool:
        predefined = safe_text(getattr(entity, "PredefinedType", None))
        if predefined and predefined.upper() == "ELEVATOR":
            return True
        properties = flatten_psets(entity)
        corpus = " ".join([entity_name(entity), safe_text(getattr(entity, "ObjectType", None)) or "", " ".join(map(str, properties.values()))]).casefold()
        return any(keyword.casefold() in corpus for keyword in self.config.semantic_elevator_keywords)

    def extract_elevators(self) -> dict[str, VerticalMobilityRecord]:
        """Detect and cluster elevator occurrences into vertically aligned parents."""
        occurrences = []
        for entity in self.model.by_type("IfcTransportElement"):
            if not self._is_elevator(entity):
                continue
            # Mapped Revit families can keep ObjectPlacement at the storey origin;
            # the world-space mesh centroid/footprint is required for shaft grouping.
            point, footprint, method = self.geometry.point_and_footprint(entity)
            if point is None:
                continue
            occurrences.append({"entity": entity, "point": point, "footprint": footprint, "method": method, "storey_id": self._storey_id(entity)})
        clusters: list[list[dict[str, Any]]] = []
        for occurrence in sorted(occurrences, key=lambda item: item["point"].z):
            if occurrence["storey_id"] is None:
                occurrence["storey_id"] = self._nearest_storey_id(float(occurrence["point"].z))
            best = None
            best_distance = float("inf")
            for cluster in clusters:
                center = np.mean([[x["point"].x, x["point"].y] for x in cluster], axis=0)
                distance = math.hypot(occurrence["point"].x - center[0], occurrence["point"].y - center[1])
                name_a = self._normalise_vertical_name(entity_name(occurrence["entity"])).casefold()
                name_b = self._normalise_vertical_name(entity_name(cluster[0]["entity"])).casefold()
                name_match = name_a == name_b or (any(k in name_a for k in self.config.semantic_elevator_keywords) and any(k in name_b for k in self.config.semantic_elevator_keywords))
                overlaps = any(occurrence["footprint"] is not None and x["footprint"] is not None and occurrence["footprint"].buffer(self.config.vertical_alignment_tolerance_m).intersects(x["footprint"]) for x in cluster)
                if (distance <= self.config.elevator_grouping_tolerance_m and name_match) or overlaps:
                    if distance < best_distance:
                        best, best_distance = cluster, distance
            (best if best is not None else clusters.append([occurrence]))
            if best is not None:
                best.append(occurrence)
        for cluster_index, cluster in enumerate(clusters):
            guids = sorted(x["entity"].GlobalId for x in cluster)
            vertical_id = stable_id("elevator", *guids)
            sorted_stops = sorted(cluster, key=lambda x: x["point"].z)
            by_storey: defaultdict[str, list[Any]] = defaultdict(list)
            for stop in sorted_stops:
                by_storey[stop["storey_id"] or f"z:{stop['point'].z:.3f}"].append(stop)
            consolidated = []
            for _, stops in sorted(by_storey.items(), key=lambda item: float(np.mean([x["point"].z for x in item[1]]))):
                consolidated.append((
                    float(np.mean([x["point"].x for x in stops])),
                    float(np.mean([x["point"].y for x in stops])),
                    float(np.mean([x["point"].z for x in stops])),
                ))
            coords = consolidated
            if len(coords) == 1:
                coords.append((coords[0][0], coords[0][1], coords[0][2] + 0.01))
            path = LineString(coords)
            storeys = list(dict.fromkeys(x["storey_id"] for x in sorted_stops if x["storey_id"]))
            record = VerticalMobilityRecord(
                vertical_id=vertical_id, ifc_guid=cluster[0]["entity"].GlobalId,
                ifc_entity_id=cluster[0]["entity"].id(), ifc_class="IfcTransportElement",
                name=self._normalise_vertical_name(entity_name(cluster[0]["entity"])) or f"Elevator {cluster_index + 1}",
                vertical_type="elevator", path=path, connected_storeys=storeys,
                accessible_wheelchair=True, properties={"occurrence_guids": guids, "occurrence_count": len(cluster)},
                extraction_method="semantic_plus_xy_footprint_clustering", confidence=0.85 if len(storeys) >= 2 else 0.55,
            )
            self.vertical_elements[vertical_id] = record
        LOGGER.info("Grouped %d elevator occurrences into %d systems", len(occurrences), len(clusters))
        return self.vertical_elements

    @staticmethod
    def _normalise_vertical_name(name: str) -> str:
        name = re.sub(r":\d+(?::\d+)?\s*$", "", name).strip()
        return re.sub(r"\s+", " ", name)

    def _extract_flight_based(self, parent_class: str, flight_class: str, vertical_type: Literal["stair", "ramp"]) -> None:
        parents = self.model.by_type(parent_class)
        for parent in parents:
            flights = []
            for rel in getattr(parent, "IsDecomposedBy", []) or []:
                flights.extend(obj for obj in rel.RelatedObjects if obj.is_a(flight_class))
            if not flights and parent.is_a(flight_class):
                flights = [parent]
            lines = []
            methods = []
            for flight in flights:
                line, method = self.geometry.trajectory(flight)
                if line is not None and length_3d(line) > 1e-6:
                    lines.append(line)
                    methods.append(method)
            if not lines:
                line, method = self.geometry.trajectory(parent)
                if line is not None and length_3d(line) > 1e-6:
                    lines, methods = [line], [method]
            path: Optional[LineString | MultiLineString] = MultiLineString(lines) if len(lines) > 1 else (lines[0] if lines else None)
            storey_ids = set(filter(None, [self._storey_id(parent)] + [self._storey_id(flight) for flight in flights]))
            zs = []
            for line in lines:
                zs.extend(c[2] for c in line.coords if len(c) >= 3)
            if zs and len(storey_ids) < 2:
                storey_ids.update(self._storeys_for_z_range(min(zs), max(zs)))
            properties = flatten_psets(parent)
            width = property_value(properties, r"width", r"anchura", r"ancho")
            slope = property_value(properties, r"slope", r"pendiente")
            try: width = float(width) if width is not None else None
            except (TypeError, ValueError): width = None
            try: slope = float(slope) if slope is not None else None
            except (TypeError, ValueError): slope = None
            if slope is None and vertical_type == "ramp" and lines:
                derived_slopes = []
                for line in lines:
                    coords = np.asarray(line.coords, dtype=float)
                    horizontal = float(np.linalg.norm(np.diff(coords[:, :2], axis=0), axis=1).sum())
                    if horizontal > 1e-9:
                        derived_slopes.append(abs(float(coords[-1, 2] - coords[0, 2])) / horizontal)
                if derived_slopes:
                    slope = max(derived_slopes)
                    properties["HSIMG.DerivedLongitudinalSlope"] = slope
            accessible = (
                vertical_type == "ramp"
                and slope is not None and slope <= self.config.maximum_accessible_ramp_slope
                and width is not None and width >= self.config.minimum_walkable_width_m
            )
            vertical_id = stable_id(vertical_type, parent.GlobalId)
            self.vertical_elements[vertical_id] = VerticalMobilityRecord(
                vertical_id=vertical_id, ifc_guid=parent.GlobalId, ifc_entity_id=parent.id(),
                ifc_class=parent.is_a(), name=entity_name(parent), vertical_type=vertical_type,
                path=path, connected_storeys=sorted(storey_ids), accessible_wheelchair=accessible,
                width=width, slope=slope, properties=properties,
                extraction_method="+".join(sorted(set(methods))) or "geometry_unavailable",
                confidence=0.90 if path is not None else 0.30,
            )

    def _storeys_for_z_range(self, z_min: float, z_max: float) -> set[str]:
        tolerance = max(self.config.vertical_alignment_tolerance_m, 0.5)
        return {sid for sid, storey in self.storeys.items() if z_min - tolerance <= storey.elevation <= z_max + tolerance}

    def extract_stairs(self) -> dict[str, VerticalMobilityRecord]:
        self._extract_flight_based("IfcStair", "IfcStairFlight", "stair")
        return self.vertical_elements

    def extract_ramps(self) -> dict[str, VerticalMobilityRecord]:
        self._extract_flight_based("IfcRamp", "IfcRampFlight", "ramp")
        return self.vertical_elements

    def build_vertical_subgraphs(self) -> list[dict[str, Any]]:
        """Flatten vertical parents, stops/landings and real 3D path edges."""
        for vertical in self.vertical_elements.values():
            if vertical.path is None or vertical.path.is_empty:
                self._issue("error", "vertical_element_without_path", f"{vertical.name} has no usable trajectory", "Repair vertical-element geometry", related_ifc_guid=vertical.ifc_guid)
                continue
            subgraph_id = stable_id("subgraph", vertical.vertical_id, "vertical")
            lines = list(vertical.path.geoms) if isinstance(vertical.path, MultiLineString) else [vertical.path]
            all_coords = [coord for line in lines for coord in line.coords]
            min_coord = min(all_coords, key=lambda c: c[2] if len(c) >= 3 else 0.0)
            max_coord = max(all_coords, key=lambda c: c[2] if len(c) >= 3 else 0.0)
            parent_point = Point(
                float(np.mean([c[0] for c in all_coords])),
                float(np.mean([c[1] for c in all_coords])),
                float(np.mean([c[2] if len(c) >= 3 else 0.0 for c in all_coords])),
            )
            self._add_node(vertical.vertical_id, parent_point, node_type="vertical_mobility",
                node_role="semantic_parent", mobility_type=vertical.vertical_type,
                parent_node_id=None, subgraph_id=subgraph_id, hierarchy_level=0,
                ifc_guid=vertical.ifc_guid, ifc_class=vertical.ifc_class, name=vertical.name,
                storey_id=None, accessible_wheelchair=vertical.accessible_wheelchair,
                metadata={"connected_storeys": vertical.connected_storeys, "extraction_method": vertical.extraction_method})
            local_node_ids: dict[tuple[float, float, float], str] = {}
            edges_before = self.graph.number_of_edges()
            for line_index, line in enumerate(lines):
                endpoint_ids = []
                for endpoint_index, coord in enumerate((line.coords[0], line.coords[-1])):
                    coord3 = tuple(coord) if len(coord) >= 3 else (coord[0], coord[1], 0.0)
                    key = tuple(round(float(v), 5) for v in coord3)
                    node_id = local_node_ids.get(key)
                    if node_id is None:
                        node_id = stable_id("vertical_stop", vertical.vertical_id, *key)
                        local_node_ids[key] = node_id
                        point = Point(*coord3)
                        storey_id = self._nearest_storey_id(point.z)
                        role = "landing" if vertical.vertical_type in ("stair", "ramp") else "elevator_stop"
                        self._add_node(node_id, point, node_type="internal_mobility", node_role=role,
                            mobility_type=vertical.vertical_type, parent_node_id=vertical.vertical_id,
                            subgraph_id=subgraph_id, hierarchy_level=2, ifc_guid=vertical.ifc_guid,
                            ifc_class=vertical.ifc_class, name=f"{vertical.name} {role}", storey_id=storey_id,
                            accessible_wheelchair=vertical.accessible_wheelchair,
                            metadata={"line_index": line_index, "endpoint_index": endpoint_index})
                        self._connect_vertical_stop_to_space(node_id, point, storey_id, subgraph_id, vertical)
                    endpoint_ids.append(node_id)
                speed = {
                    "stair": self.config.stair_speed_m_s,
                    "ramp": self.config.ramp_speed_m_s,
                    "elevator": self.config.elevator_speed_m_s,
                }[vertical.vertical_type]
                travel_time = length_3d(line) / max(speed, 1e-6)
                if vertical.vertical_type == "elevator":
                    travel_time += self.config.elevator_wait_s
                restriction = None if vertical.accessible_wheelchair else (
                    "stairs_not_wheelchair_accessible" if vertical.vertical_type == "stair" else "ramp_noncompliant"
                )
                self._add_bidirectional_edge(endpoint_ids[0], endpoint_ids[1], edge_type="vertical_path",
                    mobility_mode=vertical.vertical_type, subgraph_id=subgraph_id, geometry=line,
                    accessible_wheelchair=vertical.accessible_wheelchair,
                    restriction_reason=restriction, relation_source="IFC_semantic" if vertical.confidence >= 0.9 else "hybrid",
                    confidence=vertical.confidence, estimated_time=travel_time,
                    effort_cost=length_3d(line) * (2.0 if vertical.vertical_type == "stair" else 1.2))
            self.subgraphs.append({
                "subgraph_id": subgraph_id, "parent_node_id": vertical.vertical_id,
                "subgraph_type": f"vertical_{vertical.vertical_type}", "hierarchy_level": 1,
                "extraction_method": vertical.extraction_method,
                "parameters_json": _json({"confidence": vertical.confidence}),
                "node_count": len(local_node_ids), "edge_count": self.graph.number_of_edges() - edges_before,
            })
            self.mobility_axes.append({
                "axis_id": stable_id("axis", vertical.vertical_id), "parent_node_id": vertical.vertical_id,
                "subgraph_id": subgraph_id, "mobility_type": vertical.vertical_type,
                "extraction_method": vertical.extraction_method, "pruning_threshold": None,
                "metadata_json": _json({"confidence": vertical.confidence}), "geometry": vertical.path,
            })
        LOGGER.info("Built vertical subgraphs for %d elements", len(self.vertical_elements))
        return self.subgraphs

    def _nearest_storey_id(self, z: float) -> Optional[str]:
        if not self.storeys:
            return None
        return min(self.storeys, key=lambda sid: abs(self.storeys[sid].elevation - z))

    def _connect_vertical_stop_to_space(
        self, stop_id: str, point: Point, storey_id: Optional[str], subgraph_id: str,
        vertical: VerticalMobilityRecord,
    ) -> None:
        candidates = [s for s in self.spaces.values() if s.storey_id == storey_id and s.footprint is not None and s.interior_point is not None]
        if not candidates:
            return
        raw = Point(point.x, point.y)
        space = min(candidates, key=lambda s: s.footprint.distance(raw))
        distance = space.footprint.distance(raw)
        if distance > self.config.door_projection_max_distance_m:
            return
        target_candidates = [
            nid for nid, data in self.graph.nodes(data=True)
            if data.get("storey_id") == storey_id and (
                data.get("parent_node_id") == space.space_id or nid == space.space_id
            )
        ]
        if not target_candidates:
            return
        target = min(target_candidates, key=lambda nid: raw.distance(Point(self.graph.nodes[nid]["x"], self.graph.nodes[nid]["y"])))
        connector = line_between(point, self.graph.nodes[target]["geometry"])
        connector_2d = LineString([(c[0], c[1]) for c in connector.coords])
        valid = space.footprint.buffer(max(0.10, self.config.spatial_tolerance_m)).covers(connector_2d)
        if not valid:
            self._issue("warning", "vertical_access_not_visible",
                f"Rejected vertical access from {vertical.name} to {space.name}",
                "Associate a landing door/opening or refine the walkable landing geometry",
                related_ifc_guid=vertical.ifc_guid, related_node_id=stop_id, geometry=connector)
            return
        self._add_bidirectional_edge(stop_id, target, edge_type="vertical_access",
            mobility_mode="walk", subgraph_id=subgraph_id, geometry=connector,
            accessible_wheelchair=vertical.accessible_wheelchair,
            restriction_reason=None if vertical.accessible_wheelchair else "vertical_element_noncompliant",
            relation_source="geometry_inferred", confidence=max(0.35, 1.0 - distance / self.config.door_projection_max_distance_m))

    def apply_profile_costs(self) -> None:
        for _, _, _, data in self.graph.edges(keys=True, data=True):
            time_cost = float(data.get("estimated_time") or 0.0)
            effort = float(data.get("effort_cost") or 0.0)
            data["cost_general"] = time_cost + 0.15 * effort
            data["cost_wheelchair"] = (
                time_cost + 0.30 * effort if data.get("accessible_wheelchair", False) else float("inf")
            )

    def compute_route(
        self, source_node: str, target_node: str, user_profile: str = "general",
        cost_attribute: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return an accessibility-filtered minimum-cost route in a MultiDiGraph."""
        if source_node not in self.graph or target_node not in self.graph:
            raise KeyError("Source and target must be existing graph node IDs")
        profile = user_profile.casefold()
        if profile not in {"general", "wheelchair"}:
            raise ValueError("user_profile must be 'general' or 'wheelchair'")
        attribute = cost_attribute or f"cost_{profile}"
        filtered = nx.MultiDiGraph()
        filtered.add_nodes_from(self.graph.nodes(data=True))
        for u, v, key, data in self.graph.edges(keys=True, data=True):
            if profile == "wheelchair" and not data.get("accessible_wheelchair", False):
                continue
            cost = float(data.get(attribute, data.get("estimated_time", 1.0)))
            if math.isfinite(cost):
                filtered.add_edge(u, v, key=key, **data, _route_cost=cost)
        nodes = nx.shortest_path(filtered, source_node, target_node, weight="_route_cost")
        edges = []
        total = 0.0
        for u, v in zip(nodes, nodes[1:]):
            key, data = min(filtered[u][v].items(), key=lambda item: item[1]["_route_cost"])
            edges.append(data["edge_id"])
            total += data["_route_cost"]
        return {"source": source_node, "target": target_node, "profile": profile, "node_ids": nodes, "edge_ids": edges, "total_cost": total}

    def validate_graph(self) -> list[ValidationIssue]:
        """Run semantic, geometry, hierarchy and topology diagnostics."""
        for space in self.spaces.values():
            if space.footprint is None:
                continue  # already reported during extraction
            if not space.connected_door_ids:
                self._issue("warning", "space_without_doors", f"Space {space.name} has no associated door", "Review door-space relations or non-door access", related_ifc_guid=space.ifc_guid, related_node_id=space.space_id, geometry=space.interior_point)
            if not space.footprint.is_valid:
                self._issue("error", "invalid_space_polygon", f"Space {space.name} has an invalid polygon", "Repair source geometry", related_ifc_guid=space.ifc_guid, related_node_id=space.space_id, geometry=space.interior_point)
            if space.node_class == "horizontal_mobility" and not any(s["parent_node_id"] == space.space_id for s in self.subgraphs):
                self._issue("error", "mobility_node_without_subgraph", f"Mobility space {space.name} has no internal subgraph", "Review skeletonisation and geometry", related_ifc_guid=space.ifc_guid, related_node_id=space.space_id, geometry=space.interior_point)
        for door in self.doors.values():
            if not door.connected_space_ids:
                self._issue("warning", "door_without_connected_spaces", f"Door {door.name} has no connected spaces", "Add space boundaries or review geometric tolerance", related_ifc_guid=door.ifc_guid, related_node_id=door.door_id, geometry=door.point)
            elif len(door.connected_space_ids) == 1:
                self._issue("info", "door_with_one_space", f"Door {door.name} connects one modelled space", "Confirm whether it is external", related_ifc_guid=door.ifc_guid, related_node_id=door.door_id, geometry=door.point)
            if door.width is None:
                self._issue("info", "missing_door_width", f"Door {door.name} has no usable width", "Populate OverallWidth or a recognised property", related_ifc_guid=door.ifc_guid, related_node_id=door.door_id, geometry=door.point)
        for vertical in self.vertical_elements.values():
            if len(vertical.connected_storeys) < 2:
                self._issue("warning", "vertical_incomplete_storeys", f"{vertical.name} is linked to fewer than two storeys", "Review containment, trajectory and grouping", related_ifc_guid=vertical.ifc_guid, related_node_id=vertical.vertical_id)
        for u, v, key, data in self.graph.edges(keys=True, data=True):
            if float(data.get("length_3d", 0.0)) <= 1e-9:
                self._issue("error", "zero_length_edge", f"Edge {key} has zero length", "Remove or rebuild the degenerate edge", related_node_id=u, geometry=data.get("geometry"))
            if data.get("validation_status") == "rejected_crosses_boundary":
                self._issue("error", "edge_crosses_walkable_boundary", f"Edge {key} leaves its walkable domain", "Review inferred relation; do not use this edge for routing", related_node_id=u, geometry=data.get("geometry"))
        simple = nx.Graph(self.graph)
        components = list(nx.connected_components(simple)) if simple.number_of_nodes() else []
        if len(components) > 1:
            self._issue("warning", "disconnected_graph", f"Graph contains {len(components)} connected components", "Inspect unresolved portals and vertical connections")
        # Overlap diagnostics use the spatial index and avoid O(n^2) all-pairs.
        if self._space_tree is not None:
            seen: set[tuple[str, str]] = set()
            for space in self._tree_spaces:
                try:
                    indexes = self._space_tree.query(space.footprint)
                    neighbors = [self._tree_spaces[int(i)] for i in indexes]
                except (TypeError, ValueError):
                    neighbors = []
                for other in neighbors:
                    pair = tuple(sorted((space.space_id, other.space_id)))
                    if space.space_id == other.space_id or pair in seen or space.storey_id != other.storey_id:
                        continue
                    seen.add(pair)
                    overlap = space.footprint.intersection(other.footprint).area
                    if overlap > max(0.05, self.config.spatial_tolerance_m ** 2):
                        self._issue("warning", "overlapping_spaces", f"Spaces {space.name} and {other.name} overlap by {overlap:.2f} m²", "Review IfcSpace solids and phase/design-option filtering", related_ifc_guid=space.ifc_guid, related_node_id=space.space_id, geometry=space.footprint.intersection(other.footprint).representative_point())
        LOGGER.info("Validation produced %d issues", len(self.issues))
        return self.issues

    def validation_dataframe(self):
        import pandas as pd
        return pd.DataFrame([{k: v for k, v in asdict(issue).items() if k != "geometry"} for issue in self.issues])

    def summary(self) -> dict[str, Any]:
        general_components = nx.number_connected_components(nx.Graph(self.graph)) if self.graph.number_of_nodes() else 0
        wheelchair = nx.Graph()
        wheelchair.add_nodes_from(n for n, d in self.graph.nodes(data=True) if d.get("accessible_wheelchair") is not False)
        wheelchair.add_edges_from((u, v) for u, v, d in self.graph.edges(data=True) if d.get("accessible_wheelchair", False))
        return {
            "IFC file": self.model_metadata.get("source_ifc_filename"), "IFC schema": self.model.schema,
            "Storeys": len(self.storeys), "Spaces": len(self.spaces), "Doors": len(self.doors),
            "Finalist nodes": sum(s.node_class == "finalist" for s in self.spaces.values()),
            "Horizontal mobility nodes": sum(s.node_class == "horizontal_mobility" for s in self.spaces.values()),
            "Vertical mobility nodes": len(self.vertical_elements),
            "Internal nodes": sum(d.get("hierarchy_level") == 2 for _, d in self.graph.nodes(data=True)),
            "Graph nodes": self.graph.number_of_nodes(), "Graph edges": self.graph.number_of_edges(),
            "Connected components": general_components,
            "Wheelchair-accessible components": nx.number_connected_components(wheelchair) if wheelchair.number_of_nodes() else 0,
            "Validation warnings": sum(i.severity in ("warning", "error") for i in self.issues),
        }

    def export_geopackage(self, output_path: str | Path) -> Path:
        """Export spatial layers plus relational tables to a single GeoPackage."""
        try:
            import geopandas as gpd
            import pandas as pd
        except ImportError as exc:
            raise ImportError("GeoPackage export requires geopandas, pandas, fiona/pyogrio") from exc
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        crs = self.config.manual_crs if self.model_metadata.get("georeferenced") else None

        spaces_rows = []
        for space in self.spaces.values():
            spaces_rows.append({
                "space_id": space.space_id, "ifc_guid": space.ifc_guid, "name": space.name,
                "storey_id": space.storey_id, "space_type": space.object_type,
                "node_class": space.node_class, "number_of_doors": space.number_of_doors,
                "is_horizontal_mobility": space.node_class == "horizontal_mobility",
                "classification_source": space.classification_source,
                "metadata_json": _json({"ifc_entity_id": space.ifc_entity_id, "ifc_class": space.ifc_class, "long_name": space.long_name, "area": space.area, "aspect_ratio": space.aspect_ratio, "classification_score": space.classification_score, "extraction_method": space.extraction_method, "properties": space.properties}),
                "geometry": space.footprint,
            })
        door_rows = []
        has_v2_door_metadata = any(
            "HSIMG.inout" in door.properties for door in self.doors.values()
        )
        for door in self.doors.values():
            row = {
                "door_id": door.door_id, "ifc_guid": door.ifc_guid,
                "name": door.name, "storey_id": door.storey_id,
                "width": door.width, "height": door.height,
                "threshold_height": door.threshold_height,
                "connected_space_a": door.connected_space_ids[0] if door.connected_space_ids else None,
                "connected_space_b": door.connected_space_ids[1] if len(door.connected_space_ids) > 1 else None,
                "wheelchair_accessible": door.wheelchair_accessible,
                "metadata_json": _json({
                    "ifc_entity_id": door.ifc_entity_id,
                    "operation_type": door.operation_type,
                    "relation_source": door.relation_source,
                    "confidence": door.confidence,
                    "properties": door.properties,
                }),
                "geometry": door.point,
            }
            if has_v2_door_metadata:
                row.update({
                    "inout": door.properties.get("HSIMG.inout"),
                    "door_status": door.properties.get("HSIMG.door_status"),
                    "public_access": door.properties.get("HSIMG.public_access"),
                    "door_status_source": door.properties.get("HSIMG.door_status_source"),
                })
            door_rows.append(row)
        node_rows = []
        for node_id, data in self.graph.nodes(data=True):
            node_rows.append({
                "node_id": node_id, "node_type": data.get("node_type"), "node_role": data.get("node_role"),
                "mobility_type": data.get("mobility_type"), "parent_node_id": data.get("parent_node_id"),
                "subgraph_id": data.get("subgraph_id"), "hierarchy_level": data.get("hierarchy_level"),
                "ifc_guid": data.get("ifc_guid"), "storey_id": data.get("storey_id"),
                "accessible_general": data.get("accessible_general"),
                "accessible_wheelchair": data.get("accessible_wheelchair"),
                "metadata_json": data.get("metadata_json"), "geometry": data.get("geometry"),
            })
        edge_rows = []
        for source, target, key, data in self.graph.edges(keys=True, data=True):
            edge_rows.append({
                "edge_id": data.get("edge_id", key), "source_id": source, "target_id": target,
                "edge_type": data.get("edge_type"), "mobility_mode": data.get("mobility_mode"),
                "subgraph_id": data.get("subgraph_id"), "length_3d": data.get("length_3d"),
                "vertical_displacement": data.get("vertical_displacement"), "estimated_time": data.get("estimated_time"),
                "effort_cost": data.get("effort_cost"), "accessible_general": data.get("accessible_general"),
                "accessible_wheelchair": data.get("accessible_wheelchair"), "restriction_reason": data.get("restriction_reason"),
                "relation_source": data.get("relation_source"), "validation_status": data.get("validation_status"),
                "metadata_json": data.get("metadata_json"), "geometry": data.get("geometry"),
            })
        vertical_rows = [{
            "vertical_id": v.vertical_id, "ifc_guid": v.ifc_guid, "vertical_type": v.vertical_type,
            "connected_storeys": _json(v.connected_storeys), "accessible_wheelchair": v.accessible_wheelchair,
            "metadata_json": _json({"ifc_entity_id": v.ifc_entity_id, "ifc_class": v.ifc_class, "name": v.name, "width": v.width, "slope": v.slope, "extraction_method": v.extraction_method, "confidence": v.confidence, "properties": v.properties}),
            "geometry": v.path,
        } for v in self.vertical_elements.values()]
        issue_rows = [{
            "issue_id": i.issue_id, "severity": i.severity, "issue_type": i.issue_type,
            "related_ifc_guid": i.related_ifc_guid, "related_node_id": i.related_node_id,
            "message": i.message, "suggested_action": i.suggested_action, "geometry": i.geometry,
        } for i in self.issues]

        layers = {
            "spaces": spaces_rows, "doors": door_rows, "graph_nodes": node_rows,
            "graph_edges": edge_rows, "mobility_axes": self.mobility_axes,
            "vertical_elements": vertical_rows, "validation_issues": issue_rows,
        }
        wrote_any = False
        for layer, rows in layers.items():
            if not rows:
                LOGGER.warning("GeoPackage layer %s is empty and will be represented by an empty relational table", layer)
                continue
            frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)
            frame.to_file(output, layer=layer, driver="GPKG", mode="w" if not wrote_any else "a", index=False)
            wrote_any = True
        if not wrote_any:
            raise RuntimeError("No spatial records were available for GeoPackage creation")

        with sqlite3.connect(output) as connection:
            pd.DataFrame(self.subgraphs).to_sql("subgraphs", connection, if_exists="replace", index=False)
            pd.DataFrame(self.user_profiles).to_sql("user_profiles", connection, if_exists="replace", index=False)
            pd.DataFrame([self.model_metadata]).to_sql("model_metadata", connection, if_exists="replace", index=False)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for table, description in (
                ("subgraphs", "Flattened HSIMG hierarchy registry"),
                ("user_profiles", "Accessibility routing profiles"),
                ("model_metadata", "IFC source and processing provenance"),
            ):
                connection.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (table,))
                connection.execute(
                    "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change) VALUES (?, 'attributes', ?, ?, ?)",
                    (table, table, description, now),
                )
            for layer, rows in layers.items():
                if not rows:
                    pd.DataFrame(columns=self._empty_layer_columns(layer)).to_sql(layer, connection, if_exists="replace", index=False)
        LOGGER.info("Exported GeoPackage: %s", output)
        return output

    @staticmethod
    def _empty_layer_columns(layer: str) -> list[str]:
        schemas = {
            "spaces": ["space_id", "ifc_guid", "name", "storey_id", "space_type", "node_class", "number_of_doors", "is_horizontal_mobility", "classification_source", "metadata_json"],
            "doors": ["door_id", "ifc_guid", "name", "storey_id", "width", "height", "threshold_height", "connected_space_a", "connected_space_b", "wheelchair_accessible", "metadata_json"],
            "graph_nodes": ["node_id", "node_type", "node_role", "mobility_type", "parent_node_id", "subgraph_id", "hierarchy_level", "ifc_guid", "storey_id", "accessible_general", "accessible_wheelchair", "metadata_json"],
            "graph_edges": ["edge_id", "source_id", "target_id", "edge_type", "mobility_mode", "subgraph_id", "length_3d", "vertical_displacement", "estimated_time", "effort_cost", "accessible_general", "accessible_wheelchair", "restriction_reason", "relation_source", "validation_status", "metadata_json"],
            "mobility_axes": ["axis_id", "parent_node_id", "subgraph_id", "mobility_type", "extraction_method", "pruning_threshold", "metadata_json"],
            "vertical_elements": ["vertical_id", "ifc_guid", "vertical_type", "connected_storeys", "accessible_wheelchair", "metadata_json"],
            "validation_issues": ["issue_id", "severity", "issue_type", "related_ifc_guid", "related_node_id", "message", "suggested_action"],
        }
        return schemas[layer]

    def export_graph(self, graphml_path: str | Path, json_path: str | Path) -> None:
        """Export attribute-safe GraphML and node-link JSON."""
        graphml_path = Path(graphml_path)
        json_path = Path(json_path)
        graphml_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        safe_graph = nx.MultiDiGraph()
        safe_graph.graph.update({k: _json(v) if isinstance(v, (dict, list, tuple)) else v for k, v in self.graph.graph.items()})
        for node_id, data in self.graph.nodes(data=True):
            safe_graph.add_node(node_id, **{k: (v.wkt if hasattr(v, "wkt") else _json(v) if isinstance(v, (dict, list, tuple)) else "" if v is None else v) for k, v in data.items()})
        for u, v, key, data in self.graph.edges(keys=True, data=True):
            safe_graph.add_edge(u, v, key=key, **{k: (value.wkt if hasattr(value, "wkt") else _json(value) if isinstance(value, (dict, list, tuple)) else "" if value is None else value) for k, value in data.items()})
        nx.write_graphml(safe_graph, graphml_path)
        payload = _json_compatible(nx.node_link_data(safe_graph, edges="edges"))
        if json_path.exists():
            json_path.unlink()
        json_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                default=str,
                allow_nan=False,
            ),
            encoding="utf-8",
        )

    def visualize_storey(self, storey_id: Optional[str] = None, ax: Any = None):
        """Plot spaces, doors, mobility axes and graph nodes for one storey."""
        import matplotlib.pyplot as plt
        if storey_id is None:
            storey_id = next(iter(self.storeys), None)
        if ax is None:
            _, ax = plt.subplots(figsize=(12, 10))
        for space in self.spaces.values():
            if space.storey_id != storey_id or space.footprint is None:
                continue
            geoms = space.footprint.geoms if isinstance(space.footprint, MultiPolygon) else [space.footprint]
            for geom in geoms:
                x, y = geom.exterior.xy
                ax.fill(x, y, alpha=0.18, color="#ed7d31" if space.node_class == "horizontal_mobility" else "#5b9bd5")
        for door in self.doors.values():
            if door.storey_id == storey_id and door.point is not None:
                ax.scatter(door.point.x, door.point.y, s=8, c="#c00000", marker="s")
        for axis in self.mobility_axes:
            parent = axis["parent_node_id"]
            if parent in self.spaces and self.spaces[parent].storey_id == storey_id:
                geometry = axis["geometry"]
                lines = geometry.geoms if isinstance(geometry, MultiLineString) else [geometry]
                for line in lines:
                    coords = np.asarray(line.coords)
                    ax.plot(coords[:, 0], coords[:, 1], color="#00a36c", linewidth=1.2)
        ax.set_aspect("equal", adjustable="box")
        title = self.storeys[storey_id].name if storey_id in self.storeys else str(storey_id)
        ax.set_title(f"HSIMG - {title}")
        ax.set_xlabel("X [m]"); ax.set_ylabel("Y [m]")
        return ax

    def visualize_3d(self, route: Optional[Mapping[str, Any]] = None, ax: Any = None):
        import matplotlib.pyplot as plt
        if ax is None:
            figure = plt.figure(figsize=(12, 9))
            ax = figure.add_subplot(111, projection="3d")
        route_edges = set(route.get("edge_ids", [])) if route else set()
        for _, _, key, data in self.graph.edges(keys=True, data=True):
            geometry = data.get("geometry")
            if geometry is None:
                continue
            coords = np.asarray(geometry.coords)
            if coords.shape[1] == 2:
                coords = np.column_stack([coords, np.zeros(len(coords))])
            ax.plot(coords[:, 0], coords[:, 1], coords[:, 2], color="#c00000" if key in route_edges else "#6f6f6f", linewidth=2.5 if key in route_edges else 0.6, alpha=1.0 if key in route_edges else 0.5)
        ax.set_xlabel("X [m]"); ax.set_ylabel("Y [m]"); ax.set_zlabel("Z [m]")
        ax.set_title("3D Hierarchical Semantic Indoor Mobility Graph")
        return ax

    def run_all(self) -> "HSIMGBuilder":
        """Execute the staged pipeline while preserving partial results."""
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
        self.validate_graph()
        return self


class HSIMGBuilder(_HSIMGBuilderCore):
    """Unified V3 builder with vector axes and door/access semantics."""

    def __init__(
        self,
        ifc_model: Any,
        config: HSIMGConfig | Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(ifc_model, config)
        self.door_access_metadata: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _normalise_boolean(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        text = str(value).strip().casefold()
        if text in {
            "true", "1", "yes", "si", "sí", "open", "opened", "abierta",
            "abierto", "external", "exterior", "public", "unlocked",
        }:
            return True
        if text in {
            "false", "0", "no", "closed", "cerrada", "cerrado", "internal",
            "interior", "private", "locked", "restricted",
        }:
            return False
        return None

    def _door_open_state(self, door: DoorRecord) -> tuple[bool, str]:
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
                    r"publicaccess|securitylevel|estado|abierta|cerrada|bloqueada",
                    field_name,
                ):
                    continue
                inferred = self._normalise_boolean(raw_value)
                if inferred is None:
                    continue
                if isinstance(raw_value, (bool, int, float)) and re.search(
                    r"closed|locked|cerrad|bloquead",
                    field_name,
                ):
                    inferred = not inferred
                return inferred, f"IFC_property:{key}"
        return bool(self.config.default_door_open), "config:default_door_open"

    def _classify_door_access(self) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {}
        for door_id, door in self.doors.items():
            explicit_external: Optional[bool] = None
            inout_source: Optional[str] = None
            for key, value in door.properties.items():
                if key.casefold().endswith(".isexternal"):
                    explicit_external = self._normalise_boolean(value)
                    if explicit_external is not None:
                        inout_source = f"IFC_property:{key}"
                    break
            if explicit_external is None:
                external = len(door.connected_space_ids) == 1
                inout_source = "geometry:single_connected_space"
            else:
                external = explicit_external
            is_open, status_source = self._door_open_state(door)
            metadata[door_id] = {
                "inout": int(external),
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
        for door_id, metadata in self.door_access_metadata.items():
            related_nodes = {door_id}
            related_nodes.update(
                node_id
                for node_id, data in self.graph.nodes(data=True)
                if data.get("parent_node_id") == door_id
            )
            for node_id in related_nodes:
                if node_id not in self.graph:
                    continue
                node = self.graph.nodes[node_id]
                node.update(metadata)
                node["accessible_general"] = metadata["door_open"]
                node["accessible_wheelchair"] = bool(
                    node.get("accessible_wheelchair") is not False
                    and metadata["door_open"]
                )
                node["metadata_json"] = self._merge_metadata_json(
                    node.get("metadata_json"),
                    metadata,
                )
            for _, _, _, data in self.graph.edges(
                related_nodes,
                keys=True,
                data=True,
            ):
                data.update(metadata)
                data["metadata_json"] = self._merge_metadata_json(
                    data.get("metadata_json"),
                    metadata,
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
        self._apply_door_metadata_to_graph()
        super().apply_profile_costs()
        for _, _, _, data in self.graph.edges(keys=True, data=True):
            if not data.get("accessible_general", True):
                data["cost_general"] = float("inf")
            if not data.get("accessible_wheelchair", False):
                data["cost_wheelchair"] = float("inf")

    def validate_graph(self) -> list[ValidationIssue]:
        issues = super().validate_graph()
        existing = {
            (issue.issue_type, issue.related_node_id)
            for issue in self.issues
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
                "Open it in the V3 configuration or IFC properties when allowed",
                related_ifc_guid=door.ifc_guid,
                related_node_id=door_id,
                geometry=door.point,
            )
        return issues

    def export_geopackage(self, output_path: str | Path) -> Path:
        if not self.door_access_metadata:
            self._classify_door_access()
        for door_id, metadata in self.door_access_metadata.items():
            self.doors[door_id].properties.update({
                "HSIMG.inout": metadata["inout"],
                "HSIMG.inout_source": metadata["inout_source"],
                "HSIMG.door_status": metadata["door_status"],
                "HSIMG.public_access": metadata["public_access"],
                "HSIMG.door_status_source": metadata["door_status_source"],
            })
        output = super().export_geopackage(output_path)
        with sqlite3.connect(output) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(doors)")
            }
        required = {"inout", "door_status", "public_access", "door_status_source"}
        if not required.issubset(columns):
            raise RuntimeError(
                f"GeoPackage doors layer is missing V3 columns: {sorted(required - columns)}"
            )
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        result.update({
            "Horizontal axis method": (
                "vector_boundary_voronoi_medial_axis"
                if self.config.prefer_vector_medial_axis
                else "raster_medial_axis"
            ),
            "Exterior entrance/exit doors": sum(
                metadata["inout"] == 1
                for metadata in self.door_access_metadata.values()
            ),
            "Closed doors": sum(
                metadata["door_status"] == "closed"
                for metadata in self.door_access_metadata.values()
            ),
        })
        return result


def print_summary(builder: HSIMGBuilder, geopackage_path: Optional[str | Path] = None) -> None:
    summary = builder.summary()
    if geopackage_path is not None:
        summary["GeoPackage output path"] = str(geopackage_path)
    print("\n".join(f"{key}: {value}" for key, value in summary.items()))


__all__ = [
    "DoorRecord", "GeometryEngine", "HSIMGBuilder", "HSIMGConfig", "SpaceRecord",
    "StoreyRecord", "ValidationIssue", "VerticalMobilityRecord", "print_summary",
]
