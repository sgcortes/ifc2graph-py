# IFC2GRAPH-PY

Conversión de modelos IFC en grafos tridimensionales de movilidad interior y
exportación a GeoPackage. El repositorio conserva la aplicación FastAPI
original y añade el generador jerárquico HSIMG empleado en las validaciones más
recientes.

## Generador actual: HSIMG V15

V15 construye un `MultiDiGraph` 3D con espacios, puertas, ejes internos de
circulación, escaleras, rampas y ascensores. Exporta las capas espaciales y las
tablas de conectividad a GeoPackage, además de GraphML y JSON.

Entre las mejoras acumuladas se incluyen:

- relaciones puerta–espacio basadas en IFC y trazabilidad de las inferencias;
- ejes internos que respetan huecos, pilares y anchuras transitables;
- poda de conexiones no aptas para peatones o silla de ruedas;
- conexiones bidireccionales y validación de rutas;
- escaleras y ascensores conectados entre plantas consecutivas;
- accesos de ascensor derivados de sus huecos reales en muros frontera;
- recuperación segura de pasillos, vestíbulos y aproximaciones a puertas;
- conexión V13 de portones del mismo `IfcSpace` sin atravesar muros.

V14 elimina los circuitos y ramales horizontales que no participan en rutas
mínimas entre accesos funcionales. Conserva las distancias y costes de los
perfiles general y de silla de ruedas; los puntos internos de grado dos pasan
a ser vértices de las polilíneas, sin convertir curvas en rectas. También
sustituye el eje original al insertar una unión de puerta, evitando conexiones
superpuestas. La revisión del modelo IFC (por ejemplo **v13**) es independiente
de la versión del generador (**V14**).

La [metodología V14](docs/V14_FUNCTIONAL_HORIZONTAL_CLEANUP.md) describe las
garantías y los límites. Para estudiar redundancia o alternativas, exportar
con `--keep-all-horizontal-alternatives`.

V15 sustituye los accesos de escalera basados en centros de espacio por nodos
al principio/final de cada tramo y descansillos apoyados en geometría IFC.
Resuelve bifurcaciones y conexiones por las puertas de cada planta; los padres
jerárquicos no actúan como atajos. Incluye auditorías de alcance a pasillos por
planta y de ajustes de cota. Véanse los [límites de V15](docs/V15_STAIR_LANDING_NETWORKS.md).

## Instalación

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
```

## Generar un GeoPackage V15

```powershell
.\.venv\Scripts\python.exe scripts\run_v15_export.py modelo.ifc `
  --output-dir .qa\modelo_v15_release
```

El resultado principal será:

```text
.qa/modelo_v15_release/HSIMG_v15_output.gpkg
```

La ejecución comprueba integridad SQLite, referencias de aristas, reciprocidad,
continuidad de escaleras y preservación de rutas tras la limpieza. Genera
`run_report.json`, `validation_issues.csv` y `horizontal_cleanup_actions.json`.
El informe registra SHA-256 del IFC y del GeoPackage.

Para reproducir la validación histórica específica del caso V13:

```powershell
.\.venv\Scripts\python.exe scripts\validate_v13_release.py `
  .qa\modelo_v13_release\HSIMG_v13_output.gpkg
```

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

La versión publicada incluye 88 pruebas automatizadas. La comprobación del
GeoPackage exportado se ejecuta con `scripts/validate_v15_stairs.py` y distingue
integridad estructural de accesos de planta todavía sin validar.

## Archivos principales

- `hsimg.py`: núcleo del generador.
- `v15hsimg.py`: versión más reciente, con escaleras basadas en tramos y descansillos.
- `stair_surfaces.py`: secciones de obstáculos físicos para accesos abiertos.
- `horizontal_cleanup.py`: reducción y comprobación de rutas entre accesos.
- `v2hsimg.py`–`v14hsimg.py`: cadena histórica requerida por V15.
- `scripts/run_v15_export.py`: ejecución, exportación y auditoría V15.
- `scripts/run_v13_export.py`: ejecución y exportación completa.
- `scripts/validate_v13_release.py`: validación de GeoPackages V13.
- `GEOPACKAGE_SCHEMA.md`: descripción de capas y atributos.
- `docs/V13_FINALIST_GATE_CONNECTIVITY.md`: metodología específica de V13.
- `bim_mapper.py`, `app.py` y `static/`: aplicación FastAPI original.

Los modelos IFC, los GeoPackages generados y los directorios `.qa` no se
versionan en GitHub.
