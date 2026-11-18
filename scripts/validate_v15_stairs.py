"""Independent exported-GeoPackage checks for V15 stair geometry and routes."""
import argparse
import json
from pathlib import Path
import sqlite3

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely import wkt


def validate(path):
    nodes=gpd.read_file(path,layer='graph_nodes').set_index('node_id')
    edges=gpd.read_file(path,layer='graph_edges')
    graph=nx.Graph();graph.add_nodes_from(nodes.index)
    graph.add_edges_from((r.source_id,r.target_id) for r in edges.itertuples() if r.accessible_general)
    components={n:len(c) for c in nx.connected_components(graph) for n in c}
    errors=[];checked=0;domain_checks=0
    for e in edges.itertuples():
        if e.relation_source!='IFC_flight_landing_surface_v15':continue
        checked+=1
        if e.accessible_wheelchair:errors.append(f'wheelchair stair {e.edge_id}')
        for nid,coord in ((e.source_id,e.geometry.coords[0]),(e.target_id,e.geometry.coords[-1])):
            if nid not in nodes.index:errors.append(f'dangling {nid}');continue
            import math
            if math.dist(nodes.loc[nid].geometry.coords[0],coord)>1e-5:errors.append(f'endpoint mismatch {e.edge_id}')
        md=json.loads(e.metadata_json)
        if 'route_domain_wkt' in md:
            domain_checks+=1
            if not wkt.loads(md['route_domain_wkt']).buffer(.060001).covers(e.geometry):
                errors.append(f'edge leaves evidenced surface {e.edge_id}')
    floorgraphs={}
    for e in edges.itertuples():
        if not e.accessible_general or e.edge_type=='vertical_path':continue
        s,t=nodes.loc[e.source_id],nodes.loc[e.target_id]
        if s.storey_id and s.storey_id==t.storey_id:
            floorgraphs.setdefault(s.storey_id,nx.Graph()).add_edge(e.source_id,e.target_id)
    terminals=[]
    for nid,n in nodes.iterrows():
        if n.node_role not in ('flight_start','flight_end') or pd.isna(n.storey_id) or not n.storey_id:continue
        local=floorgraphs.get(n.storey_id,nx.Graph())
        reach=nx.node_connected_component(local,nid) if nid in local else set()
        horizontal=[i for i in reach if nodes.loc[i].mobility_type=='horizontal' and nodes.loc[i].node_type=='internal_mobility']
        terminals.append(dict(node_id=nid,stair_id=n.parent_node_id,storey_id=n.storey_id,x=n.geometry.x,y=n.geometry.y,z=n.geometry.z,
                              reaches_floor_horizontal=bool(horizontal),component_size=components[nid]))
    # Both northern corner cores: prove paths between distinct floors without
    # leaving their own stair footprint or using elevators/another stair core.
    routes=[]
    for side,lo,hi in [('north_west',14,24),('north_east',54,64)]:
        core=set(n for n,d in nodes.iterrows() if d.mobility_type=='stair' and lo<d.geometry.x<hi and d.geometry.y>175)
        local=graph.subgraph(core)
        starts=[r['node_id'] for r in terminals if r['node_id'] in core and abs(r['z'])<.01]
        for target_z in (-4.0,4.7,9.4,14.1,18.8):
            ends=[r['node_id'] for r in terminals if r['node_id'] in core and abs(r['z']-target_z)<.01]
            found=next((nx.shortest_path(local,s,t) for s in starts for t in ends if nx.has_path(local,s,t)),None)
            routes.append(dict(core=side,from_z=0,to_z=target_z,connected=found is not None,path=found))
    with sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro',uri=True) as c:
        integrity=c.execute('pragma integrity_check').fetchone()[0]
    return dict(integrity=integrity,new_directed_stair_edges_checked=checked,surface_coverage_checks=domain_checks,
                structural_errors=errors,terminals=terminals,north_core_routes=routes,
                floor_terminals=len(terminals),floor_terminals_connected=sum(t['reaches_floor_horizontal'] for t in terminals),
                components=nx.number_connected_components(graph),largest_component=max(components.values(),default=0))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('gpkg',type=Path);args=parser.parse_args()
    result=validate(args.gpkg)
    dest=args.gpkg.parent/'independent_stair_validation.json'
    dest.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('terminals','north_core_routes')},indent=2))
    print('Northern core routes:',[(r['core'],r['to_z'],r['connected']) for r in result['north_core_routes']])
    if result['structural_errors'] or result['integrity']!='ok':raise SystemExit(1)
