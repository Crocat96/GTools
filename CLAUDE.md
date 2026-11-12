# G-Tools — contexto para Claude

Add-on de Blender (modelado hard-surface + pie menus) de Gabriel Jimenez. Responde en **español**.
**Se desarrolla para Blender 5.3** (el manifest admite desde 5.0; comprobar ambos cuando una API pueda diferir).
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
  Desde 0.9.0 (petición de Gabriel) se sube **de una en una en el último decimal**: 0.9.1, 0.9.2, 0.9.3…
- Probar el build local: `"<blender.exe>" --command extension build --source-dir GTools --output-dir <dir>`.
- Se trabaja en **dos PCs**; mirar cuál es por la ruta del repo o el usuario de Windows.
  - **PC de casa** (usuario `Hp`): 5.0.1 en `C:\Program Files\Blender Foundation\Blender 5.0\`; portables (3.6, 4.3,
    5.0, 5.1, **5.3 RC**) en `C:\Users\Hp\Documents\Blender\`. La extensión está instalada en el 5.3.
    En `%APPDATA%\Blender Foundation\Blender\5.0\scripts\addons\GTools` sigue la versión antigua como add-on clásico.
  - **PC del trabajo** (usuario `gjimenez`, repo en `D:\Blender\Extensions\GTools`): solo portables, sin 5.0 instalado:
    `D:\Blender\blender-5.1.0-windows-x64\blender-5.1.0-windows-x64\blender.exe` y
    `D:\Blender\blender-5.3.0-beta+v53.873fa9640966-windows.amd64-release\blender-5.3.0-beta+v53.873fa9640966-windows.amd64-release\blender.exe`
    (**5.3 beta**). La extensión aún no está instalada; en `%APPDATA%\Blender Foundation\Blender\5.1\scripts\addons\GTools`
    hay una versión antigua como add-on clásico. Git en `C:\Program Files\Git\cmd\git.exe` (puede no estar en el PATH);
    el `.gitconfig` global (H:) no es escribible, la identidad está en el config local del repo.
  - En ambos: al probar con `--python`, importar una copia renombrada para no cargar el add-on antiguo por error.

## Módulos

Herramientas propias (prefijo de clases `GTOOLS_`, operadores `*.gtools_*`):

| Módulo | Qué hace |
|---|---|
| `smart_cut` | Dibuja una línea/polilínea y corta. Edit Mode: bisect destructivo. Object Mode: cutter caja + Sharp Subtract. Tiene WorkSpaceTools. |
| `sharp_subtract` | Boolean vivo con Geometry Nodes (grupo `GT_SharpSubtract`), un modificador por cutter, aristas de corte en sharp. |
| `edge_bevel` | Click en arista en Object Mode → bevel no destructivo hecho como boolean (grupo `GT_Subtract`), Alt = loop entero. |
| `loop_resample` | Cambia el nº de lados de un loop manteniendo la forma; también segmentos/radio de bevels (estilo MESHmachine). Tipos: `LOOP`, `BEVEL` (strip) y `REBEVEL` (red con esquinas: un-bevel con `pointmerge` + `bmesh.ops.bevel` con `loop_slide=True` como el operador de Blender). **Transaccional** (ver Convenciones). REBEVEL debe reconstruir *exactamente* el bevel original con los mismos segmentos (comprobarlo en casos nuevos): planos **locales** por vértice (`_local_planes`), todos los vértices de un perfil al mismo punto (`_align_profiles`, prolongando aristas del borde hasta la arista viva), ancho y segmentos medidos solo en los lados (`_side_borders`, no en tapas), y "grosor" de cara = 2·área/perímetro. |
| `curve_align` | Ajusta vértices a una curva Bezier o caras a un patch Bezier, con puntos de control editables. |
| `modifier_scroll` | Recorre con la rueda los booleanos del objeto activo (estilo HardOps). |
| `quick_decal` | Aplana un objeto sobre otro como decal conservando normales (grupo `GT_Decal`). |
| `panel_line` | Panel line estilo DECALmachine desde un cutter (grupo `GT_PanelLine_strip`). |
| `ornament` | Shift A → curva con molduras por Geometry Nodes (`GT_Ornament_v2`, perfiles `GT_Profile_*`). |
| `curve_patch` | Pie Select (Object Mode, solo con 2–4 curvas seleccionadas) → objeto "Patch" con parche de Coons vivo (grupo `GT_CurvePatch`): 4 curvas = parche, 3 = triangular (lado Top colapsado), 2 = loft reglado. Orientación (Bottom/Top/Left/Right + Reverse) calculada al crear desde los extremos; editable en el modificador. Solo usa el primer spline. |
| `smart_slide` | En modo cara = **push**: cada vértice sigue su arista más alineada con la normal de las caras (línea infinita); por defecto la cara queda plana y paralela (avance = distancia / cos), E = misma distancia por arista; sin arista guía (cos < 0.25) va por la normal. Preview en POST_VIEW: antes de mover, todas las aristas posibles; deslizando, la arista seguida + su línea prolongada + origen. Shift G en Edit Mode (vía `mesh.push_and_slide`, que en modo cara sigue haciendo shrink/fatten). Vertex + edge slide en uno: cada vértice sigue una arista hacia un vecino no seleccionado tratada como línea infinita (pasa del vecino y, si no hay arista hacia el ratón, sigue la opuesta prolongada: sale por los bordes). E = Even, Shift = precisión, Ctrl = pasos de 0.1. **L = Lock axis** (congela el `guide`; factor con signo vía `factor_locked`, así al volver atrás sigue por las mismas aristas prolongadas; preview naranja). **Rueda / Tab / Shift Tab** = siguiente eje del vértice de referencia (`axes`, ordenados por ángulo) y lo bloquea, conservando la distancia en unidades de mundo. Lógica separada de la proyección (`project` inyectable) para poder testearla. |
| `xray_select` | Box/lasso/circle con X-ray temporal. |
| `photoshop_bridge` | Panel "Photoshop Bridge" (N > G-Tools, ajustes en `scene.gtools_bridge`): **un solo** render de la cámara activa → PNG 8/16 bit transparentes (Beauty, Clown Object, Clown Material, Mist, Alpha) en una subcarpeta por export → `.jsx` generado que se pasa a `Photoshop.exe` y apila los PNG como capas (Beauty arriba, resto ocultas) + guarda PSD. Beauty = motor de la escena con `write_still` a EXR multicapa + `save_render` a PNG (respeta la gestión de color). **En Blender 5.x el EXR multicapa va por `image_settings.media_type = 'MULTI_LAYER_IMAGE'`** y cada pase es una *parte* (subimage) distinta: leer con OpenImageIO recorriendo `seek_subimage`. Clowns = desde **Cryptomatte** (canales `Crypto{Object,Material}NN.r/g/b/a` en minúscula; pares id/cobertura, id 0 = nada; no hay en Workbench): color por hash del id, sin AA = id de mayor cobertura, con AA = media ponderada; alpha = alpha del beauty (así respeta recortes y transparencia). PNG RGBA escritos con alpha **no asociada** (`oiio:UnassociatedAlpha`; al leerlos con OIIO para comprobar, pasar ese config o premultiplica). Mist: pase Mist de Blender tal cual (`use_pass_mist`, canal `Mist.Z`; cerca = negro, lejos = blanco); el panel edita `world.mist_settings` (Start, Depth, Falloff). En 0.9.2 sustituyó a un pase Depth propio (Z normalizado con rangos y limpieza del ruido del *dithered* de EEVEE) por petición de Gabriel: no volver a Z. Caja "Resolution" en el panel: X/Y, porcentaje y botones `/ 2` y `x 2` (`render.gtools_bridge_scale_resolution`, factor, límites 4–65536). Todo ajuste tocado se guarda y restaura (`_render_state`). Photoshop se busca en el registro (`HKLM\SOFTWARE\Adobe\Photoshop\<ver>\ApplicationPath`) o en `photoshop_path` de las preferencias. Render bloqueante. |
| `keymaps` + `keymap_data` | Atajos configurables en las preferencias del add-on (ver abajo). |
| `cheatsheet` | Panel "G-Tools" en la barra lateral (N) con los atajos activos. |
| `tools_guide` | Pestaña "Tools Guide" de las preferencias: `GUIDE` = texto de cada herramienta (cómo se usa / qué hace), con buscador. **Al añadir o cambiar una herramienta, actualizar aquí su entrada.** |

Heredados de HEAVYPOLY (`HP_*`): pies de selección, vista, shading, booleanos, añadir y guardar,
operadores "smart" (extrude, bevel, delete, subdivision toggle…), dibujar primitivas, panel de render.

## Convenciones

- Booleanos vivos: los cutters se llaman `<base>_Cutter_N` y viven en la colección de Cutters. Toggle Cutters,
  Smart Delete, Apply Booleans y Modifier Scroll dependen de ese nombre: respetarlo en cualquier herramienta nueva.
- Los node groups de Geometry Nodes se crean por código con prefijo `GT_` y se reutilizan si ya existen.
- **Editar el bmesh de Edit Mode sin crashear Blender**: si un error salta a mitad de cambios (antes de
  `update_edit_mesh`) o se pasa geometría rota (aristas con 3+ caras, caras duplicadas) a ops en C como
  `bmesh.ops.bevel`, Blender puede cerrarse. Patrón de `loop_resample` (reutilizarlo en tools que reconstruyen
  geometría): ensayo completo sobre `bm.copy()` → `mesh_health` antes/después (`health_problem`) → si va bien,
  aplicar en el real con copia de seguridad (`_backup` / `_restore` = `bm.clear()` + `bm.from_mesh`). Lanzar
  `Unsafe(mensaje)` para abortar limpio. Antes de `bmesh.ops.bevel`, revisar la geometría (`_local_problem`).
  Nunca `bpy.ops` dentro de ese flujo; `bm.faces.new` comprobando `bm.faces.get` para no duplicar caras.
- Atajos (`keymaps.py`): los nuevos van al keyconfig **addon**; los de Blender que chocan se desactivan en el
  keyconfig **user** y se restauran al cambiar ajustes o desactivar. Los modal maps van al **user** (Blender no
  admite modal en addon). `keymap_data.py` se generó comparando un export HEAVYPOLYLITE con el keymap de Blender 5.3.

## Verificar cambios

- Registro: ejecutar en Blender `--background --factory-startup --python` un script que importe el paquete,
  haga `register()`/`unregister()` dos veces y compruebe que no quedan clases. Estado a 2026-11-10: limpio, 54 clases.
- Antes de usar una API de Blender, comprobarla en Blender 5.3 (`bl_rna`, `dir(bpy.ops.x)`), no de memoria.
- En `--background`, `bpy.ops.x('INVOKE_DEFAULT')` devuelve `PASS_THROUGH` sin ejecutar `invoke()`. Para probar un
  operador que solo tiene `invoke`, llamar `Clase.invoke(fake_self, context, None)` dentro de
  `context.temp_override(window=, area=<VIEW_3D>, region=<WINDOW>)`. Los `draw()` de menús se prueban con un layout falso.
- Crashes: ejecutar cada caso en **su propio proceso** de Blender (un segfault solo mata ese caso) y revisar la
  salud de la malla tras cada operación. Casos de estrés para Loop Resample: bevels convexos/cóncavos (L, estrella,
  ranura en U, cavidad), bevel 1 segmento, bevels solapados, banda de esfera/toro (no son bevel: deben cancelar),
  esquina interior recta y **esquina de bandeja con paredes inclinadas** (ángulos obtusos, extremo abierto en diagonal),
  y **caja con esquinas redondeadas + inset + cavidad** seleccionando el redondeo de una esquina entera (pared exterior →
  anillo superior → cavidad): strip que gira dos esquinas de 90° a lo largo. En strips, el perfil es la dirección
  *suave* (sin giros > 60°), no la que más gira en total.
- Modales y dibujo GPU de verdad (no funcionan en `--background`): Blender con interfaz +
  `--enable-event-simulate`, un `bpy.app.timers` que va paso a paso con `window.event_simulate(...)`
  (MOUSEMOVE / LEFTMOUSE / WHEELUPMOUSE / RET) y `bpy.ops.wm.quit_blender()` al final. Abre una ventana unos segundos.

## Grease Pencil

Desde Blender 4.3 (GP v3): tipo de objeto `'GREASEPENCIL'`, modos `EDIT` / `PAINT_GREASE_PENCIL` /
`SCULPT_GREASE_PENCIL` / `VERTEX_GREASE_PENCIL` / `WEIGHT_GREASE_PENCIL`, operadores `grease_pencil.*` y
`object.grease_pencil_add`. Los antiguos `'GPENCIL'`, `gpencil.*` y `object.gpencil_add` ya no existen.
Migrado en 0.2.1 (pie de añadir, pie de selección, Separate and Select, Smart Delete, `gp_canvas`).

## Problemas conocidos

- Muchos operadores `HP_*` no tienen `poll()` y asumen objeto activo.
- Imports y variables sin usar en los `HEAVYPOLY_*` (cosmético).
