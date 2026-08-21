# IFC2GRAPH V8: recuperacion de atajos del eje medio

V8 corrige rodeos artificiales producidos cuando la simplificacion del eje
medio omite una conexion visible entre dos ramas que siguen perteneciendo al
mismo componente. No genera un grafo de visibilidad completo ni conecta
espacios IFC diferentes.

## Regla de aceptacion

Una nueva pareja de aristas se crea solo si se cumplen simultaneamente estas
condiciones:

1. Ambos extremos son nodos horizontales internos del mismo `IfcSpace`.
2. Ya existe un recorrido entre ellos; V8 no fabrica conexiones entre
   componentes desconectados.
3. La distancia directa no supera `horizontal_shortcut_max_length_m`.
4. El recorrido previo supera tanto el factor
   `horizontal_shortcut_min_stretch_ratio` como el ahorro absoluto
   `horizontal_shortcut_min_saving_m`.
5. La linea completa esta cubierta por el dominio transitable erosionado para
   la anchura peatonal general.
6. La accesibilidad en silla de ruedas se concede solo si tambien supera el
   umbral especifico de ese perfil.

Cada conexion se exporta en ambas direcciones con:

- `edge_type=component_connector`
- `relation_source=clearance_validated_visibility_shortcut_v8`
- `validation_status=valid_shortcut_v8`

`metadata_json` conserva la distancia anterior, distancia directa, ahorro,
factor de rodeo, anchura minima y umbrales aplicados.

## Ejecucion

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\run_v8_export.py" `
  "C:\ruta\modelo.ifc" --output-dir ".\.qa\v8_full_run"
```

Valores predeterminados:

- longitud maxima: 12 m;
- factor minimo de rodeo: 1,75;
- ahorro minimo: 3 m;
- maximo por espacio: 64 conexiones;
- anchura peatonal general: 0,90 m;
- anchura para silla de ruedas: 1,20 m.

El ejecutor verifica la integridad SQLite, la reciprocidad de todas las
conexiones, la ausencia de atajos entre espacios y que el GeoPackage no
contenga aristas no aptas para peatones.
