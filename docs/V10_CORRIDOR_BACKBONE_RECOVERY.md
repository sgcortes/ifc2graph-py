# HSIMG V10: continuidad segura de pasillos

V10 corrige la desaparición de tramos largos de pasillos continuos después de
la validación de anchura de V6. La geometría IFC, las puertas, los ascensores y
las reglas de obstáculos de V9 se mantienen.

## Cambios

- La poda de callejones sin salida deja de ser ilimitadamente recursiva. Solo
  elimina un ramal completo cuando su longitud acumulada no supera el umbral
  configurado (`dead_end_max_prune_length_m`, 1,50 m por defecto).
- Los componentes sin acceso directo a una puerta se conservan cuando su
  longitud supera `accessless_component_max_prune_length_m` (2,00 m). Así
  pueden ser reparados en vez de desaparecer.
- Los componentes de un mismo `IfcSpace` se reconectan mediante el camino de
  visibilidad más corto contenido en el dominio erosionado por la anchura
  peatonal mínima. El camino puede ser curvo para rodear patios y pilares.
- Las reparaciones son bidireccionales, pertenecen siempre al mismo espacio y
  registran `relation_source=clearance_domain_visibility_backbone_v10`.
- La validación comprueba cada componente geométrico del dominio transitable y
  diagnostica regiones sin grafo o con más de una componente topológica.

No se reduce la anchura mínima de 0,90 m y no se permiten líneas que atraviesen
muros, pilares, patios u otros huecos conservados.
