# G-Tools

Herramientas de modelado hard-surface y pie menus para Blender 5.0+.

## Instalar en Blender (con actualizaciones automáticas)

1. **Edit → Preferences → Get Extensions**.
2. Abre el menú **Repositories** (arriba a la derecha) → **+** → **Add Remote Repository**.
3. URL: `https://crocat96.github.io/GTools/index.json`
4. Marca **Check for Updates on Startup** y acepta.
5. Busca **G-Tools** en *Get Extensions* e instálalo.

Cuando haya una versión nueva, Blender la mostrará en *Get Extensions* → **Updates**.

> Si tenías instalada la versión antigua como add-on (`scripts/addons/GTools`),
> desactívala y elimínala antes de instalar la extensión para que no choquen.

## Publicar una actualización

1. Sube `version` en [`GTools/blender_manifest.toml`](GTools/blender_manifest.toml)
   (por ejemplo `0.2.0` → `0.2.1`). Sin este cambio Blender no detecta la actualización.
2. Haz commit y push a `main`.
3. El workflow **Publish extension** empaqueta la extensión y actualiza el
   repositorio en GitHub Pages.

## Empaquetar localmente

```bash
blender --command extension build --source-dir GTools --output-dir dist
```
