"""Run HSIMG V15 with an auditable flight/landing stair network."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from v15hsimg import HSIMGBuilder, HSIMGConfig


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ifc',type=Path)
    parser.add_argument('--output-dir',type=Path,default=Path('deliverables/HSIMG_v15'))
    parser.add_argument('--name',default='HSIMG_v15_output')
    parser.add_argument('--general-min-width',type=float,default=.90)
    parser.add_argument('--wheelchair-min-width',type=float,default=1.20)
    parser.add_argument('--general-min-door-width',type=float,default=.60)
    parser.add_argument('--stair-floor-z-tolerance',type=float,default=.45)
    parser.add_argument('--keep-all-horizontal-alternatives',action='store_true')
    args=parser.parse_args()
    if Path(args.name).name!=args.name or args.name in {'.','..'}:parser.error('Invalid filename stem')
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/f'{args.name}.gpkg').exists():parser.error('Output already exists; choose a new directory or filename')
    source=args.ifc.resolve();started=time.perf_counter()
    config=HSIMGConfig(general_min_route_width_m=args.general_min_width,
                       wheelchair_min_route_width_m=args.wheelchair_min_width,
                       general_min_door_width_m=args.general_min_door_width,
                       stair_floor_z_tolerance_m=args.stair_floor_z_tolerance,
                       simplify_functional_horizontal_routes=not args.keep_all_horizontal_alternatives)
    b=HSIMGBuilder.from_file(source,config)
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    b.model_metadata.update(source_ifc_sha256=sha,generator_version='HSIMG V15')
    b.run_all()
    reciprocal=b._nonreciprocal_pedestrian_edges()
    if reciprocal:raise RuntimeError(f'Non-reciprocal pedestrian edges: {reciprocal}')
    gpkg=b.export_geopackage(out/f'{args.name}.gpkg')
    b.export_graph(out/f'{args.name}.graphml',out/f'{args.name}.json')
    b.validation_dataframe().to_csv(out/'validation_issues.csv',index=False)
    audit=b.stair_access_validation()
    def save(name,value):
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    save('stair_access_audit.json',audit)
    save('stair_geometry_alignment.json',b.stair_geometry_audit)
    with sqlite3.connect(gpkg) as db:
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
        dangling=db.execute('SELECT COUNT(*) FROM graph_edges e LEFT JOIN graph_nodes s ON e.source_id=s.node_id '
                            'LEFT JOIN graph_nodes t ON e.target_id=t.node_id WHERE s.node_id IS NULL OR t.node_id IS NULL').fetchone()[0]
    if integrity!='ok' or dangling:raise RuntimeError(f'Invalid export: {integrity}, {dangling}')
    report=dict(generator_version=15,source_ifc=str(source),source_ifc_sha256=sha,config=asdict(config),
                summary=b.summary(),integrity_check=integrity,dangling_edges=dangling,nonreciprocal_edges=len(reciprocal),
                fragmented_stairs=b._fragmented_stair_subgraphs(),elapsed_seconds=time.perf_counter()-started,
                geopackage=str(gpkg),geopackage_sha256=hashlib.sha256(gpkg.read_bytes()).hexdigest())
    save('run_report.json',report)
    sources=sorted(ROOT.glob('v*hsimg.py')) + [ROOT/'hsimg.py',ROOT/'v3vector.py',ROOT/'v4vector.py',ROOT/'horizontal_cleanup.py',ROOT/'stair_surfaces.py',Path(__file__).resolve()]
    save('source_code_sha256.json',{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
