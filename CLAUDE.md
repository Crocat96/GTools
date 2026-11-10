# G-Tools — contexto para Claude

Add-on de Blender (modelado hard-surface + pie menus) de Gabriel Jimenez. Responde en **español**.
Deriva de HEAVYPOLY (archivos `HEAVYPOLY_*`) con herramientas propias nuevas (archivos sin prefijo).

## Estructura

- `GTools/` es el paquete del add-on (lo que va al zip). Todo lo demás (README, workflow, este archivo) es del repo.
- `GTools/blender_manifest.toml`: manifest de extensión. `id = "gtools"`, `blender_version_min = "5.0.0"`.
  `[build].paths_exclude_pattern` deja fuera `config/`, `Keymaplists.py`, `HEAVYPOLY_menu_master.py`
  y `HEAVYPOLY_select_through_border.py` (código muerto; los dos últimos usan `bgl`, eliminado en 4.0).
- `GTools/__init__.py`: importa/recarga los módulos de `modules_names` y llama a su `register()`.
  Un módulo nuevo **hay que añadirlo a esa lista**. El `bl_info` se ignora como extensión pero se mantiene.
- Usar siempre imports relativos y `__package__` (como extensión el paquete es `bl_ext.<repo>.gtools`).

## Distribución (repositorio de extensiones)

- Push a `main` que toque `GTools/**` → workflow `.github/workflows/publish-extension.yml`: descarga Blender 5.0.x
  para Linux, `extension validate` + `build` + `server-generate --html`, y publica en GitHub Pages.
- URL del repositorio en Blender: `https://crocat96.github.io/GTools/index.json` (Pages con fuente "GitHub Actions").
- **Para que Blender ofrezca la actualización hay que subir `version` en el manifest** (y por coherencia en `bl_info`).
- Probar el build local: `"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" --command extension build --source-dir GTools --output-dir <dir>`.
- En este PC hay Blender 3.6, 4.3, 5.0, 5.1 y 5.3; el ejecutable instalado en Program Files es el 5.0.1.
  En `%APPDATA%\Blender Foundation\Blender\5.0\scripts\addons\GTools` sigue la versión antigua como add-on clásico:
  al probar con `--python`, importar una copia renombrada para no cargar esa por error.

## Módulos

Herramientas propias (prefijo de clases `GTOOLS_`, operadores `*.gtools_*`):

| Módulo | Qué hace |
|---|---|
| `smart_cut` | Dibuja una línea/polilínea y corta. Edit Mode: bisect destructivo. Object Mode: cutter caja + Sharp Subtract. Tiene WorkSpaceTools. |
| `sharp_subtract` | Boolean vivo con Geometry Nodes (grupo `GT_SharpSubtract`), un modificador por cutter, aristas de corte en sharp. |
| `edge_bevel` | Click en arista en Object Mode → bevel no destructivo hecho como boolean (grupo `GT_Subtract`), Alt = loop entero. |
| `loop_resample` | Cambia el nº de lados de un loop manteniendo la forma; también segmentos/radio de bevels (estilo MESHmachine). |
| `curve_align` | Ajusta vértices a una curva Bezier o caras a un patch Bezier, con puntos de control editables. |
| `modifier_scroll` | Recorre con la rueda los booleanos del objeto activo (estilo HardOps). |
| `quick_decal` | Aplana un objeto sobre otro como decal conservando normales (grupo `GT_Decal`). |
| `panel_line` | Panel line estilo DECALmachine desde un cutter (grupo `GT_PanelLine_strip`). |
| `ornament` | Shift A → curva con molduras por Geometry Nodes (`GT_Ornament_v2`, perfiles `GT_Profile_*`). |
| `xray_select` | Box/lasso/circle con X-ray temporal. |
| `keymaps` + `keymap_data` | Atajos configurables en las preferencias del add-on (ver abajo). |
| `cheatsheet` | Panel "G-Tools" en la barra lateral (N) con los atajos activos. |

Heredados de HEAVYPOLY (`HP_*`): pies de selección, vista, shading, booleanos, añadir y guardar,
operadores "smart" (extrude, bevel, delete, subdivision toggle…), dibujar primitivas, panel de render.

## Convenciones

- Booleanos vivos: los cutters se llaman `<base>_Cutter_N` y viven en la colección de Cutters. Toggle Cutters,
  Smart Delete, Apply Booleans y Modifier Scroll dependen de ese nombre: respetarlo en cualquier herramienta nueva.
- Los node groups de Geometry Nodes se crean por código con prefijo `GT_` y se reutilizan si ya existen.
- Atajos (`keymaps.py`): los nuevos van al keyconfig **addon**; los de Blender que chocan se desactivan en el
  keyconfig **user** y se restauran al cambiar ajustes o desactivar. Los modal maps van al **user** (Blender no
  admite modal en addon). `keymap_data.py` se generó comparando un export HEAVYPOLYLITE con el keymap de Blender 5.3.

## Verificar cambios

- Registro: ejecutar en Blender `--background --factory-startup --python` un script que importe el paquete,
  haga `register()`/`unregister()` dos veces y compruebe que no quedan clases. Estado a 2026-11-10: limpio, 54 clases.
- Antes de usar una API de Blender, comprobarla en Blender 5.0 (`bl_rna`, `hasattr(bpy.ops.x, "y")`), no de memoria.

## Problemas conocidos (revisión 2026-11-10)

- Grease Pencil antiguo: desde Blender 4.3 el tipo es `'GREASEPENCIL'` y los modos `*_GREASE_PENCIL`.
  - `HEAVYPOLY_pie_add.py`: "GPencil" llama a `bpy.ops.object.gpencil_add()` (ya no existe → error)
    y a `mode_set(mode='GPENCIL_PAINT')` (modo inexistente). Debería ser `object.grease_pencil_add` + `PAINT_GREASE_PENCIL`.
  - `HEAVYPOLY_pie_selection.py`, `HEAVYPOLY_OPERATORS.py`: ramas `case "GPENCIL"` y `bpy.ops.gpencil.*` muertas;
    con un objeto Grease Pencil el pie de selección cae en el caso por defecto (botones de malla).
  - `HP_OT_gp_canvas` no hace nada (asigna a una variable local).
- `HP_MT_pie_select.draw` usa `context.object.type` sin comprobar `None`: sin objeto activo el pie da error.
- Muchos operadores `HP_*` no tienen `poll()` y asumen objeto activo.
- Imports y variables sin usar en los `HEAVYPOLY_*` (cosmético).
