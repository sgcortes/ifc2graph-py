import unittest
from types import SimpleNamespace
import networkx as nx
import numpy as np
from shapely.geometry import Point, Polygon, box, LineString, MultiLineString
from hsimg import VerticalMobilityRecord, stable_id
from stair_surfaces import horizontal_section
from v15hsimg import HSIMGBuilder, HSIMGConfig, flight_end_axis


class StairLandingTests(unittest.TestCase):
    def builder(self):
        b=object.__new__(HSIMGBuilder)
        b.config=HSIMGConfig()
        b.graph=nx.MultiDiGraph()
        b._visibility_graph_cache={}
        return b

    def test_mid_width_flight_ends_not_mesh_vertex_averages(self):
        v=np.array([[0,0,0],[0,2,0],[4,0,3],[4,2,3],[0.2,0,0],[3.8,0,3]],dtype=float)
        m=SimpleNamespace(vertices=v,faces=np.array([[0,1,4],[2,3,5]]),footprint=box(0,0,4,2))
        axis,width=flight_end_axis(m)
        self.assertAlmostEqual(width,2)
        self.assertEqual(list(axis.coords),[(0,1,0),(4,1,3)])

    def test_route_avoids_stairwell_hole(self):
        b=self.builder();domain=Polygon([(0,0),(8,0),(8,6),(0,6)],holes=[[(3,1),(5,1),(5,5),(3,5)]])
        route=b._stair_route(domain,Point(1,3),Point(7,3),0)
        self.assertIsNotNone(route)
        self.assertGreater(route.length,6)
        self.assertTrue(domain.buffer(.01).covers(route))

    def test_no_link_across_disconnected_floor_surfaces(self):
        b=self.builder();domain=box(0,0,3,3).union(box(4,0,7,3))
        self.assertIsNone(b._stair_route(domain,Point(1,1),Point(5,1),0))

    def test_reject_long_throat_across_void(self):
        b=self.builder()
        self.assertIsNone(b._stair_route(box(0,0,3,3),Point(-1,1),Point(2,1),0))

    def test_stair_only_component_is_not_floor_access(self):
        b=self.builder();b.stair_access_audit=[dict(stair_id='s',terminal_id='a',storey_id='L')]
        b.graph.add_node('a',storey_id='L',mobility_type='stair')
        b.graph.add_node('b',storey_id='L',mobility_type='stair')
        b.graph.add_edge('a','b',edge_type='stair_landing_transition',accessible_general=True)
        self.assertFalse(b.stair_access_validation()[0]['reaches_horizontal_network'])
        b.graph.add_node('h',storey_id='L',mobility_type='horizontal',node_type='internal_mobility')
        b.graph.add_edge('b','h',edge_type='stair_floor_access',accessible_general=True)
        self.assertTrue(b.stair_access_validation()[0]['reaches_horizontal_network'])

    def test_other_floor_does_not_count_as_landing_access(self):
        b=self.builder();b.stair_access_audit=[dict(stair_id='s',terminal_id='a',storey_id='L')]
        b.graph.add_node('a',storey_id='L',mobility_type='stair')
        b.graph.add_node('h',storey_id='UP',mobility_type='horizontal',node_type='internal_mobility')
        b.graph.add_edge('a','h',edge_type='vertical_path',accessible_general=True)
        self.assertFalse(b.stair_access_validation()[0]['reaches_horizontal_network'])

    def test_invalid_tolerance(self):
        with self.assertRaises(ValueError):HSIMGConfig(stair_floor_z_tolerance_m=-1)

    def test_bifurcating_stair_uses_shared_landing_not_sorted_chain(self):
        b=self.builder()
        b.storeys={'L0':SimpleNamespace(elevation=0),'L1':SimpleNamespace(elevation=4)}
        b.spaces={}; b.subgraphs=[]; b.mobility_axes=[]
        b.stair_floor_landings={};b._stair_floor_domains={};b._stair_floor_terminals={};b.stair_access_audit=[]
        lines=[LineString([(0,2,0),(3,2,2)]),LineString([(3,0,2),(0,0,4)]),LineString([(3,4,2),(0,4,4)])]
        v=VerticalMobilityRecord('s','guid',1,'IfcStair','Bifurcating','stair',MultiLineString(lines),['L0','L1'],False)
        b.vertical_elements={'s':v}
        b.stair_parts={'s':dict(flights=[dict(guid=str(i),line=l,width=1.5,raw_coordinates=list(l.coords)) for i,l in enumerate(lines)],
                              slabs=[dict(guid='landing',footprint=box(3,-.8,5,4.8),z=2)])}
        b._build_stair_subgraphs()
        self.assertEqual(b._fragmented_stair_subgraphs(),[])
        landings=[n for n,d in b.graph.nodes(data=True) if d.get('node_role')=='intermediate_landing']
        self.assertEqual(len(landings),1)
        self.assertEqual(len(set(b.graph.successors(landings[0]))),3)
        self.assertEqual(b.graph.degree('s'),0)
        self.assertEqual(len(b._stair_floor_terminals['s']),3)
        for u,v,d in b.graph.edges(data=True):
            self.assertFalse(d['accessible_wheelchair'])
            self.assertTrue(b.graph.has_edge(v,u))

    def test_floor_door_connects_without_semantic_centre(self):
        b=self.builder()
        space=SimpleNamespace(space_id='room',ifc_guid='room-guid',name='1',long_name='Escalera',
            storey_id='L',elevation=0,footprint=box(0,0,6,3),node_class='finalist',connected_door_ids=['door'])
        b.spaces={'room':space};b.doors={'door':SimpleNamespace(ifc_guid='door-guid')}
        b._stair_floor_domains={};b.stair_floor_landings={};b.stair_access_audit=[]
        b.stair_parts={'s':dict(envelope=box(3,0,6,3),z_min=0,z_max=4)}
        vertical=SimpleNamespace(vertical_id='s',ifc_guid='stair-guid',name='Stair')
        side=stable_id('door_side','door-guid','room-guid')
        b.graph.add_node(side,geometry=Point(.05,1.5,0),storey_id='L')
        b.graph.add_node('room',geometry=Point(5,1.5,0),storey_id='L')
        b.graph.add_edge('room',side,edge_type='space_access')
        b.graph.add_node('end',geometry=Point(3,1.5,0),storey_id='L')
        b._attach_stair_floor('end',vertical,'L')
        self.assertTrue(nx.has_path(b.graph,'end',side))
        self.assertEqual(b.graph.degree('room'),0)
        for _,_,d in b.graph.edges(data=True):
            self.assertTrue(box(0,0,3,3).buffer(.001).covers(d['geometry']))

    def test_wall_section_blocks_wall_but_not_above_lintel_projection(self):
        vertices=np.array([[0,0,2],[3,0,2],[3,.2,2],[0,.2,2],
                           [0,0,3],[3,0,3],[3,.2,3],[0,.2,3]],dtype=float)
        faces=np.array([[0,1,4],[1,5,4],[1,2,5],[2,6,5],[2,3,6],[3,7,6],[3,0,7],[0,4,7],
                        [0,2,1],[0,3,2],[4,5,6],[4,6,7]])
        self.assertTrue(horizontal_section(vertices,faces,1.0).is_empty)
        wall=horizontal_section(vertices,faces,2.5)
        self.assertAlmostEqual(wall.area,.6)

    def test_shared_hall_landing_keeps_one_geometric_position(self):
        b=self.builder()
        space=SimpleNamespace(space_id='hall',ifc_guid='hall-guid',name='Hall',long_name='Hall',
            storey_id='L',elevation=0,footprint=box(0,0,12,4),node_class='horizontal_mobility',connected_door_ids=[])
        b.spaces={'hall':space};b.doors={};b.stair_parts={}
        b._stair_floor_domains={};b.stair_floor_landings={};b.stair_access_audit=[]
        vertical=SimpleNamespace(vertical_id='s',ifc_guid='stair-guid',name='Stair')
        for node,x in [('first',2),('second',8)]:
            b.graph.add_node(node,geometry=Point(x,2,0),storey_id='L')
            b._attach_stair_floor(node,vertical,'L')
        self.assertEqual(len(b.stair_floor_landings),1)
        self.assertTrue(nx.has_path(b.graph,'first','second'))
        for u,v,d in b.graph.edges(data=True):
            self.assertEqual(d['geometry'].coords[0],b.graph.nodes[u]['geometry'].coords[0])
            self.assertEqual(d['geometry'].coords[-1],b.graph.nodes[v]['geometry'].coords[0])

    def test_solid_wall_prevents_open_stair_hall_route(self):
        b=self.builder();floor=box(0,0,6,3)
        wall=box(2.9,0,3.1,3)
        self.assertIsNone(b._stair_route(floor.difference(wall),Point(1,1.5),Point(5,1.5),0))
        opening_wall=wall.difference(box(2.8,.9,3.2,2.1))
        self.assertIsNotNone(b._stair_route(floor.difference(opening_wall),Point(1,1.5),Point(5,1.5),0))


if __name__=='__main__':unittest.main()
