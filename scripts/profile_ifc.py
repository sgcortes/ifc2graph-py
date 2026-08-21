"""Read-only profiling utility for the supplied IFC research model."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import sys

import ifcopenshell
import ifcopenshell.util.element


def safe_name(entity):
    return getattr(entity, "Name", None) or getattr(entity, "LongName", None) or ""


def main(path: str) -> None:
    model = ifcopenshell.open(path)
    classes = [
        "IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace",
        "IfcDoor", "IfcOpeningElement", "IfcWall", "IfcWallStandardCase",
        "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight",
        "IfcTransportElement", "IfcSlab", "IfcRelSpaceBoundary",
        "IfcRelContainedInSpatialStructure", "IfcRelReferencedInSpatialStructure",
        "IfcRelVoidsElement", "IfcRelFillsElement", "IfcRelAggregates",
        "IfcRelConnectsElements",
    ]
    counts = {}
    for cls in classes:
        try:
            counts[cls] = len(model.by_type(cls))
        except RuntimeError:
            counts[cls] = "not-in-schema"

    storeys = []
    for storey in model.by_type("IfcBuildingStorey"):
        storeys.append({
            "id": storey.id(), "guid": storey.GlobalId, "name": safe_name(storey),
            "elevation": getattr(storey, "Elevation", None),
            "contained": sum(len(r.RelatedElements) for r in getattr(storey, "ContainsElements", []) or []),
        })

    space_names = Counter((safe_name(s) or "<unnamed>").strip().lower() for s in model.by_type("IfcSpace"))
    door_types = Counter(str(getattr(d, "OperationType", None)) for d in model.by_type("IfcDoor"))
    transports = [
        {"id": e.id(), "guid": e.GlobalId, "name": safe_name(e), "type": str(getattr(e, "OperationType", None))}
        for e in model.by_type("IfcTransportElement")
    ]
    units = []
    project = model.by_type("IfcProject")[0]
    for u in getattr(project.UnitsInContext, "Units", []) or []:
        units.append(str(u))

    # IFC2X3 does not define IfcMapConversion. Keep the check schema-safe.
    georef = {}
    for cls in ("IfcMapConversion", "IfcProjectedCRS"):
        try:
            georef[cls] = [str(x) for x in model.by_type(cls)]
        except RuntimeError:
            georef[cls] = []

    output = {
        "schema": model.schema,
        "counts": counts,
        "storeys": storeys,
        "space_name_top": space_names.most_common(30),
        "door_operation_types": door_types,
        "transport_elements": transports,
        "units": units,
        "georeferencing": georef,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path("04_EPM_full.ifc")))
