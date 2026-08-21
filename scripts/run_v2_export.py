"""Generate and validate V2 HSIMG products from an IFC file."""

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

from v2hsimg import HSIMGBuilder, HSIMGConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path, help="Input IFC model")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".qa/v2_full_run"),
        help="Directory for GeoPackage, graph and validation products",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    builder = HSIMGBuilder.from_file(args.ifc.resolve(), HSIMGConfig())
    builder.run_all()

    gpkg = builder.export_geopackage(output_dir / "HSIMG_output.gpkg")
    graphml = output_dir / "HSIMG_graph.graphml"
    graph_json = output_dir / "HSIMG_graph.json"
    builder.export_graph(graphml, graph_json)
    validation_csv = output_dir / "HSIMG_validation_report.csv"
    builder.validation_dataframe().to_csv(validation_csv, index=False)

    with sqlite3.connect(gpkg) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        gpkg_contents = connection.execute(
            "SELECT table_name, data_type FROM gpkg_contents ORDER BY table_name"
        ).fetchall()
        space_storeys = dict(
            connection.execute(
                "SELECT COALESCE(storey_id, '<NULL>'), COUNT(*) "
                "FROM spaces GROUP BY storey_id ORDER BY storey_id"
            ).fetchall()
        )
        node_storeys = dict(
            connection.execute(
                "SELECT COALESCE(storey_id, '<NULL>'), COUNT(*) "
                "FROM graph_nodes GROUP BY storey_id ORDER BY storey_id"
            ).fetchall()
        )

    payload = json.loads(graph_json.read_text(encoding="utf-8"))
    report = {
        "source_ifc": str(args.ifc.resolve()),
        "outputs": {
            "geopackage": str(gpkg),
            "graph_json": str(graph_json),
            "graphml": str(graphml),
            "validation_csv": str(validation_csv),
        },
        "summary": builder.summary(),
        "storey_assignment": {
            "spaces": space_storeys,
            "graph_nodes": node_storeys,
            "unassigned_spaces": sum(
                space.storey_id is None for space in builder.spaces.values()
            ),
            "space_counts_in_memory": dict(
                Counter(space.storey_id or "<NULL>" for space in builder.spaces.values())
            ),
        },
        "validation": {
            "gpkg_integrity": integrity,
            "gpkg_contents": gpkg_contents,
            "json_nodes": len(payload.get("nodes", [])),
            "json_edges": len(payload.get("edges", payload.get("links", []))),
        },
    }
    report_path = output_dir / "run_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
