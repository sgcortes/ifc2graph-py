"""HSIMG V5: clearance-aware pedestrian graphs and explicit access semantics.

V5 keeps the obstacle-aware vector axes introduced by V4, but no longer
assumes that every geometrically possible gap is a pedestrian route.  It
measures local clearance on horizontal axis edges, separates IFC-external
doors from usable entrance/exit portals, classifies vehicle-only ramps, and
exports ramp footprints for the web explorer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Optional, Sequence

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

import v4hsimg as v4


def _boolean(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "si", "sí", "y", "public", "open"}:
        return True
    if text in {"false", "0", "no", "n", "private", "closed", "restricted"}:
        return False
    return None


def _property_boolean(
    properties: Mapping[str, Any],
    suffixes: Sequence[str],
) -> tuple[Optional[bool], Optional[str]]:
    normalized_suffixes = {
        re.sub(r"[^a-z0-9]", "", suffix.casefold()) for suffix in suffixes
    }
    for key, value in properties.items():
        field = re.sub(
            r"[^a-z0-9]",
            "",
            key.rsplit(".", 1)[-1].casefold(),
        )
        if field not in normalized_suffixes:
            continue
        resolved = _boolean(value)
        if resolved is not None:
            return resolved, key
    return None, None


def classify_ramp_use(
    name: str,
    properties: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify pedestrian/vehicle use without treating every IfcRamp alike."""
    pedestrian, pedestrian_key = _property_boolean(
        properties,
        (
            "HIMG Pedestrian Access",
            "HSIMG Pedestrian Access",
            "PedestrianAccess",
            "PublicAccess",
        ),
    )
    vehicle, vehicle_key = _property_boolean(
        properties,
        ("HIMG Vehicle Access", "HSIMG Vehicle Access", "VehicleAccess"),
    )
    folded = name.casefold()
    vehicle_name = bool(
        re.search(r"\b(coche|coches|car|cars|vehicle|vehicular|garage|garaje)\b", folded)
    )
    pedestrian_name = bool(
        re.search(r"\b(peatonal|pedestrian|walkway|accessible|accesible)\b", folded)
    )

    if pedestrian is False or (vehicle_name and pedestrian is not True):
        route_type = "vehicle_only"
        pedestrian_access = False
        vehicle_access = True if vehicle is None else vehicle
    elif pedestrian is True and (vehicle is True or vehicle_name):
        route_type = "mixed"
        pedestrian_access = True
        vehicle_access = True
    elif pedestrian is True or pedestrian_name:
        route_type = "pedestrian"
        pedestrian_access = True
        vehicle_access = False if vehicle is None else vehicle
    else:
        # IfcRamp is normally a pedestrian circulation element.  Only explicit
        # properties or strong vehicle semantics change that safe default.
        route_type = "pedestrian"
        pedestrian_access = True
        vehicle_access = False if vehicle is None else vehicle

    source_parts = []
    if pedestrian_key:
        source_parts.append(f"IFC_property:{pedestrian_key}")
    if vehicle_key:
        source_parts.append(f"IFC_property:{vehicle_key}")
    if not source_parts:
        source_parts.append("semantic:name" if vehicle_name or pedestrian_name else "IFC_class:IfcRamp")
    return {
        "route_type": route_type,
        "pedestrian_access": pedestrian_access,
        "vehicle_access": vehicle_access,
        "classification_source": "+".join(source_parts),
    }


def cleaned_clearance_domain(
    polygon: Polygon | MultiPolygon,
    minimum_hole_area_m2: float,
) -> Polygon | MultiPolygon:
    """Use the same obstacle policy as the V4 medial-axis construction."""
    parts = list(polygon.geoms) if isinstance(polygon, MultiPolygon) else [polygon]
    cleaned: list[Polygon] = []
    for part in parts:
        holes = [
            ring.coords
            for ring in part.interiors
            if Polygon(ring).area >= minimum_hole_area_m2
        ]
        candidate = Polygon(part.exterior.coords, holes)
        if not candidate.is_valid:
            candidate = candidate.buffer(0)
        if isinstance(candidate, Polygon) and not candidate.is_empty:
            cleaned.append(candidate)
        elif isinstance(candidate, MultiPolygon):
            cleaned.extend(item for item in candidate.geoms if not item.is_empty)
    if not cleaned:
        return polygon
    return cleaned[0] if len(cleaned) == 1 else MultiPolygon(cleaned)


def minimum_route_width(
    line: LineString,
    polygon: Polygon | MultiPolygon,
    sample_spacing_m: float = 0.10,
    trim_ratio: float = 0.05,
) -> float:
    """Return twice the minimum sampled clearance along the useful edge body."""
    line_2d = LineString([(coord[0], coord[1]) for coord in line.coords])
    if line_2d.length <= 1e-9:
        return 0.0
    count = max(3, int(math.ceil(line_2d.length / sample_spacing_m)) + 1)
    start = max(0.0, min(0.45, trim_ratio))
    stop = 1.0 - start
    samples = (
        line_2d.interpolate(float(fraction), normalized=True)
        for fraction in np.linspace(start, stop, count)
    )
    clearances = [point.distance(polygon.boundary) for point in samples]
    return 2.0 * min(clearances) if clearances else 0.0


@dataclass(slots=True)
class HSIMGConfig(v4.HSIMGConfig):
    """V5 configuration for route clearance and ramp semantics."""

    general_min_route_width_m: float = 0.90
    wheelchair_min_route_width_m: float = 1.20
    route_width_sample_spacing_m: float = 0.10
    route_width_trim_ratio: float = 0.05

    def __post_init__(self) -> None:
        super().__post_init__()
        for field_name in (
            "general_min_route_width_m",
            "wheelchair_min_route_width_m",
            "route_width_sample_spacing_m",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.wheelchair_min_route_width_m < self.general_min_route_width_m:
            raise ValueError(
                "wheelchair_min_route_width_m must be at least the general minimum"
            )
        if not 0 <= self.route_width_trim_ratio < 0.5:
            raise ValueError("route_width_trim_ratio must be in [0, 0.5)")


class HSIMGBuilder(v4.HSIMGBuilder):
    """V5 builder layered on the stable obstacle-aware V4 pipeline."""

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
            raise TypeError("V5 HSIMGBuilder requires HSIMGConfig or a mapping")
        super().__init__(ifc_model, resolved)
        self.config: HSIMGConfig = resolved
        self.vertical_footprints: dict[str, Polygon | MultiPolygon] = {}
        self.clearance_restricted_directed_edges = 0

    def _classify_door_access(self) -> dict[str, dict[str, Any]]:
        metadata = super()._classify_door_access()
        for door_id, door in self.doors.items():
            item = metadata[door_id]
            connected_count = len(door.connected_space_ids)
            ifc_external, external_key = _property_boolean(
                door.properties,
                ("IsExternal",),
            )
            inferred_external = connected_count == 1
            entrance_exit = connected_count == 1 and ifc_external is not False
            orphan_external = bool(ifc_external is True and connected_count == 0)
            inconsistent_external = bool(
                ifc_external is True and connected_count != 1
            )
            explicit_public, public_key = _property_boolean(
                door.properties,
                (
                    "HIMG Public Access",
                    "HSIMG Public Access",
                    "PublicAccess",
                ),
            )
            public_access = (
                explicit_public
                if explicit_public is not None
                else bool(item["door_open"])
            )
            if orphan_external:
                public_access = False

            item.update({
                "ifc_is_external": ifc_external,
                "ifc_is_external_source": (
                    f"IFC_property:{external_key}" if external_key else None
                ),
                "geometry_single_space_external": inferred_external,
                "inout": int(entrance_exit),
                "inout_source": (
                    f"IFC_property:{external_key}+connected_space_count"
                    if external_key
                    else "geometry:single_connected_space"
                ),
                "entrance_exit_eligible": entrance_exit,
                "orphan_external": orphan_external,
                "external_space_mismatch": inconsistent_external,
                "public_access": bool(public_access),
                "public_access_source": (
                    f"IFC_property:{public_key}"
                    if public_key
                    else "door_open_fallback"
                ),
                "connected_space_count": connected_count,
            })
        self.door_access_metadata = metadata
        return metadata

    def _apply_door_metadata_to_graph(self) -> None:
        super()._apply_door_metadata_to_graph()
        for door_id, metadata in self.door_access_metadata.items():
            if metadata["connected_space_count"] != 0:
                continue
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
                node["accessible_general"] = False
                node["accessible_wheelchair"] = False
                node["metadata_json"] = self._merge_metadata_json(
                    node.get("metadata_json"),
                    metadata,
                )
            for source, target, key, data in self.graph.edges(
                related_nodes,
                keys=True,
                data=True,
            ):
                data["accessible_general"] = False
                data["accessible_wheelchair"] = False
                data["restriction_reason"] = "door_without_connected_space"
                data["metadata_json"] = self._merge_metadata_json(
                    data.get("metadata_json"),
                    metadata,
                )

    def _space_for_edge(self, source: str, target: str) -> Any:
        candidates = []
        for node_id in (source, target):
            data = self.graph.nodes.get(node_id, {})
            for candidate in (data.get("parent_node_id"), node_id):
                if candidate in self.spaces:
                    candidates.append(self.spaces[candidate])
        return candidates[0] if candidates else None

    def _apply_horizontal_clearance(self) -> None:
        restricted_by_space: dict[str, int] = {}
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
            domain = cleaned_clearance_domain(
                space.footprint,
                self.config.medial_axis_min_hole_area_m2,
            )
            width = minimum_route_width(
                geometry,
                domain,
                self.config.route_width_sample_spacing_m,
                self.config.route_width_trim_ratio,
            )
            roles = {
                self.graph.nodes[node_id].get("node_role")
                for node_id in (source, target)
            }
            door_transition = "door_projection" in roles
            general_ok = door_transition or (
                width >= self.config.general_min_route_width_m
            )
            wheelchair_ok = door_transition or (
                width >= self.config.wheelchair_min_route_width_m
            )
            previous_general = data.get("accessible_general") is not False
            previous_wheelchair = data.get("accessible_wheelchair") is not False
            data["accessible_general"] = bool(previous_general and general_ok)
            data["accessible_wheelchair"] = bool(
                previous_wheelchair and wheelchair_ok
            )
            if not data["accessible_general"]:
                data["restriction_reason"] = "insufficient_general_clearance"
                data["validation_status"] = "restricted"
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
                    "general_min_route_width_m": (
                        self.config.general_min_route_width_m
                    ),
                    "wheelchair_min_route_width_m": (
                        self.config.wheelchair_min_route_width_m
                    ),
                    "clearance_method": "minimum_sampled_medial_clearance_v5",
                    "door_transition_exemption": door_transition,
                },
            )
        self.clearance_restricted_directed_edges = restricted
        for space_id, directed_count in restricted_by_space.items():
            space = self.spaces[space_id]
            self._issue(
                "info",
                "narrow_general_routes_restricted",
                f"V5 restricted {directed_count // 2} narrow route segments in {space.name}",
                "Review obstacle geometry and configured pedestrian clearance",
                related_ifc_guid=space.ifc_guid,
                related_node_id=space_id,
                geometry=space.interior_point,
            )

    def build_horizontal_subgraphs(self) -> list[dict[str, Any]]:
        result = super().build_horizontal_subgraphs()
        self._apply_horizontal_clearance()
        return result

    def extract_ramps(self) -> dict[str, Any]:
        result = super().extract_ramps()
        for parent in self.model.by_type("IfcRamp"):
            vertical_id = v4.v3.stable_id("ramp", parent.GlobalId)
            record = self.vertical_elements.get(vertical_id)
            if record is None:
                continue
            flights = [
                obj
                for relation in getattr(parent, "IsDecomposedBy", ()) or ()
                for obj in relation.RelatedObjects
                if obj.is_a("IfcRampFlight")
            ]
            footprint_parts = []
            for entity in flights or [parent]:
                try:
                    footprint = self.geometry.mesh(entity).footprint
                except Exception:
                    footprint = None
                if footprint is not None and not footprint.is_empty:
                    footprint_parts.append(footprint)
            if footprint_parts:
                footprint = unary_union(footprint_parts)
                if isinstance(footprint, (Polygon, MultiPolygon)):
                    self.vertical_footprints[vertical_id] = footprint

            classification = classify_ramp_use(record.name, record.properties)
            record.properties.update({
                "HSIMG.RouteType": classification["route_type"],
                "HSIMG.PedestrianAccess": classification["pedestrian_access"],
                "HSIMG.VehicleAccess": classification["vehicle_access"],
                "HSIMG.RampClassificationSource": classification[
                    "classification_source"
                ],
            })
            declared_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", record.name)
            if declared_match:
                declared = float(declared_match.group(1).replace(",", ".")) / 100.0
                record.properties["HSIMG.DeclaredSlopeFromName"] = declared
                if record.slope is not None and abs(record.slope - declared) > 0.03:
                    self._issue(
                        "warning",
                        "ramp_slope_name_mismatch",
                        f"{record.name} has derived slope {record.slope:.3f} but its name suggests {declared:.3f}",
                        "Verify Revit ramp Base/Top Levels, offsets and actual slope",
                        related_ifc_guid=record.ifc_guid,
                        related_node_id=record.vertical_id,
                        geometry=record.path,
                    )
        return result

    def build_vertical_subgraphs(self) -> list[dict[str, Any]]:
        result = super().build_vertical_subgraphs()
        for vertical_id, record in self.vertical_elements.items():
            if record.vertical_type != "ramp":
                continue
            route_type = record.properties.get("HSIMG.RouteType", "pedestrian")
            pedestrian_access = bool(
                record.properties.get("HSIMG.PedestrianAccess", True)
            )
            related_nodes = {
                node_id
                for node_id, data in self.graph.nodes(data=True)
                if node_id == vertical_id or data.get("parent_node_id") == vertical_id
            }
            subgraph_ids = {
                self.graph.nodes[node_id].get("subgraph_id")
                for node_id in related_nodes
                if node_id in self.graph
            }
            for node_id in related_nodes:
                node = self.graph.nodes[node_id]
                node["route_type"] = route_type
                node["pedestrian_access"] = pedestrian_access
                if not pedestrian_access:
                    node["accessible_general"] = False
                    node["accessible_wheelchair"] = False
                node["metadata_json"] = self._merge_metadata_json(
                    node.get("metadata_json"),
                    {
                        "route_type": route_type,
                        "pedestrian_access": pedestrian_access,
                        "vehicle_access": record.properties.get(
                            "HSIMG.VehicleAccess"
                        ),
                    },
                )
            for _, _, _, data in self.graph.edges(keys=True, data=True):
                if data.get("subgraph_id") not in subgraph_ids:
                    continue
                data["route_type"] = route_type
                data["pedestrian_access"] = pedestrian_access
                if not pedestrian_access:
                    data["accessible_general"] = False
                    data["accessible_wheelchair"] = False
                    data["restriction_reason"] = "vehicle_only_ramp"
                    data["validation_status"] = "restricted"
                data["metadata_json"] = self._merge_edge_metadata(
                    data.get("metadata_json"),
                    {
                        "route_type": route_type,
                        "pedestrian_access": pedestrian_access,
                        "vehicle_access": record.properties.get(
                            "HSIMG.VehicleAccess"
                        ),
                    },
                )
        return result

    def validate_graph(self) -> list[Any]:
        issues = super().validate_graph()
        existing = {
            (issue.issue_type, issue.related_node_id) for issue in self.issues
        }
        for door_id, metadata in self.door_access_metadata.items():
            if metadata.get("orphan_external"):
                key = ("orphan_external_door", door_id)
                if key not in existing:
                    door = self.doors[door_id]
                    self._issue(
                        "warning",
                        "orphan_external_door",
                        f"External door {door.name} has no connected IfcSpace",
                        "Repair Revit From/To Room and room boundaries",
                        related_ifc_guid=door.ifc_guid,
                        related_node_id=door_id,
                        geometry=door.point,
                    )
            elif metadata.get("external_space_mismatch"):
                key = ("external_door_space_mismatch", door_id)
                if key not in existing:
                    door = self.doors[door_id]
                    self._issue(
                        "warning",
                        "external_door_space_mismatch",
                        f"IFC-external door {door.name} connects {metadata['connected_space_count']} spaces",
                        "Review Revit From/To Room and IsExternal",
                        related_ifc_guid=door.ifc_guid,
                        related_node_id=door_id,
                        geometry=door.point,
                    )
        return issues

    def export_geopackage(self, output_path: str | Path) -> Path:
        for door_id, metadata in self.door_access_metadata.items():
            self.doors[door_id].properties.update({
                "HSIMG.ifc_is_external": metadata.get("ifc_is_external"),
                "HSIMG.orphan_external": metadata.get("orphan_external"),
                "HSIMG.entrance_exit_eligible": metadata.get(
                    "entrance_exit_eligible"
                ),
                "HSIMG.public_access_source": metadata.get(
                    "public_access_source"
                ),
            })
        output = super().export_geopackage(output_path)

        try:
            import geopandas as gpd
        except ImportError as exc:
            raise ImportError("V5 ramp footprint export requires geopandas") from exc

        rows = []
        for vertical_id, footprint in self.vertical_footprints.items():
            record = self.vertical_elements[vertical_id]
            rows.append({
                "vertical_id": vertical_id,
                "ifc_guid": record.ifc_guid,
                "name": record.name,
                "vertical_type": record.vertical_type,
                "route_type": record.properties.get("HSIMG.RouteType"),
                "pedestrian_access": record.properties.get(
                    "HSIMG.PedestrianAccess"
                ),
                "vehicle_access": record.properties.get("HSIMG.VehicleAccess"),
                "connected_storeys": v4.v3._json(record.connected_storeys),
                "metadata_json": v4.v3._json({
                    "slope": record.slope,
                    "width": record.width,
                    "classification_source": record.properties.get(
                        "HSIMG.RampClassificationSource"
                    ),
                    "properties": record.properties,
                }),
                "geometry": footprint,
            })
        if rows:
            crs = self.config.manual_crs if self.model_metadata.get("georeferenced") else None
            gpd.GeoDataFrame(rows, geometry="geometry", crs=crs).to_file(
                output,
                layer="vertical_footprints",
                driver="GPKG",
                mode="a",
                index=False,
            )

        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError("V5 door access export requires pandas") from exc
        access_rows = [
            {
                "door_id": door_id,
                "ifc_guid": self.doors[door_id].ifc_guid,
                "connected_space_count": metadata.get("connected_space_count"),
                "ifc_is_external": metadata.get("ifc_is_external"),
                "orphan_external": metadata.get("orphan_external"),
                "external_space_mismatch": metadata.get(
                    "external_space_mismatch"
                ),
                "entrance_exit_eligible": metadata.get(
                    "entrance_exit_eligible"
                ),
                "public_access": metadata.get("public_access"),
                "public_access_source": metadata.get("public_access_source"),
                "inout_source": metadata.get("inout_source"),
            }
            for door_id, metadata in self.door_access_metadata.items()
        ]
        with sqlite3.connect(output) as connection:
            pd.DataFrame(access_rows).to_sql(
                "door_access_v5",
                connection,
                if_exists="replace",
                index=False,
            )
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            connection.execute(
                "DELETE FROM gpkg_contents WHERE table_name='door_access_v5'"
            )
            connection.execute(
                "INSERT INTO gpkg_contents "
                "(table_name, data_type, identifier, description, last_change) "
                "VALUES ('door_access_v5', 'attributes', 'door_access_v5', "
                "'V5 door exterior/access validation', ?)",
                (now,),
            )
        return output

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        result.update({
            "HSIMG version": 5,
            "Clearance-restricted route segments": (
                self.clearance_restricted_directed_edges // 2
            ),
            "Orphan external doors": sum(
                bool(item.get("orphan_external"))
                for item in self.door_access_metadata.values()
            ),
            "Vehicle-only ramps": sum(
                record.vertical_type == "ramp"
                and record.properties.get("HSIMG.RouteType") == "vehicle_only"
                for record in self.vertical_elements.values()
            ),
            "Vertical footprints": len(self.vertical_footprints),
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
    "classify_ramp_use",
    "cleaned_clearance_domain",
    "minimum_route_width",
    "print_summary",
]
