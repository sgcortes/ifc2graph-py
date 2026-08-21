# Esquema de `HSIMG_output.gpkg`

La jerarquía se representa mediante claves estables. Todas las entidades
derivadas conservan GlobalId e identificador interno IFC en los metadatos cuando
existen. `relation_source` distingue evidencia `IFC_semantic`,
`geometry_inferred`, `hybrid` y `unresolved`.

## Capas espaciales

| Capa | Geometría | Propósito |
|---|---|---|
| `spaces` | Polygon/MultiPolygon | Huellas de IfcSpace, clasificación y accesos |
| `doors` | Point Z | Portales, anchura, accesibilidad y espacios asociados |
| `graph_nodes` | Point Z | Nodos semánticos, accesos e internos jerárquicos |
| `graph_edges` | LineString Z | Arcos dirigidos con costes, restricciones y trazabilidad |
| `mobility_axes` | LineString/MultiLineString Z | Ejes mediales transitables y trayectorias verticales |
| `vertical_elements` | LineString/MultiLineString Z | Ascensores, escaleras y rampas |
| `vertical_footprints` (V5) | Polygon/MultiPolygon | Huellas de rampas y clasificación peatonal/vehicular |
| `validation_issues` | Geometría nullable | Localización de errores y advertencias |

En V4, `mobility_axes.extraction_method` diferencia
`vector_obstacle_aware_medial_axis_v4` de
`bounded_component_connector_v4`. Las aristas derivadas de estos últimos usan
`graph_edges.edge_type=component_connector` y
`relation_source=bounded_geometry_repair`.

En V6, el GeoPackage tiene alcance `general_pedestrian_only`. Los ejes
horizontales usan `extraction_method=pedestrian_pruned_medial_axis_v6` y se
reconstruyen exclusivamente a partir de las aristas que permanecen en el
grafo. No se exportan aristas horizontales con `accessible_general=0`.

## Tablas de atributos

| Tabla | Claves/campos principales |
|---|---|
| `subgraphs` | `subgraph_id`, `parent_node_id`, tipo, nivel, método, conteos |
| `user_profiles` | perfil, restricciones y pesos de coste en JSON |
| `model_metadata` | IFC origen, esquema, CRS, unidades, parámetros y versiones |
| `door_access_v5` | Exterior IFC, número de espacios, puerta huérfana y elegibilidad de entrada/salida |
| `door_access_v6` | Equivalente V6 de la auditoría de accesos y puertas |

## Integridad lógica

- `graph_nodes.parent_node_id` referencia un nodo semántico padre.
- `graph_nodes.subgraph_id`, `graph_edges.subgraph_id` y
  `mobility_axes.subgraph_id` enlazan con `subgraphs.subgraph_id`.
- `graph_edges.source_id` y `target_id` enlazan con `graph_nodes.node_id`.
- `spaces.space_id` y `doors.door_id` coinciden con sus nodos semánticos cuando
  estos tienen geometría utilizable.
- En V6, toda fila de `graph_edges` cumple `accessible_general=1`.
- En V6, toda conexión peatonal tiene una arista inversa equivalente.
- `HandicapAccessible` controla `accessible_wheelchair`; el estado de puerta
  solo se infiere desde propiedades explícitas de apertura, cierre o bloqueo.
- Cada subgrafo `vertical_stair` debe formar una sola componente. Los vuelos se
  enlazan mediante conectores de descansillo y solo los terminales inferior y
  superior conservan aristas `vertical_access` hacia espacios.
