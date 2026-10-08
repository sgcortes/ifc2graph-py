"""Generate a V14 routing graph and its reproducible cleanup audit from any IFC."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v14hsimg import HSIMGBuilder, HSIMGConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".qa/v14_full_run"))
    parser.add_argument("--name", default="HSIMG_v14_output", help="Output filename stem")
    parser.add_argument("--general-min-width", type=float, default=0.90)
    parser.add_argument("--wheelchair-min-width", type=float, default=1.20)
    parser.add_argument("--general-min-door-width", type=float, default=0.60)
    parser.add_argument("--keep-all-horizontal-alternatives", action="store_true")
    args = parser.parse_args()
    if Path(args.name).name != args.name or args.name in {".", ".."}:
        parser.error("--name must be a filename stem, without directories")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = args.ifc.resolve()
    with source.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    started = time.perf_counter()
    config = HSIMGConfig(
        general_min_route_width_m=args.general_min_width,
        wheelchair_min_route_width_m=args.wheelchair_min_width,
        general_min_door_width_m=args.general_min_door_width,
        simplify_functional_horizontal_routes=not args.keep_all_horizontal_alternatives,
    )
    builder = HSIMGBuilder.from_file(source, config)
    builder.model_metadata.update(source_ifc_sha256=source_hash, generator_version="HSIMG V14")
    builder.run_all()
    nonreciprocal = builder._nonreciprocal_pedestrian_edges()
    if nonreciprocal:
        raise RuntimeError(f"Nonreciprocal pedestrian edges: {nonreciprocal}")
    if builder._fragmented_stair_subgraphs():
        raise RuntimeError("Fragmented stair subgraphs remain")
    gpkg = builder.export_geopackage(output / f"{args.name}.gpkg")
    builder.export_graph(output / f"{args.name}.graphml", output / f"{args.name}.json")
    builder.validation_dataframe().to_csv(output / "validation_issues.csv", index=False)
    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        dangling = connection.execute("SELECT COUNT(*) FROM graph_edges e LEFT JOIN graph_nodes s "
                                      "ON e.source_id=s.node_id LEFT JOIN graph_nodes t ON e.target_id=t.node_id "
                                      "WHERE s.node_id IS NULL OR t.node_id IS NULL").fetchone()[0]
    if integrity != "ok" or dangling:
        raise RuntimeError(f"GeoPackage integrity={integrity}; dangling edges={dangling}")
    with gpkg.open("rb") as stream:
        output_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"generator_version": 14, "source_ifc": source.name, "source_ifc_sha256": source_hash,
              "geopackage": gpkg.name, "geopackage_sha256": output_hash,
              "elapsed_seconds": time.perf_counter() - started,
              "summary": builder.summary(), "integrity_check": integrity,
              "dangling_edges": dangling, "nonreciprocal_edges": len(nonreciprocal)}
    if builder.horizontal_cleanup is not None:
        report["horizontal_subgraphs"] = builder.horizontal_cleanup.spaces
        (output / "horizontal_cleanup_actions.json").write_text(
            json.dumps(builder.horizontal_cleanup.audit, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (output / "run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "horizontal_subgraphs"},
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    main()
