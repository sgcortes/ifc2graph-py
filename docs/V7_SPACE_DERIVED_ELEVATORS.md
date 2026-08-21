# IFC2GRAPH V7: ascensores derivados de espacios IFC

V7 resuelve el caso en el que Revit exporta el hueco o la cabina como
`IfcSpace`, pero no existe un `IfcTransportElement` vertical fiable. No exige
modificar el modelo original: utiliza los espacios cuyo nombre, `LongName`,
tipo o propiedades contienen de forma explícita `Elevador`, `Elevator`,
`Ascensor`, `Lift` o `Montacargas`.

## Algoritmo

1. Selecciona únicamente espacios con semántica explícita de ascensor.
2. Agrupa espacios de plantas diferentes mediante solape de huellas y
   proximidad de centroides XY.
3. Consolida duplicados alineados de una misma planta y registra una incidencia.
4. Rechaza grupos que no alcancen dos plantas o un metro de desnivel.
5. Crea un sistema vertical sintético estable a partir de los GUID de origen.
6. Conserva una parada interna por planta y crea solamente tramos entre plantas
   consecutivas.
7. Para cada parada examina exclusivamente los `IfcRelSpaceBoundary` de sus
   espacios fuente y los `IfcWall` relacionados.
8. Acepta primero un `IfcOpeningElement` del muro frontera rellenado por un
   `IfcDoor`. Si no existe relleno, acepta el hueco solo cuando su perfil tiene
   semántica explícita de puerta de ascensor, por ejemplo
   `0900 x 2032mm MIO Puesta Ascensor`.
9. Comprueba que el centro del hueco cae sobre el segmento de contorno del
   espacio de ascensor (tolerancia predeterminada de 0,25 m). Así se evita usar
   otro hueco situado más adelante en el mismo muro.
10. Busca al otro lado únicamente otro `IfcSpace` de la misma planta que use el
    mismo muro como frontera y cuyo contorno alcance el hueco. Si no existe,
    deja la parada desconectada y genera un diagnóstico.
11. Para un hueco sin `IfcDoor`, crea un portal sintético auditable cuyo GUID de
    origen es el del `IfcOpeningElement`. Conserva muro, perfil, ancho, alto,
    espacio de ascensor y espacio de pasillo.
12. Obtiene ancho y alto del perfil bidimensional barrido, no de la profundidad
    de extrusión del vacío. Un perfil de 0,90 m supera el umbral de silla de
    ruedas de 0,80 m.
13. Deduplica huecos coincidentes producidos por muros duplicados, pero conserva
    varios huecos físicamente distintos de una misma parada.
14. Exporta la cadena de evidencia completa en nodos, aristas e informe JSON.

Los `IfcTransportElement` clasificados como ascensor se conservan únicamente si
abarcan al menos dos plantas y un desnivel real. Un elemento de una sola planta
o con una trayectoria vertical despreciable se rechaza con una incidencia V7.

## Ejecución

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\run_v7_export.py" `
  "C:\ruta\modelo.ifc" --output-dir ".\.qa\v7_full_run"
```

El informe `run_v7_report.json` contiene los sistemas, paradas, conexiones a
puerta, ambigüedades, paradas no conectadas y paradas cuya puerta no tiene
anchura suficiente para el perfil de silla de ruedas. El GeoPackage debe superar
`PRAGMA integrity_check` y conservar la reciprocidad de las aristas peatonales.

## Alcance de la inferencia

V7 no busca ni selecciona la puerta más próxima. La cadena aceptada es:

`IfcSpace -> IfcRelSpaceBoundary -> IfcWall -> IfcRelVoidsElement ->
IfcOpeningElement -> IfcRelFillsElement (opcional) -> IfcDoor (opcional)`.

Un hueco semántico sin relleno usa
`relation_source=IfcSpaceBoundary_wall_semantic_IfcOpeningElement_v7`. Una
parada sin hueco válido o sin espacio de desembarco delimitado permanece
desconectada. Esta decisión es conservadora: evita inventar rutas, aunque puede
dejar sin conexión plantas cuyo modelado IFC esté incompleto.

## Validación del modelo 10

La ejecución sobre `10_EPM_IFC4_SpaceBoundary.ifc` produjo 13 sistemas, 62
paradas y 45 portales sintéticos basados en huecos de 0,90 x 2,20 m. Quedaron
17 paradas desconectadas: 9 sin hueco válido en sus muros frontera y 8 con hueco
pero sin acceso demostrable a un espacio de desembarco. El GeoPackage pasó
`PRAGMA integrity_check`, no contiene relaciones `nearest` y conserva una ruta
accesible Nivel 0 -> Nivel 2 mediante ascensor y sin escaleras.
