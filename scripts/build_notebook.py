"""Generate the self-contained Google Colab HSIMG notebook."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def md(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": source.splitlines(keepends=True)}


def code(source: str) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": source.splitlines(keepends=True)}


def section(number: int, title: str, body: str) -> dict:
    return md(f"## {number}. {title}\n\n{body}\n")


def main() -> None:
    module_source = (ROOT / "hsimg.py").read_text(encoding="utf-8")
    cells = [
        md("# HSIMG 3D desde BIM/IFC\n\n**Prototipo de investigación reproducible para Google Colab.** Construye un *Hierarchical Semantic Indoor Mobility Graph* tridimensional, conserva trazabilidad IFC, modela perfiles de accesibilidad y exporta un GeoPackage.\n\nEl flujo implementa cuatro etapas: extracción semántica, geometría, ensamblaje jerárquico y validación/exportación. Las relaciones inferidas nunca se presentan como semántica IFC: llevan `relation_source` y `confidence`."),
        section(1, "Título y objetivo de investigación", "El objetivo es obtener un grafo `nx.MultiDiGraph` que combine nodos semánticos globales con subgrafos internos de movilidad horizontal y vertical. La jerarquía se aplana mediante `parent_node_id`, `subgraph_id` y `hierarchy_level`, lo que permite persistirla en NetworkX y GeoPackage."),
        section(2, "Entorno de ejecución", "El notebook detecta Colab, informa versiones y mantiene las coordenadas IFC en metros. Si no hay georreferenciación, no asigna WGS84 ni otro CRS arbitrario."),
        code("import sys, platform\nprint('Python:', sys.version)\nprint('Platform:', platform.platform())\nIN_COLAB = 'google.colab' in sys.modules\nprint('Google Colab:', IN_COLAB)\n"),
        section(3, "Instalación de bibliotecas", "Se emplean exclusivamente bibliotecas abiertas. `rtree` es opcional porque Shapely 2 incluye `STRtree`."),
        code("%pip install -q 'ifcopenshell>=0.8.0' 'networkx>=3.2' 'shapely>=2.0' 'geopandas>=0.14' 'pandas>=2.0' 'numpy>=1.24' 'scipy>=1.10' 'scikit-image>=0.22' 'matplotlib>=3.7' 'pyproj>=3.6' 'fiona>=1.9' 'tqdm>=4.66'\n"),
        section(4, "Imports e implementación modular", "La siguiente celda materializa el módulo reutilizable. Esto hace que el `.ipynb` sea autocontenido en Colab y, al mismo tiempo, permite importar la misma implementación desde otros experimentos."),
        code("%%writefile hsimg.py\n" + module_source),
        code("from pathlib import Path\nimport json, logging, os, sqlite3\nimport matplotlib.pyplot as plt\nimport networkx as nx\nimport pandas as pd\nimport geopandas as gpd\nimport ifcopenshell\nfrom hsimg import HSIMGBuilder, HSIMGConfig, print_summary\nlogging.getLogger('hsimg').setLevel(logging.INFO)\n"),
        section(5, "Parámetros de configuración", "Todos los umbrales metodológicos se concentran en una dataclass validada. Ajuste `manual_crs` solo si conoce el CRS real del modelo."),
        code("CONFIG = HSIMGConfig(\n    horizontal_mobility_min_doors=3,\n    spatial_tolerance_m=0.05,\n    vertical_alignment_tolerance_m=0.25,\n    elevator_grouping_tolerance_m=1.50,\n    door_projection_max_distance_m=5.0,\n    door_space_search_distance_m=0.60,\n    minimum_walkable_width_m=0.90,\n    wheelchair_min_door_width_m=0.80,\n    maximum_accessible_ramp_slope=0.08,\n    medial_axis_pruning_length_m=0.50,\n    skeleton_resolution_m=0.15,\n    manual_crs=None,  # Ejemplo: 'EPSG:25830' solo si está verificado\n    generate_diagnostics=True,\n    export_debug_layers=True,\n)\nCONFIG\n"),
        section(6, "Carga del archivo IFC", "En Colab se abre un selector de archivos. En ejecución local se reutiliza `04_EPM_full.ifc` si existe; también puede definir la variable de entorno `IFC_PATH`."),
        code("candidate = Path(os.environ.get('IFC_PATH', '04_EPM_full.ifc'))\nif candidate.exists():\n    IFC_PATH = candidate\nelif IN_COLAB:\n    from google.colab import files\n    uploaded = files.upload()\n    if not uploaded:\n        raise RuntimeError('No se cargó ningún IFC')\n    IFC_PATH = Path(next(iter(uploaded)))\nelse:\n    raise FileNotFoundError('Defina IFC_PATH o copie el IFC junto al notebook')\nprint('IFC:', IFC_PATH, f'({IFC_PATH.stat().st_size/1e6:.1f} MB)')\n"),
        section(7, "Inspección del modelo IFC", "Se contabilizan las entidades relevantes, se identifica el esquema y se registran unidades y versiones de software."),
        code("builder = HSIMGBuilder.from_file(IFC_PATH, CONFIG)\ninspection = builder.inspect_model()\npd.Series(inspection['counts'], name='count').to_frame()\n"),
        section(8, "Análisis del sistema de coordenadas", "IFC4 puede declarar `IfcMapConversion`; IFC2X3 suele conservar coordenadas locales o compartidas sin CRS EPSG. La ausencia de CRS se registra como `LOCAL_ENGINEERING_CRS`."),
        code("pd.Series(inspection['georeferencing'], name='value').to_frame()\n"),
        section(9, "Funciones auxiliares de geometría", "`GeometryEngine` aplica transformaciones globales de IfcOpenShell, proyecta triángulos de malla para generar huellas, repara polígonos e identifica puntos interiores navegables. El eje medial se rasteriza y vectoriza; si falla, se crea un grafo de visibilidad validado dentro del polígono."),
        code("from hsimg import GeometryEngine\ngeometry_engine = builder.geometry\nprint('USE_WORLD_COORDS activo; resolución de esqueleto:', CONFIG.skeleton_resolution_m, 'm')\n"),
        section(10, "Extracción de plantas", "Las plantas se extraen como registros estables basados en GlobalId; la cota procede de `Elevation` o de la colocación global."),
        code("builder.extract_storeys()\nstoreys_df = pd.DataFrame([vars(s) if hasattr(s, '__dict__') else {k:getattr(s,k) for k in s.__dataclass_fields__} for s in builder.storeys.values()])\nstoreys_df\n"),
        section(11, "Extracción de espacios", "Cada `IfcSpace` conserva GlobalId, id interno IFC, clase, metadatos, huella, área, elongación y un punto navegable garantizado en el interior cuando existe geometría."),
        code("builder.extract_spaces()\nspaces_preview = pd.DataFrame([{\n    'space_id': s.space_id, 'ifc_guid': s.ifc_guid, 'name': s.name, 'storey_id': s.storey_id,\n    'area_m2': s.area, 'aspect_ratio': s.aspect_ratio, 'geometry': s.extraction_method\n} for s in builder.spaces.values()])\nspaces_preview.head()\n"),
        section(12, "Extracción de puertas", "Las puertas se representan por un portal 3D estable a partir de su colocación global. La huella detallada es opcional para evitar que familias Revit complejas dominen el tiempo de proceso."),
        code("builder.extract_doors()\ndoors_preview = pd.DataFrame([{\n    'door_id': d.door_id, 'ifc_guid': d.ifc_guid, 'name': d.name, 'storey_id': d.storey_id,\n    'width_m': d.width, 'height_m': d.height, 'wheelchair': d.wheelchair_accessible\n} for d in builder.doors.values()])\ndoors_preview.head()\n"),
        section(13, "Relaciones puerta–espacio", "Se prioriza `IfcRelSpaceBoundary`. Cuando falta, un índice espacial busca hasta dos espacios compatibles con la planta y próximos al portal. Las relaciones se marcan como `IFC_semantic`, `geometry_inferred`, `hybrid` o `unresolved`."),
        code("builder.analyse_door_space_relationships()\nrelation_summary = pd.Series([d.relation_source for d in builder.doors.values()]).value_counts().rename_axis('source').to_frame('doors')\nrelation_summary\n"),
        section(14, "Clasificación de espacios finalistas y de movilidad horizontal", "El clasificador híbrido combina vocabulario multilingüe, número de puertas, área y elongación; la puntuación y la fuente quedan persistidas."),
        code("builder.classify_spaces()\nclassification_df = pd.DataFrame([{\n    'name': s.name, 'doors': s.number_of_doors, 'area': s.area, 'aspect': s.aspect_ratio,\n    'class': s.node_class, 'score': s.classification_score, 'source': s.classification_source\n} for s in builder.spaces.values()]).sort_values('score', ascending=False)\nclassification_df.head(20)\n"),
        section(15, "Generación del eje medial horizontal", "El método principal usa `skimage.morphology.skeletonize`, vectoriza cadenas de píxeles y elimina ramas por debajo de `medial_axis_pruning_length_m`. Los polígonos demasiado grandes o fallos numéricos activan `fallback_visibility_graph`."),
        code("builder.assemble_space_and_door_graph()\nprint('Nodos semánticos y portales:', builder.graph.number_of_nodes())\n"),
        section(16, "Subgrafos internos horizontales", "Los accesos laterales de cada puerta se proyectan al eje solo si la conexión queda dentro del dominio caminable. Los enlaces que cruzan límites no se incorporan."),
        code("builder.build_horizontal_subgraphs()\npd.Series([s['extraction_method'] for s in builder.subgraphs], name='method').value_counts()\n"),
        section(17, "Extracción y agrupación de ascensores", "En IFC4 se usa `PredefinedType=ELEVATOR`; en IFC2X3 se combinan nombres, tipos y propiedades. Las ocurrencias se agrupan por proximidad XY, solape de huella y nombre normalizado, sin exigir igualdad exacta."),
        code("builder.extract_elevators()\nprint('Sistemas de ascensor:', sum(v.vertical_type == 'elevator' for v in builder.vertical_elements.values()))\n"),
        section(18, "Escaleras y trayectoria 3D", "Los tramos agregados se convierten en ejes 3D mediante cuantiles inferior/superior de la malla. Así se conserva una trayectoria inclinada por tramo, no una línea vertical abstracta."),
        code("builder.extract_stairs()\nprint('Escaleras:', sum(v.vertical_type == 'stair' for v in builder.vertical_elements.values()))\n"),
        section(19, "Rampas y accesibilidad", "Las rampas se procesan como trayectorias 3D; la viabilidad para silla de ruedas considera pendiente y anchura cuando están disponibles, y conserva la incertidumbre si faltan."),
        code("builder.extract_ramps()\nprint('Rampas:', sum(v.vertical_type == 'ramp' for v in builder.vertical_elements.values()))\n"),
        section(20, "Ensamblaje global HSIMG", "Los padres verticales, paradas, rellanos y tramos se añaden al mismo `MultiDiGraph`. Los nodos internos apuntan a su padre y subgrafo."),
        code("builder.build_vertical_subgraphs()\nbuilder.apply_profile_costs()\nprint('Graph:', builder.graph.number_of_nodes(), 'nodes /', builder.graph.number_of_edges(), 'directed edges')\n"),
        section(21, "Metadatos de accesibilidad y perfiles", "Se materializan costes para peatón general y usuario de silla de ruedas. Los arcos de escalera, puertas estrechas y rampas no conformes quedan excluidos del perfil accesible."),
        code("pd.DataFrame(builder.user_profiles)\n"),
        section(22, "Validación del grafo", "El informe detecta geometría ausente/inválida, espacios o puertas sin conexión, solapes, subgrafos ausentes, elementos verticales incompletos, arcos degenerados, cruces de límites y componentes desconectados."),
        code("builder.validate_graph()\nvalidation_df = builder.validation_dataframe()\nvalidation_df.to_csv('HSIMG_validation_report.csv', index=False)\nvalidation_df.groupby(['severity', 'issue_type']).size().rename('count').to_frame().sort_values('count', ascending=False).head(30)\n"),
        section(23, "Cálculo de rutas", "Se seleccionan dos destinos del componente principal cuando es posible. El perfil wheelchair filtra arcos no accesibles antes de ejecutar Dijkstra."),
        code("def example_route(profile='general'):\n    allowed = nx.MultiDiGraph()\n    allowed.add_nodes_from(builder.graph.nodes(data=True))\n    for u,v,k,d in builder.graph.edges(keys=True, data=True):\n        if profile != 'wheelchair' or d.get('accessible_wheelchair', False):\n            allowed.add_edge(u,v,key=k,**d)\n    components = sorted(nx.weakly_connected_components(allowed), key=len, reverse=True)\n    if not components or len(components[0]) < 2:\n        print('No hay un componente con dos nodos para el perfil', profile); return None\n    destinations = [n for n in components[0] if builder.graph.nodes[n].get('node_type') == 'space']\n    if len(destinations) < 2:\n        destinations = list(components[0])\n    try:\n        return builder.compute_route(destinations[0], destinations[-1], profile)\n    except nx.NetworkXNoPath:\n        print('No se encontró ruta dirigida de ejemplo para', profile); return None\n\ngeneral_route = example_route('general')\nwheelchair_route = example_route('wheelchair')\ngeneral_route, wheelchair_route\n"),
        section(24, "Visualización", "Se muestran espacios, puertas, ejes y nodos por planta, además del grafo 3D y la ruta de ejemplo. Puede desactivar esta etapa mediante `generate_diagnostics=False`."),
        code("if CONFIG.generate_diagnostics:\n    first_storey = next(iter(builder.storeys), None)\n    builder.visualize_storey(first_storey)\n    plt.show()\n    builder.visualize_3d(general_route)\n    plt.show()\n"),
        section(25, "Exportación a GeoPackage", "El GeoPackage contiene `spaces`, `doors`, `graph_nodes`, `graph_edges`, `mobility_axes`, `vertical_elements`, `validation_issues` y las tablas `subgraphs`, `user_profiles`, `model_metadata`. También se generan GraphML y JSON."),
        code("GPKG_PATH = Path('HSIMG_output.gpkg')\nbuilder.export_geopackage(GPKG_PATH)\nbuilder.export_graph('HSIMG_graph.graphml', 'HSIMG_graph.json')\nwith sqlite3.connect(GPKG_PATH) as connection:\n    layers = pd.read_sql_query('SELECT table_name, data_type FROM gpkg_contents ORDER BY table_name', connection)\nlayers\n"),
        section(26, "Resumen final y descarga", "El resumen es apto para registrar la ejecución experimental. En Colab se muestran enlaces de descarga para todos los productos."),
        code("print_summary(builder, GPKG_PATH)\nif IN_COLAB:\n    from google.colab import files\n    for output in [GPKG_PATH, Path('HSIMG_validation_report.csv'), Path('HSIMG_graph.graphml'), Path('HSIMG_graph.json')]:\n        if output.exists():\n            display(files.download(str(output)))\n"),
        section(27, "Limitaciones y trabajo futuro", "Este prototipo no afirma resolver todos los IFC. La ausencia de `IfcRelSpaceBoundary`, geometría de espacio, propiedades de accesibilidad o georreferenciación reduce la confianza y genera avisos. La trayectoria de escaleras/rampas es una aproximación basada en malla; una validación de producción debería usar geometría de peldaños, landings, obstáculos interiores, estados de puerta y normas locales. La clasificación de circulación debe calibrarse con datos anotados. Los resultados inferidos deben revisarse antes de decisiones de seguridad, evacuación o cumplimiento normativo."),
    ]
    for index, cell in enumerate(cells):
        cell["id"] = f"cell-{index:03d}"
    notebook = {
        "cells": cells,
        "metadata": {
            "colab": {"name": "HSIMG_3D_IFC_Colab.ipynb", "provenance": []},
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.x"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    output = ROOT / "HSIMG_3D_IFC_Colab.ipynb"
    output.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
