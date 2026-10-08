# V14: limpieza funcional de ejes horizontales

V14 conserva la extracción IFC, las reglas de anchura y la movilidad vertical
de V13. La revisión del modelo de entrada (por ejemplo IFC **v13**) y la versión
del generador (**HSIMG V14**) son identificadores independientes.

## Problemas corregidos

1. El eje medial produce circuitos alrededor de pilares y ramales hacia
   esquinas que no conducen a accesos. La poda anterior de ramas cortas no
   elimina circuitos.
2. Un cambio de dirección de la polilínea no necesita ser un nodo del grafo.
3. La inserción V12 de una unión puerta-eje añadía dos tramos sin retirar el
   tramo original, creando una representación superpuesta del mismo eje.

## Regla de simplificación

La limpieza se ejecuta **después** de construir puertas, escaleras, rampas,
ascensores y costes. Para cada subgrafo horizontal:

- Se protegen los nodos que conectan con puertas, otros espacios o movilidad
  vertical, las proyecciones de puertas, los desembarcos y destinos explícitos.
- Se conserva la unión de las rutas mínimas entre todos los pares ordenados
  de esos terminales, para longitud y coste del perfil general y del perfil
  de silla de ruedas. Se respetan sentidos y restricciones de cada arista.
- Los tramos que no participan en ninguna de esas rutas se eliminan junto
  con sus nodos derivados aislados. La arista inversa de cada conexión
  bidireccional conservada permanece.
- Una componente con menos de dos terminales se conserva para diagnóstico;
  no se elimina para mejorar artificialmente la conectividad del informe.
- Los nodos internos de grado dos, sin función de acceso y con restricciones
  compatibles, se contraen concatenando las **polilíneas originales**. No se
  sustituyen por una recta. Longitudes, tiempos y costes se suman; se mantiene
  la trazabilidad a aristas y nodos originales.
- Se recalculan todas las distancias entre terminales antes y después. Un
  cambio superior a la tolerancia numérica aborta la exportación.

No se incrementa la anchura mínima global (por defecto 0,90 m para el recorrido
general y 1,20 m para silla de ruedas). Una alternativa más larga se conserva
si es necesaria para un acceso, para otro perfil o para el coste seleccionado.
Los grandes circuitos de pasillos permanecen cuando intervienen en rutas
mínimas entre accesos. No se calcula un árbol de expansión mínimo.

La unión puerta-eje ahora sustituye la arista original cuando crea una unión
interior y se han generado correctamente las dos mitades bidireccionales.
Las validaciones de acceso al eje y cobertura de regiones también consideran
las polilíneas completas, para no exigir nodos intermedios que la reducción
ha sustituido correctamente por vértices geométricos.

## Alcance

Este producto es un grafo reducido para calcular rutas entre accesos modelados.
La distancia se conserva respecto al grafo original, no se certifica el mínimo
geométrico continuo dentro del edificio. Los puntos arbitrarios que no son
destinos modelados no forman parte de esa garantía. Para estudios de redundancia,
rutas alternativas o fallo de conexiones, utilizar la opción
`--keep-all-horizontal-alternatives` y conservar el grafo sin reducción.

Las incidencias originales del IFC y de conectividad permanecen visibles. La
limpieza no implica que todos los espacios sean accesibles ni sustituye una
validación contra planos o inspección independiente.

## Ejecución

```powershell
.\.venv\Scripts\python.exe scripts\run_v14_export.py modelo.ifc `
  --output-dir deliverables\modelo_v14 --name EPM_IFC_v13_HSIMG_v14
```

Salidas: GeoPackage, GraphML, JSON, `validation_issues.csv`, `run_report.json`
y `horizontal_cleanup_actions.json`. El informe incluye SHA-256 del IFC y del
GeoPackage, tiempos, conteos y comprobaciones de preservación de rutas.

El GeoPackage incorpora `horizontal_cleanup_v14`, con registros `summary`,
`subgraph` y `action` en JSON. Las acciones de eliminación incluyen la geometría
original WKT; las contracciones identifican el nodo y las aristas sustitutas.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Se cubren circuitos alrededor de pilares, ramales sin destino, accesos en un
recorrido alternativo, restricciones de silla de ruedas, sentidos únicos,
duplicados, conservación de curvas, componentes con un solo acceso, diferencias
entre longitud y coste y sustitución correcta de un eje al insertar una puerta.
