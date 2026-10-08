# Comprobación de HSIMG V14 sobre el IFC EPM v13

Fecha: 8 de octubre de 2026.

- Entrada exacta: `13_EPM_IFC4_SpaceBoundary (2).ifc`.
- SHA-256 IFC: `454083d1ed8c672081ac9f9be1f8c9ecf3a2a7c41cf3399f1416c262a829c0f5`.
- Producto: `EPM_IFC_v13_HSIMG_v14.gpkg`.
- SHA-256 GeoPackage: `c508507dc898162ea7149566668995645534ca05f0a335cd0d945010079a58a8`.
- IFC4, 8 plantas declaradas, 1.517 espacios y 1.401 puertas.
- Tiempo de la ejecución final: 408,07 s en el equipo local, incluida exportación.

## Comparación con el generador V13 en el mismo archivo IFC

| Medida | Generador V13 | Generador V14 |
|---|---:|---:|
| Nodos | 9.574 | 7.965 |
| Arcos dirigidos | 19.162 | 15.542 |
| Puertas originales | 1.401 | 1.401 |
| Espacios originales | 1.517 | 1.517 |
| Arcos peatonales sin inverso | 0 | 0 |
| Escaleras fragmentadas | 0 | 0 |

El cambio de inserción puerta-eje sustituye 68 arcos originales. Después de
completar esa construcción, la reducción funcional elimina 1.028 arcos no
utilizados y 347 nodos derivados; contrae otros 1.263 nodos de grado dos.
Esos conteos parten del grafo V14 antes de reducirlo (9.575 nodos), no del
grafo V13, porque la corrección de subdivisión también afecta a la construcción.

## Preservación de rutas

- La limpieza V14 contrasta 41.264 rutas finitas entre terminales funcionales,
  para longitud y coste de ambos perfiles. Cambio máximo por redondeo:
  `1.14e-13`. No se pierde alcanzabilidad.
- Una comprobación independiente sobre el GeoPackage exportado compara las
  23 entradas con 7.040 destinos e interfaces estables: 161.920 pares por perfil.
- Perfil general: de los 125.870 pares antes alcanzables, se conservan todos.
- Silla de ruedas: de los 5.941 pares antes alcanzables, se conservan todos.
- No falta ninguno de los 7.040 destinos protegidos; no hay referencias de
  arista a nodos inexistentes ni discrepancias entre longitud almacenada y
  longitud 3D de la polilínea exportada.

La comparación **V13 completo frente a V14 completo** cambia la longitud de
4.751 pares generales (cambio absoluto máximo 3,475 m), por la corrección de
subdivisión y de las conexiones de puerta durante la construcción. Esto es
distinto de la reducción final V14, que conserva las distancias de su grafo de
entrada. Las rutas de silla de ruedas solo cambian por redondeo numérico.

## Límites y anomalías pendientes

Estos resultados son verificaciones internas, no una validación física del
edificio. Permanecen 3 terminales de escalera sin acceso a espacio, una parada
de ascensor sin alcance horizontal, 14 proyecciones de puerta sin alcance al
eje y 8 regiones transitables sin grafo. El CSV contiene el diagnóstico por
elemento. No se calculan precision, recall ni F1 sin una referencia independiente.

Las coordenadas son locales del IFC: no se asigna un CRS proyectado inventado.
El número bruto de componentes incluye padres semánticos y otros nodos que no
son destinos de navegación; no equivale al número de zonas físicas incomunicadas.

## Pruebas de software

77 pruebas Python superadas. Graph Explorer: comprobación TypeScript, compilación
estática, carga de la base de datos con SQL.js y comprobación en navegador de las
vistas 2D/3D y del archivo seleccionado por defecto. El despliegue vuelve a
comprobar la integridad y el SHA-256 del archivo incluido.
