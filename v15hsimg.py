"""HSIMG V15: flight-end and surface-supported landing stair networks.

Stair parents are semantic containers, never shortcuts through a stairwell.
Floor access is routed to IFC-related door sides inside the remaining landing
area. No distance-only connection between consecutive stair objects is used.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import networkx as nx
import numpy as np
import ifcopenshell.geom
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box
from shapely.ops import nearest_points, unary_union

from hsimg import LOGGER, VerticalMobilityRecord, entity_name, flatten_psets, length_3d, stable_id
import v14hsimg as v14
from stair_surfaces import horizontal_section


def flight_end_axis(mesh: Any) -> tuple[LineString, float]:
    """Mid-width ends of a straight flight, not quantile-averaged mesh points.

    The oriented rectangle supplies exact longitudinal bounds. The mesh's
    height gradient chooses its rising axis. Curved/non-supported axes fail
    closed rather than introducing a diagonal across a void.
    """
    vertices = mesh.vertices
    rectangle = np.asarray(mesh.footprint.minimum_rotated_rectangle.exterior.coords)[:4, :2]
    axes = [rectangle[1]-rectangle[0], rectangle[2]-rectangle[1]]
    axes = [a/np.linalg.norm(a) for a in axes]
    xy = vertices[:, :2] - vertices[:, :2].mean(axis=0)
    gradient = np.linalg.lstsq(xy, vertices[:, 2]-vertices[:, 2].mean(), rcond=None)[0]
    direction = max(axes, key=lambda a: abs(float(a @ gradient)))
    if direction @ gradient < 0:
        direction = -direction
    across = np.array([-direction[1], direction[0]])
    along = vertices[:, :2] @ direction
    cross = vertices[:, :2] @ across
    middle = (cross.min()+cross.max())/2
    triangles = vertices[mesh.faces]
    flat = triangles[np.ptp(triangles[:, :, 2], axis=1) < 1e-6]
    if not len(flat):
        raise ValueError('flight has no horizontal tread faces')
    low, high = float(flat[:, :, 2].min()), float(flat[:, :, 2].max())
    if high-low < .10:
        raise ValueError('flight has no usable rise')
    start = direction*along.min()+across*middle
    end = direction*along.max()+across*middle
    line = LineString([(*start, low), (*end, high)])
    if not mesh.footprint.buffer(.005).covers(LineString([start, end])):
        raise ValueError('straight flight axis leaves its actual footprint')
    return line, float(cross.max()-cross.min())


@dataclass(slots=True)
class HSIMGConfig(v14.HSIMGConfig):
    stair_floor_z_tolerance_m: float = .45
    stair_landing_z_tolerance_m: float = .25
    stair_surface_xy_tolerance_m: float = .06
    stair_access_max_length_m: float = 15.0
    stair_open_boundary_gap_m: float = .20

    def __post_init__(self):
        super().__post_init__()
        for name in ('stair_floor_z_tolerance_m', 'stair_landing_z_tolerance_m',
                     'stair_surface_xy_tolerance_m', 'stair_access_max_length_m', 'stair_open_boundary_gap_m'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive and finite')


class HSIMGBuilder(v14.HSIMGBuilder):
    def __init__(self, ifc_model: Any, config: HSIMGConfig | Mapping[str, Any] | None = None):
        resolved = HSIMGConfig(**dict(config)) if isinstance(config, Mapping) else config or HSIMGConfig()
        super().__init__(ifc_model, resolved)
        self.stair_parts: dict[str, dict] = {}
        self.stair_floor_landings: dict[tuple, str] = {}
        self.stair_access_audit: list[dict] = []
        self.stair_geometry_audit: list[dict] = []
        self._stair_floor_domains: dict[tuple, Any] = {}
        self._stair_floor_terminals: dict[str, list[str]] = {}
        self._stair_wall_sections = None
        self._stair_wall_sections_valid = False

    def extract_stairs(self):
        LOGGER.info('V15 extracting supported stair flights and landing slabs')
        # Do not invoke V6's sorted-flight chain: bifurcating stairs are not chains.
        for parent in self.model.by_type('IfcStair'):
            sid = stable_id('stair', parent.GlobalId)
            children = [obj for rel in (parent.IsDecomposedBy or ()) for obj in rel.RelatedObjects]
            flights, slabs = [], []
            for obj in children:
                if not (obj.is_a('IfcStairFlight') or obj.is_a('IfcSlab')):
                    continue
                try:
                    mesh = self.geometry.mesh(obj)
                    if obj.is_a('IfcStairFlight'):
                        line, width = flight_end_axis(mesh)
                        flights.append(dict(guid=obj.GlobalId, line=line, footprint=mesh.footprint,
                                            width=width, raw_coordinates=list(line.coords)))
                    else:
                        slabs.append(dict(guid=obj.GlobalId, footprint=mesh.footprint, z=mesh.z_max))
                except Exception as exc:
                    self._issue('error', 'stair_part_geometry_unavailable_v15',
                                f'{entity_name(obj)}: {exc}', 'Repair or review the flight/landing geometry',
                                related_ifc_guid=obj.GlobalId, related_node_id=sid)
            if not flights:
                self._issue('error', 'stair_without_supported_flights_v15', entity_name(parent),
                            'Model explicit IfcStairFlight components; no centroid fallback is used',
                            related_ifc_guid=parent.GlobalId)
            for flight in flights:
                corrected = []
                for coordinate in flight['line'].coords:
                    x, y, z = coordinate
                    options = [(abs(z-s['z']), s['z'], 'IfcSlab', s['guid']) for s in slabs
                               if s['footprint'].distance(Point(x,y)) <= self.config.stair_surface_xy_tolerance_m
                               and abs(z-s['z']) <= self.config.stair_landing_z_tolerance_m]
                    floor = self._nearest_storey_id(z)
                    if floor and abs(z-self.storeys[floor].elevation) <= self.config.stair_floor_z_tolerance_m:
                        options.append((abs(z-self.storeys[floor].elevation), self.storeys[floor].elevation,
                                        'IfcBuildingStorey', floor))
                    match = min(options, default=None)
                    corrected.append((x,y,match[1] if match else z))
                    if match and match[0] > 1e-5:
                        self.stair_geometry_audit.append(dict(stair_id=sid, flight_guid=flight['guid'],
                            raw_z=z, aligned_z=match[1], offset_m=match[1]-z,
                            evidence_type=match[2], evidence_id=match[3]))
                flight['line'] = LineString(corrected)
            lines = [f['line'] for f in flights]
            elevations = [c[2] for line in lines for c in line.coords]
            storeys = sorted({self._nearest_storey_id(z) for z in elevations
                              if abs(z-self.storeys[self._nearest_storey_id(z)].elevation) < 1e-5},
                             key=lambda k:self.storeys[k].elevation)
            record = VerticalMobilityRecord(sid,parent.GlobalId,parent.id(),parent.is_a(),entity_name(parent),
                'stair', MultiLineString(lines) if lines else None, storeys, False,
                properties=flatten_psets(parent), extraction_method='flight_end_centres_and_supported_landings_v15')
            record.properties.update({'HSIMG.FlightCount':len(flights), 'HSIMG.LandingCount':len(slabs)})
            self.vertical_elements[sid] = record
            self.stair_parts[sid] = dict(flights=flights, slabs=slabs,
                envelope=unary_union([f['footprint'] for f in flights]+[s['footprint'] for s in slabs]).convex_hull,
                z_min=min(elevations,default=0), z_max=max(elevations,default=0))
        return self.vertical_elements

    def _stair_route(self, domain, start, end, z, max_throat=.65):
        """Clearance-aware route within one evidenced surface, with short end throats."""
        if domain.is_empty:
            return None
        tolerance = self.config.stair_surface_xy_tolerance_m
        original = domain.buffer(tolerance)
        start, end = Point(start.x,start.y), Point(end.x,end.y)
        if not original.covers(start) or not original.covers(end):
            return None
        candidates = []
        for part in self._polygon_parts(domain.buffer(-self.config.general_min_route_width_m/2)):
            a, _ = nearest_points(part,start); b, _ = nearest_points(part,end)
            if max(a.distance(start), b.distance(end)) > max_throat:
                continue
            first, last = LineString([start,a]), LineString([b,end])
            if not original.covers(first) or not original.covers(last):
                continue
            middle = self._visibility_path(a,b,part)
            if middle is None:
                continue
            coords = []
            for segment in (first,middle,last):
                for x,y,*_ in segment.coords:
                    c=(float(x),float(y),float(z))
                    if not coords or math.dist(coords[-1],c)>1e-8:
                        coords.append(c)
            if len(coords)<2:
                continue
            path=LineString(coords)
            if path.length<=self.config.stair_access_max_length_m and original.covers(path):
                candidates.append(path)
        return min(candidates,key=lambda p:p.length,default=None)

    def _stair_node(self, node_id, point, vertical, role, storey=None, metadata=None, parent=None):
        self._add_node(node_id,point,node_type='internal_mobility',node_role=role,
            mobility_type='stair',parent_node_id=parent or vertical.vertical_id,
            subgraph_id=stable_id('subgraph',vertical.vertical_id,'vertical'),hierarchy_level=2,
            ifc_guid=vertical.ifc_guid,ifc_class='IfcStair',name=f'{vertical.name} / {role}',
            storey_id=storey,accessible_wheelchair=False,metadata=metadata)

    def _stair_edge(self, source, target, line, vertical, kind, metadata):
        self._add_bidirectional_edge(source,target,edge_type=kind,mobility_mode='stair' if kind=='vertical_path' else 'walk',
            subgraph_id=stable_id('subgraph',vertical.vertical_id,'vertical'),geometry=line,
            accessible_wheelchair=False,restriction_reason='stairs_not_wheelchair_accessible',
            relation_source='IFC_flight_landing_surface_v15',confidence=.90,
            validation_status='surface_supported_v15', metadata=metadata,
            estimated_time=length_3d(line)/(self.config.stair_speed_m_s if kind=='vertical_path' else self.config.walking_speed_m_s))

    def _floor_domain(self, space):
        key=(space.space_id,space.storey_id)
        if key not in self._stair_floor_domains:
            # The projected IfcSpace volume can include the entire stair shaft.
            # Exclude the flight/half-landing envelope, leaving the floor landing.
            envelopes=[p['envelope'] for p in self.stair_parts.values()
                       if p['z_min']-1e-5<=space.elevation<=p['z_max']+1e-5
                       and p['envelope'].intersects(space.footprint)]
            self._stair_floor_domains[key]=space.footprint.difference(unary_union(envelopes))
        return self._stair_floor_domains[key]

    @staticmethod
    def _stair_space(space):
        return any(k in f'{space.name} {space.long_name}'.casefold() for k in ('escalera','stair'))

    def _attach_stair_floor(self, terminal, vertical, storey):
        point=self.graph.nodes[terminal]['geometry']; tol=self.config.stair_surface_xy_tolerance_m
        spaces=[s for s in self.spaces.values() if s.storey_id==storey and s.footprint is not None
                and s.footprint.buffer(tol).covers(point)]
        spaces.sort(key=lambda s:(not self._stair_space(s),s.space_id))
        attached=[]
        for space in spaces:
            domain=self._floor_domain(space)
            for index,part in enumerate(self._polygon_parts(domain)):
                if not part.buffer(tol).covers(point):
                    continue
                safe=part.buffer(-self.config.general_min_route_width_m/2)
                if safe.is_empty:
                    continue
                local_safe=safe if self._stair_space(space) else safe.intersection(point.buffer(2.5))
                if local_safe.is_empty:continue
                centre=local_safe.centroid if local_safe.covers(local_safe.centroid) else local_safe.representative_point()
                centre=Point(centre.x,centre.y,space.elevation)
                path=self._stair_route(part,point,centre,space.elevation)
                if path is None:
                    continue
                key=(space.space_id,index)
                landing=self.stair_floor_landings.get(key)
                if landing is None:
                    landing=stable_id('stair_floor_landing_v15',space.ifc_guid,index)
                    self.stair_floor_landings[key]=landing
                    self._stair_node(landing,centre,vertical,'floor_landing',storey,
                                     dict(space_id=space.space_id,landing_domain_wkt=part.wkt),parent=space.space_id)
                    # Stair-space centroids do not represent a safe route. Remove
                    # their star connections; keep the semantic node for identity.
                    if self._stair_space(space):
                        remove=[(u,v,k) for u,v,k,d in self.graph.edges(keys=True,data=True)
                                if space.space_id in (u,v) and d.get('edge_type')=='space_access']
                        for e in remove:self.graph.remove_edge(*e)
                    targets=self._space_door_sides(space)
                    if space.node_class=='horizontal_mobility':
                        targets += [n for n,d in self.graph.nodes(data=True)
                                    if d.get('parent_node_id')==space.space_id and d.get('mobility_type')=='horizontal'
                                    and self.graph.degree(n)>0]
                    elif not self._stair_space(space) and space.space_id in self.graph:
                        targets.append(space.space_id)
                    targets=sorted(set(targets),key=lambda n:centre.distance(self.graph.nodes[n]['geometry']))
                    for target in targets:
                        access=self._stair_route(part,centre,self.graph.nodes[target]['geometry'],space.elevation)
                        if access is None:continue
                        self._stair_edge(landing,target,access,vertical,'stair_floor_access',
                            dict(space_id=space.space_id,surface_source='IfcSpace_minus_stair_envelopes',
                                 route_domain_wkt=part.wkt,target_id=target))
                        if self.graph.nodes[target].get('mobility_type')=='horizontal':break
                self._stair_edge(terminal,landing,path,vertical,'vertical_access',
                    dict(space_id=space.space_id,surface_source='IfcSpace_minus_stair_envelopes',route_domain_wkt=part.wkt))
                attached.append(dict(space_id=space.space_id,landing_id=landing))
            if attached:break
        self.stair_access_audit.append(dict(stair_id=vertical.vertical_id,stair_name=vertical.name,
            terminal_id=terminal,storey_id=storey,attachments=attached))

    def _load_stair_wall_sections(self):
        if self._stair_wall_sections is not None:return
        region=unary_union([p['envelope'] for p in self.stair_parts.values()]).buffer(5)
        elements={e.id():e for cls in ('IfcWall','IfcCurtainWall','IfcColumn','IfcWindow','IfcDoor','IfcRailing') for e in self.model.by_type(cls)}
        LOGGER.info('V15 checking %d physical obstacle meshes for open stair access',len(elements))
        sections={sid:[] for sid in self.storeys};seen=set()
        iterator=ifcopenshell.geom.iterator(self.geometry.settings,self.model,4,include=list(elements.values()))
        if iterator.initialize():
            while True:
                shape=iterator.get();seen.add(shape.id)
                vertices=np.asarray(shape.geometry.verts).reshape(-1,3)
                faces=np.asarray(shape.geometry.faces).reshape(-1,3)
                if len(vertices):
                    low=vertices.min(axis=0);high=vertices.max(axis=0)
                    if region.intersects(box(*low[:2],*high[:2])):
                        for sid,floor in self.storeys.items():
                            if elements[shape.id].is_a('IfcRailing') and low[2]<floor.elevation+1.8 and high[2]>floor.elevation+.05:
                                footprint=self.geometry._project_triangles(vertices,faces)
                                if footprint is not None:sections[sid].append(footprint)
                            for offset in (.10,1.0,1.8):
                                z=floor.elevation+offset+.000003
                                if low[2]<z<high[2]:
                                    section=horizontal_section(vertices,faces,z)
                                    if not section.is_empty:sections[sid].append(section)
                if not iterator.next():break
        missing=[e.GlobalId for eid,e in elements.items() if eid not in seen and e.Representation is not None]
        self._stair_wall_sections_valid=not missing
        self._stair_wall_sections={sid:unary_union(parts) for sid,parts in sections.items()}
        LOGGER.info('V15 obstacle sections complete: %d meshes; %d unavailable',len(seen),len(missing))
        self.model_metadata['stair_obstacle_meshes_v15']=len(seen)
        self.model_metadata['stair_obstacle_meshes_missing_v15']=json.dumps(missing)
        if missing:
            self._issue('warning','stair_open_access_obstacles_incomplete_v15',f'{len(missing)} obstacle meshes unavailable',
                        'Open-boundary inference is disabled; repair the obstacle geometry')

    def _recover_open_stair_floor_access(self):
        """Doorless stair/hall boundaries: require surface contact AND no solid barrier."""
        missing=[r for r in self.stair_access_validation() if not r['reaches_horizontal_network']]
        if not missing:return
        self._load_stair_wall_sections()
        if not self._stair_wall_sections_valid:return
        pairs={frozenset(d.connected_space_ids) for d in self.doors.values() if len(d.connected_space_ids)==2}
        for row in missing:
            terminal=row['terminal_id'];point=self.graph.nodes[terminal]['geometry'];sid=row['storey_id']
            vertical=self.vertical_elements[row['stair_id']]
            stair_spaces=[s for s in self.spaces.values() if s.storey_id==sid and self._stair_space(s)
                          and s.footprint is not None and s.footprint.buffer(.06).covers(point)]
            connected=False
            for space in stair_spaces:
                floor=self._floor_domain(space)
                if not floor.buffer(.06).covers(point):continue
                halls=[h for h in self.spaces.values() if h.storey_id==sid and h.node_class=='horizontal_mobility'
                       and h.footprint is not None and h.footprint.distance(space.footprint)<self.config.stair_open_boundary_gap_m
                       and frozenset((space.space_id,h.space_id)) not in pairs]
                halls.sort(key=lambda h:h.footprint.distance(point))
                for hall in halls:
                    # Only join contacting space surfaces. No widening across a wall:
                    # solids (including glazing and door leaves) are then subtracted.
                    half_gap=self.config.stair_open_boundary_gap_m/2
                    domain=floor.buffer(half_gap).union(hall.footprint.buffer(half_gap)).buffer(-half_gap)
                    domain=domain.difference(self._stair_wall_sections[sid])
                    targets=[n for n,d in self.graph.nodes(data=True) if d.get('parent_node_id')==hall.space_id
                             and d.get('mobility_type')=='horizontal' and self.graph.degree(n)>0
                             and point.distance(d['geometry'])<self.config.stair_access_max_length_m]
                    targets.sort(key=lambda n:point.distance(self.graph.nodes[n]['geometry']))
                    for target in targets[:20]:
                        route=self._stair_route(domain,point,self.graph.nodes[target]['geometry'],point.z)
                        if route is None:continue
                        if route.intersects(self._stair_wall_sections[sid].buffer(.002)):
                            continue  # XY tolerance must never bridge a thin physical barrier.
                        # Expose a floor landing on the approach rather than using
                        # the remote semantic centre of either IFC space.
                        along=min(.6,route.length/2);xy=route.interpolate(along)
                        centre=Point(xy.x,xy.y,point.z)
                        landing=stable_id('open_stair_floor_v15',terminal,hall.space_id)
                        self._stair_node(landing,centre,vertical,'floor_landing',sid,
                            dict(space_id=space.space_id,hall_space_id=hall.space_id))
                        from shapely.ops import substring
                        for u,v,segment in ((terminal,landing,substring(route,0,along)),
                                            (landing,target,substring(route,along,route.length))):
                            geometry=LineString([(c[0],c[1],point.z) for c in segment.coords])
                            self._stair_edge(u,v,geometry,vertical,'stair_open_floor_access',
                                dict(space_id=space.space_id,hall_space_id=hall.space_id,route_domain_wkt=domain.wkt,
                                     surface_source='touching_IfcSpaces_minus_IFC_wall_column_glazing_door_sections',
                                     space_boundary_gap_m=space.footprint.distance(hall.footprint),
                                     obstacle_section_heights_m=[.10,1.0,1.8]))
                        for audit in self.stair_access_audit:
                            if audit['terminal_id']==terminal:
                                audit['attachments'].append(dict(space_id=space.space_id,hall_space_id=hall.space_id,landing_id=landing))
                        connected=True;break
                    if connected:break
                if connected:break

    def build_vertical_subgraphs(self):
        all_vertical=self.vertical_elements
        self.vertical_elements={k:v for k,v in all_vertical.items() if v.vertical_type!='stair'}
        try:super().build_vertical_subgraphs()
        finally:self.vertical_elements=all_vertical
        self._build_stair_subgraphs()
        self._recover_open_stair_floor_access()
        self._refresh_subgraph_counts()
        return self.subgraphs

    def _build_stair_subgraphs(self):
        """Build the stair-only layer after the existing elevator/ramp pipeline."""
        stair_spaces={s.space_id for s in self.spaces.values() if self._stair_space(s)}
        for u,v,k,d in list(self.graph.edges(keys=True,data=True)):
            if d.get('edge_type')!='space_access':continue
            metadata=json.loads(d.get('metadata_json','{}'))
            if u in stair_spaces or v in stair_spaces or metadata.get('space_id') in stair_spaces:
                self.graph.remove_edge(u,v,k)
        for sid,parts in self.stair_parts.items():
            vertical=self.vertical_elements[sid]
            if vertical.path is None:continue
            subgraph=stable_id('subgraph',sid,'vertical')
            coords=[c for f in parts['flights'] for c in f['line'].coords]
            self._add_node(sid,Point(np.mean(coords,axis=0)),node_type='vertical_mobility',node_role='semantic_parent',
                mobility_type='stair',parent_node_id=None,subgraph_id=subgraph,hierarchy_level=0,
                ifc_guid=vertical.ifc_guid,ifc_class='IfcStair',name=vertical.name,storey_id=None,
                accessible_wheelchair=False,metadata=dict(routing_parent=False,connected_storeys=vertical.connected_storeys))
            endpoints=[]; floor_terminals=[]
            for flight in parts['flights']:
                ids=[]
                for index,coord in enumerate(flight['line'].coords):
                    nid=stable_id('stair_flight_end_v15',flight['guid'],index)
                    point=Point(coord); storey=self._nearest_storey_id(point.z)
                    if abs(point.z-self.storeys[storey].elevation)>1e-5:storey=None
                    self._stair_node(nid,point,vertical,'flight_start' if index==0 else 'flight_end',storey,
                        dict(flight_guid=flight['guid'],width_m=flight['width'],raw_coordinate=flight['raw_coordinates'][index]))
                    endpoints.append(nid);ids.append(nid)
                    if storey:floor_terminals.append(nid)
                self._stair_edge(*ids,flight['line'],vertical,'vertical_path',
                    dict(flight_guid=flight['guid'],flight_width_m=flight['width'],path_kind='flight'))
            for slab in parts['slabs']:
                safe=slab['footprint'].buffer(-self.config.general_min_route_width_m/2)
                if safe.is_empty:continue
                centre=safe.centroid if safe.covers(safe.centroid) else safe.representative_point()
                centre=Point(centre.x,centre.y,slab['z'])
                nid=stable_id('stair_landing_v15',sid,slab['guid'])
                self._stair_node(nid,centre,vertical,'intermediate_landing',metadata=dict(landing_guid=slab['guid']))
                for endpoint in endpoints:
                    p=self.graph.nodes[endpoint]['geometry']
                    if abs(p.z-slab['z'])>1e-5:continue
                    route=self._stair_route(slab['footprint'],p,centre,slab['z'])
                    if route is not None:
                        self._stair_edge(endpoint,nid,route,vertical,'vertical_path',
                            dict(landing_guid=slab['guid'],path_kind='landing',route_domain_wkt=slab['footprint'].wkt))
            self._stair_floor_terminals[sid]=floor_terminals
            for terminal in floor_terminals:
                self._attach_stair_floor(terminal,vertical,self.graph.nodes[terminal]['storey_id'])
            self.subgraphs.append(dict(subgraph_id=subgraph,parent_node_id=sid,subgraph_type='vertical_stair',
                hierarchy_level=1,extraction_method=vertical.extraction_method,parameters_json=json.dumps({'v15':True}),node_count=0,edge_count=0))
            actual=[d['geometry'] for u,v,d in self.graph.edges(data=True)
                    if u<v and d.get('subgraph_id')==subgraph and d.get('edge_type')=='vertical_path']
            if actual:vertical.path=MultiLineString(actual)
            self.mobility_axes.append(dict(axis_id=stable_id('axis',sid),parent_node_id=sid,subgraph_id=subgraph,
                mobility_type='stair',extraction_method=vertical.extraction_method,pruning_threshold=None,
                metadata_json='{}',geometry=vertical.path))
    def _fragmented_stair_subgraphs(self):
        result=[]
        for sid in self.stair_parts:
            nodes=[n for n,d in self.graph.nodes(data=True) if d.get('parent_node_id')==sid]
            local=self.graph.subgraph(nodes).to_undirected()
            if len(local) and not nx.is_connected(local):result.append(sid)
        return result

    def stair_access_validation(self):
        """A stair-to-stair transition alone is NOT evidence of access to a floor."""
        floor_graphs={}
        for u,v,d in self.graph.edges(data=True):
            if d.get('accessible_general') is False or d.get('edge_type')=='vertical_path':continue
            sid=self.graph.nodes[u].get('storey_id')
            if sid is not None and sid==self.graph.nodes[v].get('storey_id'):
                floor_graphs.setdefault(sid,nx.Graph()).add_edge(u,v)
        results=[]
        for row in self.stair_access_audit:
            sid=row['storey_id']; terminal=row['terminal_id']
            local=floor_graphs.get(sid,nx.Graph())
            reachable=nx.node_connected_component(local,terminal) if terminal in local else set()
            horizontal={n for n in reachable if self.graph.nodes[n].get('mobility_type')=='horizontal'
                        and self.graph.nodes[n].get('node_type')=='internal_mobility'}
            results.append(dict(row,reaches_horizontal_network=bool(horizontal),reachable_floor_nodes=len(reachable)))
        return results

    def _stairs_missing_terminal_access(self):
        return [(r['stair_id'],r['terminal_id']) for r in self.stair_access_validation()
                if not r['reaches_horizontal_network']]

    def summary(self):
        result=super().summary()
        result.update({'HSIMG version':15,'Version':15,'Stair flight-end nodes':sum(2*len(p['flights']) for p in self.stair_parts.values()),
                       'Stair floor landings':sum(d.get('node_role')=='floor_landing' for _,d in self.graph.nodes(data=True)),
                       'Stair terminals reaching floor network':sum(r['reaches_horizontal_network'] for r in self.stair_access_validation())})
        return result

    def export_geopackage(self, output_path: str | Path) -> Path:
        output = super().export_geopackage(output_path)
        with sqlite3.connect(output) as db:
            db.execute('CREATE TABLE IF NOT EXISTS stair_audit_v15 '
                       '(fid INTEGER PRIMARY KEY, record_type TEXT, data_json TEXT)')
            db.execute('DELETE FROM stair_audit_v15')
            db.executemany('INSERT INTO stair_audit_v15(record_type,data_json) VALUES (?,?)',
                [('floor_access',json.dumps(r)) for r in self.stair_access_validation()]
                + [('geometry_alignment',json.dumps(r)) for r in self.stair_geometry_audit])
            db.execute("INSERT OR REPLACE INTO gpkg_contents(table_name,data_type,identifier,description,last_change) VALUES "
                       "('stair_audit_v15','attributes','stair_audit_v15','V15 stair floor access and alignment audit',"
                       "strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        return output


__all__=['HSIMGBuilder','HSIMGConfig','flight_end_axis']
