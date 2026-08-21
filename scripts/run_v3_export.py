"""Generate and validate unified V3 HSIMG products from an IFC model."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v3hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path, help="Input IFC2X3 or IFC4 model")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".qa/v3_full_run"),
        help="Directory for V3 GeoPackage, graph and validation products",
    )
    parser.add_argument(
        "--boundary-spacing",
        type=float,
        default=0.30,
        help="Boundary sampling interval for the vector medial axis in metres",
    )
    parser.add_argument(
        "--branch-pruning",
        type=float,
        default=0.50,
        help="Maximum unprotected leaf length removed from the medial axis",
    )
    parser.add_argument(
        "--raster-axis",
        action="store_true",
        help="Disable the vector method and use the legacy raster skeleton",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = HSIMGConfig(
        vector_boundary_sample_spacing_m=args.boundary_spacing,
        medial_axis_pruning_length_m=args.branch_pruning,
        prefer_vector_medial_axis=not args.raster_axis,
    )
    builder = HSIMGBuilder.from_file(args.ifc.resolve(), config)
    builder.run_all()

    gpkg = builder.export_geopackage(output_dir / "HSIMG_v3_output.gpkg")
    graphml = output_dir / "HSIMG_v3_graph.graphml"
    graph_json = output_dir / "HSIMG_v3_graph.json"
    builder.export_graph(graphml, graph_json)
    validation_csv = output_dir / "HSIMG_v3_validation_report.csv"
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        axis_methods = dict(connection.execute(
            "SELECT extraction_method, COUNT(*) FROM mobility_axes "
            "GROUP BY extraction_method ORDER BY extraction_method"
        ).fetchall())
        node_storeys = dict(connection.execute(
            "SELECT COALESCE(storey_id, '<NULL>'), COUNT(*) "
            "FROM graph_nodes GROUP BY storey_id ORDER BY storey_id"
        ).fetchall())

    payload = json.loads(graph_json.read_text(encoding="utf-8"))
    issue_counts = Counter(issue.issue_type for issue in builder.issues)
    report = {
        "version": 3,
        "source_ifc": str(args.ifc.resolve()),
        "configuration": {
            "axis_method": (
                "vector_boundary_voronoi_medial_axis"
                if config.prefer_vector_medial_axis
                else "raster_medial_axis"
            ),
            "boundary_sample_spacing_m": config.vector_boundary_sample_spacing_m,
            "branch_pruning_m": config.medial_axis_pruning_length_m,
        },
        "outputs": {
            "geopackage": str(gpkg),
            "graph_json": str(graph_json),
            "graphml": str(graphml),
            "validation_csv": str(validation_csv),
        },
        "summary": builder.summary(),
        "validation": {
            "gpkg_integrity": integrity,
            "axis_methods": axis_methods,
            "json_nodes": len(payload.get("nodes", [])),
            "json_edges": len(payload.get("edges", payload.get("links", []))),
            "node_storeys": node_storeys,
            "orphan_nodes_removed": issue_counts["orphan_horizontal_node_removed"],
            "vector_axis_fallbacks": issue_counts["vector_axis_fallback"],
        },
    }
    report_path = output_dir / "run_v3_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
