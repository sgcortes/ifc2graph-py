# IFC2GRAPH V9: continuidad horizontal de desembarcos

V9 corrige el caso en que una puerta real enlaza un ascensor con un hall, pero
la simplificacion y poda del eje medio deja el hall dividido en componentes.
La parada parece conectada localmente, aunque no puede alcanzar el resto del
pasillo de la planta.

## Puentes dentro del mismo espacio

V9 busca pares de componentes internos del mismo `IfcSpace`. Solo los conecta
si una linea de hasta 12 m:

- permanece completamente dentro de la huella transitable;
- conserva al menos 0,90 m de anchura peatonal;
- no atraviesa huecos que representan pilares, muros u otros obstaculos;
- tiene sus dos extremos asociados al mismo espacio.

Las aristas son bidireccionales y se exportan con
`relation_source=same_space_clearance_component_bridge_v9`.

## Pasos abiertos entre espacios

Dos espacios horizontales diferentes solo pueden recibir una transicion sin
puerta cuando comparten un tramo de frontera de anchura suficiente y no existe
un `IfcWall` que aparezca como frontera de ambos. Una separacion geometrica,
aunque sea pequena, no se salva automaticamente.

Las transiciones aceptadas usan
`relation_source=IfcSpace_wall_free_shared_boundary_v9`.

## Validacion de ascensores

Para cada parada con puerta de desembarco, V9 construye el componente de su
planta y comprueba que contiene un espacio de movilidad horizontal. Las
excepciones se registran como
`elevator_landing_without_horizontal_reach_v9` para permitir revisar espacios
de servicio internos que no sean pasillos publicos.
