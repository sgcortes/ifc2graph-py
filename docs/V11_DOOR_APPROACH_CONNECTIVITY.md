# HSIMG V11: aproximaciones a puertas de vestíbulos

V11 corrige las puertas correctamente asociadas a un vestíbulo cuyo nodo de
proyección quedaba aislado después de erosionar el espacio para comprobar la
anchura del pasillo.

La proximidad a una pared no se utiliza como anchura en el cuello de la puerta.
Ese tramo se valida con la anchura real de `IfcDoor`; después se crea un segundo
tramo contenido íntegramente en el dominio peatonal erosionado. Los dos tramos
son bidireccionales, permanecen dentro del mismo `IfcSpace` y no pueden
atravesar huecos ni obstáculos.

V11 añade además dos comprobaciones:

- toda proyección de puerta de un espacio de movilidad horizontal debe alcanzar
  su eje interno;
- toda puerta exterior elegible debe alcanzar al menos una puerta interior.

Las aristas nuevas registran
`relation_source=door_width_validated_throat_v11` y
`relation_source=door_approach_safe_connector_v11`.
