# HSIMG V13: conectividad segura de portones en espacios finalistas

## Problema corregido

En un garaje o almacén grande, varias puertas pueden estar asociadas
explícitamente al mismo `IfcSpace`. El generador anterior intentaba unir cada
nodo interior de puerta con un único punto representativo del espacio. Si la
línea recta abandonaba brevemente el polígono, se rechazaba correctamente, pero
la puerta quedaba reducida a un componente de dos nodos: portal y lado interior.

En el IFC 12 esto ocurría en dos parejas:

- portón sur con FID `doors=1333`, `graph_nodes=5480`;
- portón norte con FID `doors=1334`, `graph_nodes=5482`.

## Método V13

1. Solo considera lados de puertas asociados explícitamente al mismo espacio.
2. Conserva el rechazo de la línea recta que sale del `IfcSpace`.
3. Busca el lado de puerta ya conectado más próximo dentro de ese espacio.
4. Calcula una trayectoria mediante visibilidad en el polígono erosionado por
   la mitad de la anchura mínima de circulación.
5. Comprueba por separado 0,90 m para peatón general y 1,20 m para silla de
   ruedas.
6. Añade siempre los dos sentidos y registra
   `relation_source=same_ifc_space_visibility_bridge_v13`.

No se conectan puertas por mera proximidad ni se crean enlaces entre espacios
distintos.

## Validación del IFC 12

- `PRAGMA integrity_check`: `ok`.
- 9.549 nodos y 19.098 conexiones dirigidas.
- 66 aristas dirigidas V13, equivalentes a 33 puentes bidireccionales.
- 0 conexiones peatonales sin inversa.
- 17 portones articulados comprobados y 0 aislados.
- Los siete portones FID 1332–1338 alcanzan el componente principal de 8.134
  nodos.
- 0 puentes V13 fuera del polígono de su `IfcSpace`.
- 64 pruebas unitarias superadas.
