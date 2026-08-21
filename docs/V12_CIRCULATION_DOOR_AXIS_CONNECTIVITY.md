# IFC2GRAPH V12: continuidad entre puertas y ejes de circulación

V12 corrige un caso en el que una puerta IFC relacionaba correctamente dos
pasillos, pero la proyección de uno de sus lados quedaba separada del eje medio
del pasillo. El portal existía, aunque no podía utilizarse para alcanzar la red
horizontal.

## Método

1. Se mantiene la conexión explícita `IfcDoor` entre los dos espacios.
2. Si la proyección de la puerta no alcanza un eje, se busca el punto más
   próximo sobre un segmento de eje perteneciente al mismo `IfcSpace`.
3. Se crea un nodo de unión sobre ese segmento.
4. El enlace puerta-unión debe permanecer dentro de la huella transitable
   limpia del espacio y superar las comprobaciones de anchura y obstáculos.
5. Las conexiones se crean siempre en ambos sentidos.

No se conectan espacios únicamente por proximidad. Un muro continúa bloqueando
el enlace salvo que exista una puerta, un hueco o una transición abierta ya
validada por las reglas heredadas.

## Anchuras

V12 separa la anchura de una puerta de la anchura libre exigida al pasillo. La
puerta se evalúa con `general_min_door_width_m`; el recorrido posterior conserva
`general_min_route_width_m` y `wheelchair_min_route_width_m`.

## Validación

La exportación informa de las reparaciones puerta-eje, proyecciones de puerta
que continúan aisladas y espacios de circulación que no alcanzan ninguna red
vertical. El ejecutor acepta además dos FID para exigir que una ruta concreta
exista antes de considerar válida la salida.
