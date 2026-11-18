# HSIMG V15 — flight ends and landing-based stair routing

V15 retains the V14 horizontal, door, elevator and ramp pipeline. It replaces
the stair layer, without modifying the IFC input or lowering wheelchair width
thresholds. Stair edges remain excluded from wheelchair routes.

## Defects addressed

1. Mesh quantiles placed flight terminals inside the flight rather than at its
   ends, sometimes below the landing elevation.
2. A nearest-node connector could select the semantic centre of a stair space.
   Its straight door connector could intersect the stairwell hole and be
   rejected, leaving an otherwise connected stair stack isolated from halls.
3. Sorting all flights by elevation and connecting them in a chain is incorrect
   for stairs with two outgoing branches sharing a half-landing.
4. Distance-only inter-stair links do not establish a walkable landing. Nor does
   reaching another stair prove that a terminal reaches its own floor network.

## Implemented network

- Each supported straight `IfcStairFlight` has a start and end node at mid-width.
  An oriented footprint rectangle defines longitudinal limits; the mesh height
  gradient determines ascending direction. An axis outside the actual footprint
  is rejected, not replaced with a centroid shortcut.
- Each usable decomposed `IfcSlab` landing contributes an interior centre node.
  Flight ends connect to it only at the same aligned elevation and through a
  route supported by the landing surface. Three-flight bifurcations connect to
  the same landing; no arbitrary flight ordering is used.
- Floor landings are derived from the containing `IfcSpace` at the same storey.
  The envelope of the flights and intermediate landing is subtracted: projecting
  the entire space volume onto XY is not proof of a walkable floor over the shaft.
- Within each remaining landing region, clearance-aware visibility paths join
  the flight terminals, real door-side nodes, and the existing horizontal network.
  Short, bounded approach segments are permitted at the flight and door ends;
  the path body uses the configured general pedestrian clearance.
- Consecutive IFC stairs communicate through a shared, surface-supported floor
  landing, not through a distance-only link between their endpoints.
- A doorless stair/hall boundary is handled separately: the spaces must touch
  within a small geometry tolerance, and the route is checked against sections
  of actual walls, columns, glazing and door leaves at walking height (plus
  railing footprints). A nearby hall alone is insufficient. Existing door
  pairs are excluded from this fallback so a closed door is not bypassed.
  If obstacle meshes are unavailable, this fallback is disabled.
  The maximum unrepresented gap between space boundaries is 0.20 m, allowing
  opposite faces of an open wall threshold. This does not authorise crossing
  a wall: the resulting route must still be disjoint from the actual obstacle
  sections, and its interior must pass the pedestrian-clearance check.
- The `IfcStair` parent remains an isolated semantic container. Routes use its
  children. A shared floor landing belongs to its containing space. The existing
  `vertical_elements`, `subgraphs`, `mobility_axes`, and graph layers remain usable.

## Elevation inference and limits

The mesh can contain tread finishes/undersides and stop short of the declared
storey. End elevations may align to a nearby decomposed slab (default maximum
0.25 m, requiring XY contact) or a storey (maximum 0.45 m). Every nonzero change
is recorded with original elevation, aligned elevation, offset, and evidence ID
in `stair_geometry_alignment.json` and the GeoPackage's `stair_audit_v15` table.
These are modelling tolerances, **not building-code tolerances or evidence that
missing physical steps exist**. Large discrepancies require BIM review.

Curved/unsupported flights, missing slabs/spaces, disconnected floor surfaces,
and doors without a usable connection remain diagnostics; V15 does not invent a
wall crossing to force a successful route. Existing inferred door associations
retain their original provenance. The algorithm does not certify as-built safety.

## Validation

`stair_access_audit.json` reports every floor-level flight terminal, including
both upper branches of bifurcating stairs. Success requires a same-storey path
to an actual internal horizontal-mobility node; another stair alone is insufficient.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\run_v15_export.py model.ifc --output-dir deliverables\model_v15
.\.venv\Scripts\python.exe scripts\validate_v15_stairs.py deliverables\model_v15\HSIMG_v15_output.gpkg
```

The independent exported-file validator checks endpoint coordinates, support of
new connectors by their recorded surfaces, wheelchair exclusions, same-floor
reachability, and paths through the two northern corner cores. The named corner
checks are EPM-specific; geometry and floor-reach checks are model-independent.
Export success and SQLite integrity alone do not imply complete building access.
