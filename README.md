# IFC2GRAPH-PY

Conversión de modelos IFC en grafos tridimensionales de movilidad interior y
exportación a GeoPackage. El repositorio conserva la aplicación FastAPI
original y añade el generador jerárquico HSIMG empleado en las validaciones más
recientes.

## Generador actual: HSIMG V13

V13 construye un `MultiDiGraph` 3D con espacios, puertas, ejes internos de
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

## Instalación

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
```

## Generar un GeoPackage V13

```powershell
.\.venv\Scripts\python.exe scripts\run_v13_export.py modelo.ifc `
  --output-dir .qa\modelo_v13_release
```

El resultado principal será:

```text
.qa/modelo_v13_release/HSIMG_v13_output.gpkg
```

Para validar posteriormente un archivo generado:

```powershell
.\.venv\Scripts\python.exe scripts\validate_v13_release.py `
  .qa\modelo_v13_release\HSIMG_v13_output.gpkg
```

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

La versión publicada incluye 64 pruebas automatizadas.

## Archivos principales

- `hsimg.py`: núcleo del generador.
- `v13hsimg.py`: versión más reciente.
- `v2hsimg.py`–`v12hsimg.py`: cadena histórica requerida por V13.
- `scripts/run_v13_export.py`: ejecución y exportación completa.
- `scripts/validate_v13_release.py`: validación de GeoPackages V13.
- `GEOPACKAGE_SCHEMA.md`: descripción de capas y atributos.
- `docs/V13_FINALIST_GATE_CONNECTIVITY.md`: metodología específica de V13.
- `bim_mapper.py`, `app.py` y `static/`: aplicación FastAPI original.

Los modelos IFC, los GeoPackages generados y los directorios `.qa` no se
versionan en GitHub.
