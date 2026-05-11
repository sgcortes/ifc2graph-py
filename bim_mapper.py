"""
BIMAccessibilityMapper — extraído de v4_BIM2GRAPH_Accesibilidad_Mejorado_Colab.ipynb
Adaptado para la web app: geopandas es opcional (solo para exportar GPKG).
"""

from collections import defaultdict
import math
import os

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as ifc_element
import networkx as nx
from shapely.geometry import LineString, Point

try:
    import geopandas as gpd
    import fiona
    HAS_GEOPANDAS = True
except ImportError:
    HAS_GEOPANDAS = False


class BIMAccessibilityMapper:

    def __init__(self, ifc_path):
        self.ifc_path = ifc_path
        self.model = ifcopenshell.open(ifc_path)
        self.G = nx.Graph()
        self.filename = os.path.splitext(os.path.basename(ifc_path))[0]

        self.settings = ifcopenshell.geom.settings()
        self.settings.set(self.settings.USE_WORLD_COORDS, True)

        self.storey_by_name = {}
        self.storey_elev_by_name = {}

        for st in self.model.by_type("IfcBuildingStorey"):
            name = (st.Name or "Nivel Desconocido").strip()
            self.storey_by_name[name] = st
            elev = getattr(st, "Elevation", None)
            try:
                elev_val = float(elev) if elev is not None else 0.0
            except Exception:
                elev_val = 0.0
            self.storey_elev_by_name[name] = elev_val

        if not self.storey_elev_by_name:
            print("Advertencia: no se encontraron IfcBuildingStorey. Se asume un nivel base en Z=0.0.")
            self.storey_elev_by_name["Nivel 0"] = 0.0

        self._sorted_storeys = sorted(self.storey_elev_by_name.items(), key=lambda kv: kv[1])

        self._geom_cache = {}
        self._bbox2d_by_space = {}
        self._space_centers = {}
        self._doors_by_level = defaultdict(list)
        self._storey_polylines = defaultdict(list)

    # -----------------------------------------------------------------------
    # Geometría
    # -----------------------------------------------------------------------
    def get_element_geometry(self, element):
        gid = getattr(element, "GlobalId", id(element))
        if gid in self._geom_cache:
            return self._geom_cache[gid]
        try:
            shape = ifcopenshell.geom.create_shape(self.settings, element)
            verts = shape.geometry.verts
            xs = verts[0::3]; ys = verts[1::3]; zs = verts[2::3]
            if not xs:
                self._geom_cache[gid] = (None, None)
                return None, None
            centroid = (sum(xs)/len(xs), sum(ys)/len(ys), sum(zs)/len(zs))
            bbox = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
            self._geom_cache[gid] = (centroid, bbox)
            return centroid, bbox
        except Exception:
            self._geom_cache[gid] = (None, None)
            return None, None

    def get_element_vertices(self, element):
        try:
            shape = ifcopenshell.geom.create_shape(self.settings, element)
            verts = shape.geometry.verts
            return list(zip(verts[0::3], verts[1::3], verts[2::3]))
        except Exception:
            return []

    def snap_z_to_level(self, z_value):
        try:
            z = float(z_value)
        except Exception:
            z = 0.0
        closest_name, closest_elev = min(self._sorted_storeys, key=lambda kv: abs(kv[1] - z))
        return closest_name, float(closest_elev)

    def check_proximity(self, point, bbox, tolerance=0.5):
        px, py, pz = point
        min_x, min_y, min_z, max_x, max_y, max_z = bbox
        return (min_x - tolerance <= px <= max_x + tolerance and
                min_y - tolerance <= py <= max_y + tolerance and
                min_z - tolerance <= pz <= max_z + tolerance)

    def distance_2d(self, a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def bbox2d_contains(self, xy, bbox2d, tolerance=0.3):
        x, y = xy
        min_x, min_y, max_x, max_y = bbox2d
        return (min_x - tolerance <= x <= max_x + tolerance and
                min_y - tolerance <= y <= max_y + tolerance)

    def bbox2d_distance(self, xy, bbox2d):
        x, y = xy
        min_x, min_y, max_x, max_y = bbox2d
        dx = max(min_x - x, 0, x - max_x)
        dy = max(min_y - y, 0, y - max_y)
        return math.hypot(dx, dy)

    def element_storey_name(self, element):
        try:
            container = ifc_element.get_container(element)
            if container and container.is_a("IfcBuildingStorey"):
                return (container.Name or "Nivel Desconocido").strip()
        except Exception:
            pass
        centroid, bbox = self.get_element_geometry(element)
        if bbox:
            return self.snap_z_to_level(bbox[2])[0]
        if centroid:
            return self.snap_z_to_level(centroid[2])[0]
        return "Nivel Desconocido"

    # -----------------------------------------------------------------------
    # Vectores y trayectorias ortogonales
    # -----------------------------------------------------------------------
    def normalize_2d(self, vec):
        x, y = vec
        norm = math.hypot(x, y)
        if norm < 1e-9:
            return None
        return (x / norm, y / norm)

    def principal_axis_2d(self, points_xy):
        if len(points_xy) < 2:
            return None
        mx = sum(p[0] for p in points_xy) / len(points_xy)
        my = sum(p[1] for p in points_xy) / len(points_xy)
        sxx = sum((p[0] - mx) ** 2 for p in points_xy)
        syy = sum((p[1] - my) ** 2 for p in points_xy)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in points_xy)
        trace = sxx + syy
        det = sxx * syy - sxy * sxy
        disc = max(trace * trace / 4.0 - det, 0.0)
        eig = trace / 2.0 + math.sqrt(disc)
        vx = sxy
        vy = eig - sxx
        if abs(vx) < 1e-9 and abs(vy) < 1e-9:
            vx, vy = (1.0, 0.0) if sxx >= syy else (0.0, 1.0)
        return self.normalize_2d((vx, vy))

    def get_host_wall_for_door(self, door):
        try:
            for rel_fill in self.model.by_type("IfcRelFillsElement"):
                related = getattr(rel_fill, "RelatedBuildingElement", None)
                if related and getattr(related, "GlobalId", None) == getattr(door, "GlobalId", None):
                    opening = getattr(rel_fill, "RelatingOpeningElement", None)
                    if opening is None:
                        continue
                    for rel_void in self.model.by_type("IfcRelVoidsElement"):
                        related_opening = getattr(rel_void, "RelatedOpeningElement", None)
                        if related_opening and getattr(related_opening, "GlobalId", None) == getattr(opening, "GlobalId", None):
                            wall = getattr(rel_void, "RelatingBuildingElement", None)
                            if wall and wall.is_a() in ("IfcWall", "IfcWallStandardCase", "IfcCurtainWall"):
                                return wall
        except Exception:
            pass
        return None

    def infer_wall_axis_2d(self, wall_or_door):
        verts = self.get_element_vertices(wall_or_door)
        if len(verts) < 2:
            return (1.0, 0.0)
        zs = [v[2] for v in verts]
        min_z = min(zs)
        pts_xy = [(x, y) for x, y, z in verts if z <= min_z + 0.40]
        if len(pts_xy) < 2:
            pts_xy = [(x, y) for x, y, _ in verts]
        axis = self.principal_axis_2d(pts_xy)
        return axis or (1.0, 0.0)

    def infer_door_frame(self, door):
        wall = self.get_host_wall_for_door(door)
        wall_axis = self.infer_wall_axis_2d(wall if wall is not None else door)
        wall_normal = (-wall_axis[1], wall_axis[0])
        return wall_axis, wall_normal

    def orthogonal_path_from_door(self, door_xy, target_xy, wall_axis, wall_normal):
        dx = target_xy[0] - door_xy[0]
        dy = target_xy[1] - door_xy[1]
        dot_n = dx * wall_normal[0] + dy * wall_normal[1]
        nx_ = wall_normal[0] if dot_n >= 0 else -wall_normal[0]
        ny_ = wall_normal[1] if dot_n >= 0 else -wall_normal[1]
        t = dx * nx_ + dy * ny_
        px = door_xy[0] + t * nx_
        py = door_xy[1] + t * ny_
        pts = [door_xy]
        if self.distance_2d(door_xy, (px, py)) > 1e-6:
            pts.append((px, py))
        if self.distance_2d((px, py), target_xy) > 1e-6:
            pts.append(target_xy)
        if len(pts) == 1:
            pts.append(target_xy)
        return pts

    def orthogonal_path_xy(self, src_xy, dst_xy, prefer_axis="x"):
        sx, sy = src_xy; dx, dy = dst_xy
        mid = (dx, sy) if prefer_axis == "x" else (sx, dy)
        pts = [(sx, sy)]
        if self.distance_2d((sx, sy), mid) > 1e-6:
            pts.append(mid)
        if self.distance_2d(mid, (dx, dy)) > 1e-6:
            pts.append((dx, dy))
        if len(pts) == 1:
            pts.append((dx, dy))
        return pts

    def add_edge_with_polyline(self, u, v, coords3d, weight=1.0, accessible=True, edge_type="camino"):
        if len(coords3d) < 2:
            return
        geom = LineString(coords3d)
        levels = "|".join(sorted(set([
            str(self.G.nodes[u].get("level", "Desconocido")),
            str(self.G.nodes[v].get("level", "Desconocido")),
        ])))
        self.G.add_edge(u, v, weight=float(weight), accessible=bool(accessible),
                        edge_type=edge_type, geometry=geom, levels=levels)

    def connect_door_to_space_orthogonal(self, door_id, space_info, weight, accessible):
        door_node = self.G.nodes[door_id]
        space_node = self.G.nodes[space_info["id"]]
        door_xy = (door_node["x"], door_node["y"])
        target_xy = (space_node["x"], space_node["y"])
        wall_axis = (door_node["wall_axis_x"], door_node["wall_axis_y"])
        wall_normal = (door_node["wall_normal_x"], door_node["wall_normal_y"])
        coords2d = self.orthogonal_path_from_door(door_xy, target_xy, wall_axis, wall_normal)
        coords3d = [(x, y, door_node["z"]) for x, y in coords2d]
        self.add_edge_with_polyline(door_id, space_info["id"], coords3d,
                                    weight=weight, accessible=accessible, edge_type="puerta_espacio")

    def connect_vertical_to_space(self, node_id, space_info, weight=1.0, accessible=True):
        a = self.G.nodes[node_id]; b = self.G.nodes[space_info["id"]]
        coords2d = self.orthogonal_path_xy((a["x"], a["y"]), (b["x"], b["y"]), prefer_axis="x")
        coords3d = [(x, y, a["z"]) for x, y in coords2d]
        self.add_edge_with_polyline(node_id, space_info["id"], coords3d,
                                    weight=weight, accessible=accessible, edge_type="vertical_espacio")

    def connect_vertical_to_door(self, node_id, door_id, weight=1.0, accessible=True):
        vertical_node = self.G.nodes[node_id]; door_node = self.G.nodes[door_id]
        door_xy = (door_node["x"], door_node["y"])
        target_xy = (vertical_node["x"], vertical_node["y"])
        wall_axis = (door_node["wall_axis_x"], door_node["wall_axis_y"])
        wall_normal = (door_node["wall_normal_x"], door_node["wall_normal_y"])
        coords2d = list(reversed(
            self.orthogonal_path_from_door(door_xy, target_xy, wall_axis, wall_normal)
        ))
        coords3d = [(x, y, vertical_node["z"]) for x, y in coords2d]
        self.add_edge_with_polyline(node_id, door_id, coords3d,
                                    weight=weight, accessible=accessible, edge_type="vertical_puerta")

    # -----------------------------------------------------------------------
    # Planta base
    # -----------------------------------------------------------------------
    def is_external_wall(self, wall):
        try:
            pset = ifc_element.get_pset(wall, "Pset_WallCommon")
            if isinstance(pset, dict):
                value = pset.get("IsExternal", None)
                if value is not None:
                    return bool(value)
        except Exception:
            pass
        return False

    def extract_low_edges(self, element, max_rel_z=0.25):
        verts = self.get_element_vertices(element)
        if not verts:
            return []
        zs = [v[2] for v in verts]
        min_z = min(zs)
        low_pts = [(x, y) for x, y, z in verts if z <= min_z + max_rel_z]
        if len(low_pts) < 2:
            return []
        unique_pts = []
        seen = set()
        for x, y in low_pts:
            key = (round(x, 3), round(y, 3))
            if key not in seen:
                seen.add(key)
                unique_pts.append((x, y))
        if len(unique_pts) < 2:
            return []
        lines = []
        for i in range(len(unique_pts) - 1):
            if self.distance_2d(unique_pts[i], unique_pts[i + 1]) > 0.20:
                lines.append(LineString([unique_pts[i], unique_pts[i + 1]]))
        if len(unique_pts) > 2 and self.distance_2d(unique_pts[-1], unique_pts[0]) > 0.20:
            lines.append(LineString([unique_pts[-1], unique_pts[0]]))
        return lines

    def collect_storey_outline(self):
        wall_like = {"IfcWall", "IfcWallStandardCase", "IfcCurtainWall"}
        seen = set()

        for rel in self.model.by_type("IfcRelSpaceBoundary"):
            elem = getattr(rel, "RelatedBuildingElement", None)
            if elem is None or elem.is_a() not in wall_like:
                continue
            lvl = self.element_storey_name(elem)
            gid = getattr(elem, "GlobalId", None)
            key = (gid, lvl)
            if key in seen:
                continue
            seen.add(key)
            for line in self.extract_low_edges(elem):
                self._storey_polylines[lvl].append(line)

        for wall in self.model.by_type("IfcWall") + self.model.by_type("IfcWallStandardCase"):
            if not self.is_external_wall(wall):
                continue
            lvl = self.element_storey_name(wall)
            gid = getattr(wall, "GlobalId", None)
            key = (gid, lvl)
            if key in seen:
                continue
            seen.add(key)
            for line in self.extract_low_edges(wall):
                self._storey_polylines[lvl].append(line)

        # Fallback: todos los muros si no hay nada
        if not self._storey_polylines:
            for wall in self.model.by_type("IfcWall") + self.model.by_type("IfcWallStandardCase"):
                lvl = self.element_storey_name(wall)
                gid = getattr(wall, "GlobalId", None)
                key = (gid, lvl)
                if key in seen:
                    continue
                seen.add(key)
                for line in self.extract_low_edges(wall):
                    self._storey_polylines[lvl].append(line)

    # -----------------------------------------------------------------------
    # Ascensores
    # -----------------------------------------------------------------------
    def identify_lift_candidates(self):
        """
        Detecta ascensores en el modelo IFC.

        Estrategia dual:
        a) Elementos que ya abarcan varias plantas (hueco completo): se aceptan
           directamente si cumplen dimensiones y son semánticos o tienen puertas
           cercanas.
        b) Elementos de una sola planta (cabinas individuales): se agrupan por
           proximidad XY. Si el grupo reúne ≥ 2 niveles distintos, se trata como
           un único ascensor multi-planta.

        Palabras clave aceptadas: ELEVATOR, LIFT, ELEVADOR, ASCENSOR, ASCENSORE,
        FAHRSTUHL, AUFZUG (sin distinción de mayúsculas).
        """
        LIFT_KEYWORDS = {
            "ELEVATOR", "LIFT", "ELEVADOR",
            "ASCENSOR", "ASCENSORE", "FAHRSTUHL", "AUFZUG",
        }
        SKIP_TYPES = {"ESCALATOR", "MOVINGWALKWAY", "CRANEWAY"}
        XY_GROUP_TOL = 2.0   # m — distancia máxima para agrupar cabinas del mismo hueco

        good          = []   # resultados finales: (elem, centroid, bbox, sorted_levels)
        single_cabins = []   # cabinas de una sola planta: (elem, cx, cy, centroid, bbox, lvl)
        seen          = set()

        for elem in self.model.by_type("IfcTransportElement"):
            gid = getattr(elem, "GlobalId", None)
            if gid in seen:
                continue
            seen.add(gid)

            ptype = str(getattr(elem, "PredefinedType", "") or "").upper().strip()
            if ptype in SKIP_TYPES:
                continue

            # Detección semántica: tipo IFC o palabras clave en nombre/tipo/desc
            is_semantic = ptype in {"ELEVATOR", "LIFT", "USERDEFINED", "NOTDEFINED"}
            if not is_semantic:
                for attr in ("Name", "ObjectType", "Description"):
                    val = str(getattr(elem, attr, "") or "").upper()
                    if any(kw in val for kw in LIFT_KEYWORDS):
                        is_semantic = True
                        break

            centroid, bbox = self.get_element_geometry(elem)
            if not bbox:
                continue

            dx = bbox[3] - bbox[0]
            dy = bbox[4] - bbox[1]
            dz = bbox[5] - bbox[2]

            touched_levels = [
                lvl for lvl, elev in self._sorted_storeys
                if bbox[2] - 0.50 <= elev <= bbox[5] + 0.50
            ]

            # Umbrales geométricos mínimos
            if is_semantic:
                min_xy, max_xy, min_dz = 0.60, 8.00, 1.00
            else:
                min_xy, max_xy, min_dz = 0.80, 6.00, 2.20

            if not (min_xy <= dx <= max_xy and min_xy <= dy <= max_xy):
                continue
            if dz < min_dz:
                continue
            if not touched_levels:
                continue

            if len(touched_levels) >= 2:
                # Elemento que ya abarca varias plantas
                door_hits = sum(
                    1 for lvl in touched_levels
                    if any(
                        self.distance_2d(
                            (centroid[0], centroid[1]), (d["x"], d["y"])
                        ) <= 5.0
                        for d in self._doors_by_level.get(lvl, [])
                    )
                )
                if is_semantic or door_hits >= 1:
                    good.append((elem, centroid, bbox, touched_levels))
            elif is_semantic:
                # Cabina de una sola planta: guardar para agrupar
                single_cabins.append(
                    (elem, centroid[0], centroid[1], centroid, bbox, touched_levels[0])
                )

        # ── Agrupar cabinas individuales por proximidad XY ──────────────────
        groups = []
        for cabin in single_cabins:
            cx, cy = cabin[1], cabin[2]
            placed = False
            for grp in groups:
                if math.hypot(cx - grp[0][1], cy - grp[0][2]) <= XY_GROUP_TOL:
                    grp.append(cabin)
                    placed = True
                    break
            if not placed:
                groups.append([cabin])

        for grp in groups:
            # Niveles únicos del grupo, ordenados por cota
            lvl_seen, all_lvls = set(), []
            for cabin in grp:
                lvl = cabin[5]
                if lvl and lvl not in lvl_seen:
                    lvl_seen.add(lvl)
                    all_lvls.append(lvl)
            all_lvls = sorted(all_lvls,
                              key=lambda l: self.storey_elev_by_name.get(l, 0))
            if len(all_lvls) < 2:
                continue
            # Centroide XY medio del grupo como posición del nodo
            avg_cx = sum(c[1] for c in grp) / len(grp)
            avg_cy = sum(c[2] for c in grp) / len(grp)
            rep_elem     = grp[0][0]
            rep_centroid = (avg_cx, avg_cy, grp[0][3][2])
            rep_bbox     = grp[0][4]
            good.append((rep_elem, rep_centroid, rep_bbox, all_lvls))

        return good

    # -----------------------------------------------------------------------
    # Extracción principal
    # -----------------------------------------------------------------------
    def extraer_datos(self):
        print("Extrayendo datos del modelo IFC...")
        spaces_data = []

        # 1) Espacios
        for space in self.model.by_type("IfcSpace"):
            centroid, bbox = self.get_element_geometry(space)
            if not centroid:
                continue
            level_name, floor_z = self.snap_z_to_level(bbox[2])
            bbox2d = (bbox[0], bbox[1], bbox[3], bbox[4])
            self.G.add_node(space.GlobalId, name=space.Name or "Estancia", type="Habitacion",
                            level=level_name, x=float(centroid[0]), y=float(centroid[1]),
                            z=float(floor_z), accessible=True)
            info = {"id": space.GlobalId, "bbox": bbox, "bbox2d": bbox2d,
                    "name": space.Name, "level": level_name}
            spaces_data.append(info)
            self._bbox2d_by_space[space.GlobalId] = bbox2d
            self._space_centers[space.GlobalId] = (centroid[0], centroid[1])

        # 2) Losas pequeñas
        for slab in self.model.by_type("IfcSlab"):
            ptype = str(getattr(slab, "PredefinedType", "") or "").upper()
            if ptype in {"ROOF", "BASESLAB"}:
                continue
            centroid, bbox = self.get_element_geometry(slab)
            if not centroid:
                continue
            dx = bbox[3] - bbox[0]; dy = bbox[4] - bbox[1]
            if dx > 10.0 and dy > 10.0:
                continue
            if any(self.check_proximity(centroid, sp["bbox"], tolerance=0.10) for sp in spaces_data):
                continue
            level_name, floor_z = self.snap_z_to_level(bbox[2])
            bbox2d = (bbox[0], bbox[1], bbox[3], bbox[4])
            self.G.add_node(slab.GlobalId, name=slab.Name or "Suelo/Pasillo", type="Suelo",
                            level=level_name, x=float(centroid[0]), y=float(centroid[1]),
                            z=float(floor_z), accessible=True)
            info = {"id": slab.GlobalId, "bbox": bbox, "bbox2d": bbox2d,
                    "name": "Suelo", "level": level_name}
            spaces_data.append(info)
            self._bbox2d_by_space[slab.GlobalId] = bbox2d
            self._space_centers[slab.GlobalId] = (centroid[0], centroid[1])

        # 3) Puertas
        num_doors = 0
        for door in self.model.by_type("IfcDoor"):
            centroid, bbox = self.get_element_geometry(door)
            if not centroid:
                continue
            num_doors += 1
            level_name, floor_z = self.snap_z_to_level(bbox[2])
            width = float(getattr(door, "OverallWidth", 0.0) or 0.0)
            is_accessible = width >= 0.85 if width > 0 else True
            weight = 1.0 if is_accessible else 999999.0
            wall_axis, wall_normal = self.infer_door_frame(door)
            self.G.add_node(door.GlobalId, name=door.Name or "Puerta", type="Puerta",
                            level=level_name, x=float(centroid[0]), y=float(centroid[1]),
                            z=float(floor_z), width=width, accessible=is_accessible,
                            wall_axis_x=float(wall_axis[0]), wall_axis_y=float(wall_axis[1]),
                            wall_normal_x=float(wall_normal[0]), wall_normal_y=float(wall_normal[1]))
            door_info = {"id": door.GlobalId, "x": centroid[0], "y": centroid[1],
                         "z": floor_z, "level": level_name, "bbox": bbox,
                         "wall_axis": wall_axis, "wall_normal": wall_normal}
            self._doors_by_level[level_name].append(door_info)

            connected = []
            for sp in spaces_data:
                if sp["level"] != level_name:
                    continue
                if self.bbox2d_contains((centroid[0], centroid[1]), sp["bbox2d"], tolerance=0.65):
                    connected.append((0.0, sp))
                else:
                    d = self.bbox2d_distance((centroid[0], centroid[1]), sp["bbox2d"])
                    if d <= 1.10:
                        connected.append((d, sp))
            for _, sp in sorted(connected, key=lambda t: t[0])[:2]:
                self.connect_door_to_space_orthogonal(door.GlobalId, sp, weight, is_accessible)

        # 4) Escaleras
        num_stairs = 0
        processed = set()
        stair_flight_info = {}  # GlobalId -> {start_id, end_id, start_z, end_z}

        for stair in self.model.by_type("IfcStairFlight"):
            if stair.GlobalId in processed:
                continue
            verts = self.get_element_vertices(stair)
            if not verts:
                continue
            xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
            min_z, max_z = min(zs), max(zs)

            # Posición XY: centroide de la franja inferior (inicio) y superior (fin).
            # Refleja la posición geométrica real de entrada/salida del tramo.
            tol_z = max((max_z - min_z) * 0.10, 0.15)
            bot = [(x, y) for x, y, z in verts if z <= min_z + tol_z]
            top = [(x, y) for x, y, z in verts if z >= max_z - tol_z]
            cx_s = sum(v[0] for v in bot) / len(bot) if bot else xs[zs.index(min_z)]
            cy_s = sum(v[1] for v in bot) / len(bot) if bot else ys[zs.index(min_z)]
            cx_e = sum(v[0] for v in top) / len(top) if top else xs[zs.index(max_z)]
            cy_e = sum(v[1] for v in top) / len(top) if top else ys[zs.index(max_z)]

            level_s, fz_s = self.snap_z_to_level(min_z)
            level_e, fz_e = self.snap_z_to_level(max_z)
            id_s = f"{stair.GlobalId}_START"
            id_e = f"{stair.GlobalId}_END"

            self.G.add_node(id_s, name="Escalera Inicio", type="Escalera", level=level_s,
                            x=float(cx_s), y=float(cy_s), z=float(fz_s), accessible=False)
            self.G.add_node(id_e, name="Escalera Fin",   type="Escalera", level=level_e,
                            x=float(cx_e), y=float(cy_e), z=float(fz_e), accessible=False)
            self.add_edge_with_polyline(id_s, id_e,
                [(cx_s, cy_s, fz_s), (cx_e, cy_e, fz_e)],
                weight=999999.0, accessible=False, edge_type="escalera")

            stair_flight_info[stair.GlobalId] = {
                'start_id': id_s, 'end_id': id_e,
                'start_z': min_z, 'end_z': max_z,
            }

            for node_id, cx, cy, lvl in [
                (id_s, cx_s, cy_s, level_s),
                (id_e, cx_e, cy_e, level_e),
            ]:
                linked = False
                for sp in spaces_data:
                    if sp["level"] == lvl and self.check_proximity(
                            (cx, cy, self.storey_elev_by_name[lvl]), sp["bbox"], tolerance=0.35):
                        self.connect_vertical_to_space(node_id, sp)
                        linked = True; break
                if not linked:
                    doors = self._doors_by_level.get(lvl, [])
                    if doors:
                        near = min(doors,
                                   key=lambda d: self.distance_2d((cx, cy), (d["x"], d["y"])))
                        if self.distance_2d((cx, cy), (near["x"], near["y"])) <= 3.0:
                            self.connect_vertical_to_door(node_id, near["id"])
            num_stairs += 1
            processed.add(stair.GlobalId)

        # Conectar tramos consecutivos del mismo IfcStair (IfcRelAggregates)
        flights_by_stair = defaultdict(list)
        for rel in self.model.by_type("IfcRelAggregates"):
            relating = getattr(rel, "RelatingObject", None)
            if relating is None or not relating.is_a("IfcStair"):
                continue
            for obj in (getattr(rel, "RelatedObjects", None) or []):
                if obj.is_a("IfcStairFlight") and obj.GlobalId in stair_flight_info:
                    flights_by_stair[relating.GlobalId].append(obj.GlobalId)

        for parent_gid, flight_gids in flights_by_stair.items():
            sorted_flights = sorted(
                flight_gids, key=lambda gid: stair_flight_info[gid]['start_z']
            )
            for i in range(len(sorted_flights) - 1):
                fi = stair_flight_info[sorted_flights[i]]
                fj = stair_flight_info[sorted_flights[i + 1]]
                end_a, start_b = fi['end_id'], fj['start_id']
                if not self.G.has_edge(end_a, start_b):
                    na, nb = self.G.nodes[end_a], self.G.nodes[start_b]
                    dist = math.hypot(na['x'] - nb['x'], na['y'] - nb['y'])
                    self.add_edge_with_polyline(
                        end_a, start_b,
                        [(na['x'], na['y'], na['z']), (nb['x'], nb['y'], nb['z'])],
                        weight=max(dist * 1.2, 0.5),
                        accessible=False, edge_type="escalera_rellano",
                    )

        # Fallback geométrico: solo para tramos NO conectados vía IfcRelAggregates
        # y únicamente entre NIVELES ADYACENTES (índice ±1). Conectar nodos de
        # plantas no consecutivas crea aristas cruzadas que no reflejan la
        # circulación real y sobrecargan el grafo visualmente.
        storeys_idx = {name: i for i, (name, _) in enumerate(self._sorted_storeys)}
        stair_nodes = [
            (nid, d) for nid, d in self.G.nodes(data=True)
            if d.get("type") == "Escalera"
        ]
        for i, (na_id, na) in enumerate(stair_nodes):
            for nb_id, nb in stair_nodes[i + 1:]:
                if self.G.has_edge(na_id, nb_id):
                    continue
                lvl_a = na.get("level", ""); lvl_b = nb.get("level", "")
                if lvl_a == lvl_b:
                    continue
                # Solo niveles consecutivos (adyacentes en la lista de plantas)
                idx_a = storeys_idx.get(lvl_a, -1)
                idx_b = storeys_idx.get(lvl_b, -1)
                if idx_a < 0 or idx_b < 0 or abs(idx_a - idx_b) != 1:
                    continue
                dxy = math.hypot(na['x'] - nb['x'], na['y'] - nb['y'])
                dz  = abs(na['z'] - nb['z'])
                if dxy <= 2.5 and 0.5 <= dz <= 6.0:
                    self.add_edge_with_polyline(
                        na_id, nb_id,
                        [(na['x'], na['y'], na['z']), (nb['x'], nb['y'], nb['z'])],
                        weight=max(dxy * 1.2, 0.5),
                        accessible=False, edge_type="escalera_rellano",
                    )

        # 5) Rampas
        num_ramps = 0
        processed = set()
        for ramp in self.model.by_type("IfcRamp") + self.model.by_type("IfcRampFlight"):
            if ramp.GlobalId in processed:
                continue
            verts = self.get_element_vertices(ramp)
            if not verts:
                continue
            xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
            min_z, max_z = min(zs), max(zs)
            if max_z - min_z < 0.15:
                continue
            p_start = (xs[zs.index(min_z)], ys[zs.index(min_z)], min_z)
            p_end   = (xs[zs.index(max_z)], ys[zs.index(max_z)], max_z)
            level_s, fz_s = self.snap_z_to_level(p_start[2])
            level_e, fz_e = self.snap_z_to_level(p_end[2])
            id_s = f"{ramp.GlobalId}_START"; id_e = f"{ramp.GlobalId}_END"
            self.G.add_node(id_s, name="Rampa Inicio", type="Rampa", level=level_s,
                            x=float(p_start[0]), y=float(p_start[1]), z=float(fz_s), accessible=True)
            self.G.add_node(id_e, name="Rampa Fin",   type="Rampa", level=level_e,
                            x=float(p_end[0]),   y=float(p_end[1]),   z=float(fz_e), accessible=True)
            self.add_edge_with_polyline(id_s, id_e,
                [(p_start[0], p_start[1], fz_s), (p_end[0], p_end[1], fz_e)],
                weight=1.20, accessible=True, edge_type="rampa")
            for node_id, p, lvl in [(id_s, p_start, level_s), (id_e, p_end, level_e)]:
                linked = False
                for sp in spaces_data:
                    if sp["level"] == lvl and self.check_proximity(
                            (p[0], p[1], self.storey_elev_by_name[lvl]), sp["bbox"], tolerance=0.40):
                        self.connect_vertical_to_space(node_id, sp)
                        linked = True; break
                if not linked:
                    doors = self._doors_by_level.get(lvl, [])
                    if doors:
                        near = min(doors, key=lambda d: self.distance_2d((p[0], p[1]), (d["x"], d["y"])))
                        if self.distance_2d((p[0], p[1]), (near["x"], near["y"])) <= 3.0:
                            self.connect_vertical_to_door(node_id, near["id"])
            num_ramps += 1
            processed.add(ramp.GlobalId)

        # 6) Ascensores
        num_lifts = 0
        for lift, centroid, bbox, touched_levels in self.identify_lift_candidates():
            num_lifts += 1
            prev_node_id = None
            for lvl_name in touched_levels:
                lvl_z = self.storey_elev_by_name[lvl_name]
                node_id = f"{lift.GlobalId}_{lvl_name}"
                self.G.add_node(node_id, name="Ascensor", type="Ascensor", level=lvl_name,
                                x=float(centroid[0]), y=float(centroid[1]), z=float(lvl_z), accessible=True)
                if prev_node_id:
                    prev = self.G.nodes[prev_node_id]
                    self.add_edge_with_polyline(prev_node_id, node_id,
                        [(prev["x"], prev["y"], prev["z"]), (centroid[0], centroid[1], lvl_z)],
                        weight=1.0, accessible=True, edge_type="ascensor")
                prev_node_id = node_id
                nearby_doors = sorted(self._doors_by_level.get(lvl_name, []),
                                      key=lambda d: self.distance_2d((centroid[0], centroid[1]), (d["x"], d["y"])))[:2]
                linked = False
                for door_info in nearby_doors:
                    if self.distance_2d((centroid[0], centroid[1]), (door_info["x"], door_info["y"])) <= 3.0:
                        self.connect_vertical_to_door(node_id, door_info["id"])
                        linked = True
                if not linked:
                    cands = sorted([sp for sp in spaces_data if sp["level"] == lvl_name],
                                   key=lambda sp: self.bbox2d_distance((centroid[0], centroid[1]), sp["bbox2d"]))
                    for sp in cands[:2]:
                        if self.bbox2d_distance((centroid[0], centroid[1]), sp["bbox2d"]) <= 2.5:
                            self.connect_vertical_to_space(node_id, sp)
                            linked = True; break

        # 7) Planta base
        self.collect_storey_outline()

        print(f"Espacios/suelos: {len(spaces_data)} | Puertas: {num_doors} | "
              f"Escaleras: {num_stairs} | Rampas: {num_ramps} | Ascensores: {num_lifts}")
        print(f"Grafo: {self.G.number_of_nodes()} nodos, {self.G.number_of_edges()} aristas")

    # -----------------------------------------------------------------------
    # Ruta
    # -----------------------------------------------------------------------
    def calcular_ruta(self, start_node_id, end_node_id, wheelchair=False):
        """
        Devuelve (path, total_weight) o (None, None) si no hay ruta.
        wheelchair=True: solo usa aristas accessible=True (evita escaleras y puertas estrechas).
        wheelchair=False: usa todas las aristas (escaleras penalizadas por peso 999999).
        """
        try:
            if wheelchair:
                edge_list = [(u, v, d) for u, v, d in self.G.edges(data=True)
                             if d.get("accessible", True)]
            else:
                edge_list = list(self.G.edges(data=True))

            H = nx.Graph()
            H.add_nodes_from(self.G.nodes(data=True))
            for u, v, d in edge_list:
                H.add_edge(u, v, **d)

            path = nx.shortest_path(H, source=start_node_id, target=end_node_id, weight="weight")
            weight = nx.shortest_path_length(H, source=start_node_id, target=end_node_id, weight="weight")
            return path, weight
        except Exception:
            return None, None

    # -----------------------------------------------------------------------
    # Exportar GeoPackage (requiere geopandas)
    # -----------------------------------------------------------------------
    def exportar_geopackage(self):
        if not HAS_GEOPANDAS:
            raise ImportError("geopandas no está instalado. Instala con: pip install geopandas fiona pyogrio")
        import geopandas as gpd
        gpkg_name = f"{self.filename}_grafo.gpkg"
        nodes_data = [{"geometry": Point(d["x"], d["y"], d["z"]), "id": n,
                       "tipo": d["type"], "nombre": d.get("name", ""),
                       "level": d.get("level", ""), "acc": d.get("accessible", True)}
                      for n, d in self.G.nodes(data=True)]
        if nodes_data:
            gpd.GeoDataFrame(nodes_data, geometry="geometry").to_file(gpkg_name, layer="nodos", driver="GPKG")
        edges_data = []
        for u, v, attr in self.G.edges(data=True):
            geom = attr.get("geometry") or LineString([
                (self.G.nodes[u]["x"], self.G.nodes[u]["y"], self.G.nodes[u]["z"]),
                (self.G.nodes[v]["x"], self.G.nodes[v]["y"], self.G.nodes[v]["z"]),
            ])
            edges_data.append({"geometry": geom, "acc": attr.get("accessible", True),
                                "levels": attr.get("levels", ""), "edge_type": attr.get("edge_type", "")})
        if edges_data:
            gpd.GeoDataFrame(edges_data, geometry="geometry").to_file(gpkg_name, layer="caminos", driver="GPKG")
        outline_data = []
        for lvl, lines in self._storey_polylines.items():
            z = self.storey_elev_by_name.get(lvl, 0.0)
            for line in lines:
                coords3d = [(x, y, z) for x, y in line.coords]
                outline_data.append({"geometry": LineString(coords3d), "level": lvl, "tipo": "PlantaBase"})
        if outline_data:
            gpd.GeoDataFrame(outline_data, geometry="geometry").to_file(gpkg_name, layer="plantas_base", driver="GPKG")
        print(f"GeoPackage exportado: {gpkg_name}")
        return gpkg_name
