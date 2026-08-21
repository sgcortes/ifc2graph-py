# HSIMG V5: anchura transitable, puertas exteriores y rampas

## Objetivo

V5 mantiene la geometria y la jerarquia de V4, pero separa tres conceptos que
antes quedaban mezclados:

1. Un hueco geometrico no implica que exista una ruta peatonal valida.
2. `Pset_DoorCommon.IsExternal` no implica que la puerta este conectada al
   grafo ni que sea una entrada publica.
3. `IfcRamp` puede representar una rampa peatonal, mixta o exclusiva para
   vehiculos.

## Anchura util

Para cada arista `internal_axis` o `component_connector`, V5 toma muestras
sobre la linea y calcula dos veces la distancia minima al limite limpio del
`IfcSpace`. El limite aplica la misma politica de obstaculos que V4: conserva
huecos con area suficiente para representar pilares u otros elementos fijos y
elimina microhuecos de triangulacion.

Los valores se guardan en `graph_edges.metadata_json`:

- `minimum_route_width_m`
- `general_min_route_width_m`
- `wheelchair_min_route_width_m`
- `clearance_method`

Un tramo demasiado estrecho permanece en los datos para auditoria, pero recibe
`accessible_general=false`, coste infinito para el perfil general y
`restriction_reason=insufficient_general_clearance`. Un tramo valido para una
persona a pie pero no para silla de ruedas usa
`insufficient_wheelchair_clearance`.

## Puertas

V5 conserva el valor IFC como `ifc_is_external`, pero solo asigna `inout=1`
cuando la puerta tiene exactamente un espacio conectado y el IFC no la declara
interior. Una puerta con `IsExternal=true` y cero espacios recibe:

- `orphan_external=true`
- `entrance_exit_eligible=false`
- `public_access=false`

La tabla `door_access_v5` permite auditar estos campos sin alterar la capa
espacial `doors`. Graph Explorer oculta las puertas huerfanas en la vista
general y las conserva en el modo detallado de puertas.

## Rampas

V5 busca parametros explicitos `HIMG/HSIMG Pedestrian Access` y
`HIMG/HSIMG Vehicle Access`. Si no existen, aplica vocabulario semantico sobre
el nombre. Terminos como `coche`, `vehicle`, `garage` o `garaje` producen
`route_type=vehicle_only`; una propiedad peatonal explicita prevalece y puede
producir `mixed`.

Las rampas exclusivas para vehiculos se conservan, pero sus aristas quedan
fuera de las rutas peatonales. La nueva capa `vertical_footprints` contiene la
huella, uso, espacios/plantes conectados y metadatos. Graph Explorer utiliza
esta capa para dibujar la rampa en 2D y mantiene su trayectoria roja en 3D.

Cuando el nombre contiene una pendiente porcentual y la geometria produce una
pendiente muy distinta, V5 crea la incidencia `ramp_slope_name_mismatch` para
que el equipo BIM revise niveles, offsets y pendiente real en Revit.

## Limitaciones

- Los umbrales de 0,90 m y 1,20 m son parametros de analisis, no una declaracion
  automatica de cumplimiento normativo.
- Los conectores inmediatos a una puerta se conservan para no separar el portal
  del eje interior; la anchura de la propia puerta se valida por separado.
- La calidad final mejora cuando el IFC contiene `IfcRelSpaceBoundary`, Rooms
  correctos en ambos lados de las puertas y parametros de uso de rampas.
