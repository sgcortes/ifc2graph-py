"""End-to-end validation against the supplied IFC model."""

from pathlib import Path
import json
import sqlite3
import sys

import networkx as nx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hsimg import HSIMGBuilder, HSIMGConfig


def main() -> None:
    out = Path(".qa/full_run")
    out.mkdir(parents=True, exist_ok=True)
    builder = HSIMGBuilder.from_file("04_EPM_full.ifc", HSIMGConfig())
    builder.run_all()
    routes = {}
    for profile in ("general", "wheelchair"):
        eligible = nx.MultiDiGraph()
        eligible.add_nodes_from(builder.graph.nodes(data=True))
        for u, v, key, data in builder.graph.edges(keys=True, data=True):
            if profile == "general" or data.get("accessible_wheelchair", False):
                eligible.add_edge(u, v, key=key, **data)
        components = sorted(nx.weakly_connected_components(eligible), key=len, reverse=True)
        if components:
            destinations = [n for n in components[0] if builder.graph.nodes[n].get("node_type") == "space"]
            if len(destinations) >= 2:
                try:
                    routes[profile] = builder.compute_route(destinations[0], destinations[-1], profile)
                except nx.NetworkXNoPath:
                    routes[profile] = None
    gpkg = builder.export_geopackage(out / "HSIMG_output.gpkg")
    builder.validation_dataframe().to_csv(out / "HSIMG_validation_report.csv", index=False)
    builder.export_graph(out / "HSIMG_graph.graphml", out / "HSIMG_graph.json")
    with sqlite3.connect(gpkg) as connection:
        layers = connection.execute("SELECT table_name, data_type FROM gpkg_contents ORDER BY table_name").fetchall()
    report = {"summary": builder.summary(), "routes": routes, "gpkg_contents": layers}
    (out / "run_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
