"""
IFC2GRAPH Web App — Backend FastAPI
Sirve la API REST y los archivos estáticos del frontend.
"""

import os
import sys

# Windows: añadir DLLs de OSGeo4W al path de carga antes de importar shapely/ifcopenshell
if sys.platform == "win32":
    _osgeo4w_bin = r"C:\Users\sgcortes\AppData\Local\Programs\OSGeo4W\bin"
    if os.path.exists(_osgeo4w_bin):
        os.add_dll_directory(_osgeo4w_bin)

import uuid
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bim_mapper import BIMAccessibilityMapper

app = FastAPI(title="IFC2GRAPH")

# Almacenamiento en memoria de sesiones (app local, usuario único)
sessions: dict[str, BIMAccessibilityMapper] = {}


class RouteRequest(BaseModel):
    session_id: str
    origin: str
    destination: str
    wheelchair: bool = False


@app.post("/api/upload")
async def upload_ifc(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".ifc"):
        raise HTTPException(400, "Solo se aceptan archivos .ifc")

    tmp_path = tempfile.mktemp(suffix=".ifc")
    try:
        content = await file.read()
        with open(tmp_path, "wb") as f:
            f.write(content)

        mapper = BIMAccessibilityMapper(tmp_path)
        mapper.extraer_datos()

        storeys = sorted(
            mapper.storey_elev_by_name.keys(),
            key=lambda k: mapper.storey_elev_by_name[k],
        )

        nodes = [
            {
                "id": nid,
                "name": d.get("name", ""),
                "type": d.get("type", ""),
                "level": d.get("level", ""),
                "x": round(float(d.get("x", 0)), 4),
                "y": round(float(d.get("y", 0)), 4),
                "z": round(float(d.get("z", 0)), 4),
                "accessible": bool(d.get("accessible", True)),
                "width": round(float(d["width"]), 3) if d.get("width") else None,
            }
            for nid, d in mapper.G.nodes(data=True)
        ]

        edges = []
        for u, v, d in mapper.G.edges(data=True):
            geom = d.get("geometry")
            if geom is not None:
                coords = [list(c) for c in geom.coords]
            else:
                nu = mapper.G.nodes[u]
                nv = mapper.G.nodes[v]
                coords = [[nu["x"], nu["y"], nu["z"]], [nv["x"], nv["y"], nv["z"]]]
            edges.append({
                "u": u,
                "v": v,
                "type": d.get("edge_type", "camino"),
                "accessible": bool(d.get("accessible", True)),
                "levels": d.get("levels", ""),
                "weight": float(d.get("weight", 1.0)),
                "coords": coords,
            })

        floorplans: dict[str, list] = {}
        for lvl, lines in mapper._storey_polylines.items():
            floorplans[lvl] = [[list(c) for c in line.coords] for line in lines]

        session_id = str(uuid.uuid4())
        sessions[session_id] = mapper

        return {
            "session_id": session_id,
            "filename": file.filename,
            "storeys": storeys,
            "nodes": nodes,
            "edges": edges,
            "floorplans": floorplans,
        }
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.post("/api/route")
async def calculate_route(req: RouteRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "Sesión expirada. Carga el archivo IFC de nuevo.")

    mapper = sessions[req.session_id]

    if req.origin not in mapper.G.nodes:
        raise HTTPException(400, "Nodo origen no encontrado en el grafo.")
    if req.destination not in mapper.G.nodes:
        raise HTTPException(400, "Nodo destino no encontrado en el grafo.")

    path, weight = mapper.calcular_ruta(req.origin, req.destination, wheelchair=req.wheelchair)

    if path is None:
        mode = "accesible (sin escaleras)" if req.wheelchair else "general"
        return {
            "found": False,
            "message": f"No existe ruta {mode} entre los nodos indicados.",
            "path": [],
            "path_nodes": [],
            "total_weight": None,
        }

    path_nodes = [
        {
            "id": nid,
            "name": mapper.G.nodes[nid].get("name", ""),
            "type": mapper.G.nodes[nid].get("type", ""),
            "level": mapper.G.nodes[nid].get("level", ""),
            "x": round(float(mapper.G.nodes[nid].get("x", 0)), 4),
            "y": round(float(mapper.G.nodes[nid].get("y", 0)), 4),
            "z": round(float(mapper.G.nodes[nid].get("z", 0)), 4),
        }
        for nid in path
    ]

    wheelchair_label = " ♿" if req.wheelchair else ""
    return {
        "found": True,
        "path": path,
        "path_nodes": path_nodes,
        "total_weight": round(float(weight), 2),
        "message": f"Ruta{wheelchair_label} encontrada · {len(path)} nodos · coste {weight:.1f}",
    }


# Servir frontend estático
static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
