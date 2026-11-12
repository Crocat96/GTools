"""Photoshop Bridge: render the active camera with compositing passes and open them
in Photoshop as layers of one document.

Passes (PNG, 8 or 16 bit, transparent background):
  Beauty  the scene's own engine and color management, film transparent
  Clown   Workbench, flat random colour per object (or per material), no AA by default
  Depth   Z of the beauty render, normalised to the visible objects (near = white)
  Alpha   alpha of the beauty render (white = object)

The beauty render is written once as a multilayer EXR (Z + alpha are read from it
with OpenImageIO, bundled with Blender) and once as PNG through `save_render`, so
the PNG keeps the scene's view transform. The clown pass is a second, fast
Workbench render. Every render setting touched is restored afterwards.

Photoshop is driven with a generated ExtendScript (.jsx) passed to Photoshop.exe:
it opens the PNGs, stacks them as layers (Beauty on top, the rest hidden) and saves
a PSD next to them.
"""

import colorsys
import glob
import hashlib
import os
import subprocess
import sys
import tempfile
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty

ADDON_ID = __package__


# ---------------------------------------------------------------------------
# Settings

class GTOOLS_BridgeSettings(bpy.types.PropertyGroup):
    output_dir: StringProperty(
        name="Folder",
        description="Where the passes are saved (a subfolder is made for every export). "
                    "Relative paths need a saved .blend; otherwise the temp folder is used",
        default="//Bridge/", subtype='DIR_PATH',
    )
    color_depth: EnumProperty(
        name="Bit Depth",
        items=(('8', "8 bit", "8 bits per channel"), ('16', "16 bit", "16 bits per channel")),
        default='16',
    )
    use_beauty: BoolProperty(name="Beauty", description="Final render with transparent background", default=True)
    use_clown: BoolProperty(name="Clown", description="Flat random colour per object or material, for selections", default=True)
    use_depth: BoolProperty(name="Depth", description="Distance to the camera, near = white", default=True)
    use_alpha: BoolProperty(name="Alpha", description="Object mask, white = object", default=True)
    clown_mode: EnumProperty(
        name="Clown Colors",
        items=(
            ('OBJECT', "Per Object", "A random colour for every object"),
            ('MATERIAL', "Per Material", "A random colour for every material"),
        ),
        default='OBJECT',
    )
    clown_aa: BoolProperty(
        name="Clown Anti-Aliasing",
        description="Smooth clown edges. Off gives hard edges, so the Magic Wand picks exact colours",
        default=False,
    )
    depth_invert: BoolProperty(name="Invert Depth", description="Near = black, far = white", default=False)
    open_photoshop: BoolProperty(
        name="Open in Photoshop",
        description="Stack the passes as layers in Photoshop and save a PSD next to the PNGs",
        default=True,
    )


def _settings(context):
    return context.scene.gtools_bridge


# ---------------------------------------------------------------------------
# Saving and restoring render settings

def _snapshot(obj, names):
    return [(obj, n, getattr(obj, n)) for n in names if hasattr(obj, n)]


def _restore(snap):
    for obj, name, value in snap:
        try:
            setattr(obj, name, value)
        except Exception:
            pass


def _render_state(scene, view_layer):
    r = scene.render
    shading = scene.display.shading
    snap = []
    snap += _snapshot(r, ("engine", "film_transparent", "filepath", "use_compositing", "use_sequencer",
                          "use_single_layer", "use_file_extension", "dither_intensity"))
    # media_type / file_format first: changing them resets the depth and mode
    snap += _snapshot(r.image_settings, ("media_type", "file_format", "color_mode", "color_depth", "exr_codec",
                                         "compression"))
    snap += _snapshot(view_layer, ("use_pass_z", "use_pass_combined"))
    snap += _snapshot(scene.display, ("render_aa",))
    snap += _snapshot(shading, ("light", "color_type", "show_shadows", "show_cavity", "show_object_outline",
                                "show_specular_highlight", "use_dof", "show_xray", "show_backface_culling",
                                "use_world_space_lighting"))
    snap += _snapshot(scene.view_settings, ("view_transform", "look", "exposure", "gamma", "use_curve_mapping"))
    return snap


def _set_png(scene, depth, rgba=True):
    im = scene.render.image_settings
    if hasattr(im, "media_type"):
        im.media_type = 'IMAGE'
    im.file_format = 'PNG'
    im.color_mode = 'RGBA' if rgba else 'BW'
    im.color_depth = depth
    im.compression = 15


# ---------------------------------------------------------------------------
# Image helpers (OpenImageIO)

def _read_exr(path):
    import OpenImageIO as oiio
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise RuntimeError("Could not read the render passes: " + oiio.geterror())
    # Blender 5 writes every pass as its own part (subimage) of the EXR
    names, planes = [], []
    sub = 0
    while inp.seek_subimage(sub, 0):
        spec = inp.spec()
        data = inp.read_image(sub, 0, 0, spec.nchannels, oiio.FLOAT)
        if data is not None:
            data = np.asarray(data, dtype=np.float32).reshape(spec.height, spec.width, spec.nchannels)
            for c, n in enumerate(spec.channelnames):
                names.append(n)
                planes.append(data[:, :, c])
        sub += 1
    inp.close()
    if not planes:
        raise RuntimeError("The render passes are empty")
    return names, np.stack(planes, axis=2)


def _channel(names, data, view_layer, want):
    """`want` = 'A' (combined alpha) or 'Z' (depth)."""
    prefix = view_layer + "."
    for strict in (True, False):
        for i, n in enumerate(names):
            if strict and not n.startswith(prefix):
                continue
            if want == 'A' and n.endswith("Combined.A"):
                return data[:, :, i]
            if want == 'Z' and (n.endswith("Depth.Z") or n.endswith(".Z")):
                return data[:, :, i]
    return None


def _write_gray(path, values, depth):
    """values: float array 0..1, top row first."""
    import OpenImageIO as oiio
    values = np.clip(np.nan_to_num(values), 0.0, 1.0)
    if depth == '16':
        arr, fmt = (values * 65535.0 + 0.5).astype(np.uint16), oiio.UINT16
    else:
        arr, fmt = (values * 255.0 + 0.5).astype(np.uint8), oiio.UINT8
    h, w = arr.shape
    spec = oiio.ImageSpec(w, h, 1, fmt)
    out = oiio.ImageOutput.create(path)
    if out is None or not out.open(path, spec):
        raise RuntimeError("Could not write " + path)
    out.write_image(arr.reshape(h, w, 1))
    out.close()


def _depth_values(z, alpha, invert):
    mask = (alpha > 1e-4) & np.isfinite(z) & (z < 1e9)
    out = np.zeros_like(z)
    if not mask.any():
        return out
    near, far = float(z[mask].min()), float(z[mask].max())
    span = max(far - near, 1e-8)
    t = (z - near) / span
    out = t if invert else 1.0 - t
    out[~mask] = 0.0
    return out


# ---------------------------------------------------------------------------
# Clown colours per material

def _material_color(name):
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)
    hue = (h % 1000) / 1000.0
    sat = 0.55 + ((h >> 10) % 45) / 100.0
    val = 0.6 + ((h >> 17) % 40) / 100.0
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return (r, g, b, 1.0)


# ---------------------------------------------------------------------------
# Photoshop

def find_photoshop(context):
    prefs = context.preferences.addons.get(ADDON_ID)
    custom = getattr(prefs.preferences, "photoshop_path", "") if prefs else ""
    if custom:
        path = bpy.path.abspath(custom)
        if os.path.isdir(path):
            path = os.path.join(path, "Photoshop.exe")
        return path if os.path.isfile(path) else None
    if sys.platform != "win32":
        return None
    found = []
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                root = winreg.OpenKey(hive, r"SOFTWARE\Adobe\Photoshop")
            except OSError:
                continue
            i = 0
            while True:
                try:
                    ver = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, ver) as k:
                        app = winreg.QueryValueEx(k, "ApplicationPath")[0]
                    exe = os.path.join(app, "Photoshop.exe")
                    if os.path.isfile(exe):
                        found.append((float(ver) if ver.replace(".", "", 1).isdigit() else 0.0, exe))
                except OSError:
                    pass
    except ImportError:
        pass
    if not found:
        for exe in glob.glob(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                                          "Adobe", "Adobe Photoshop*", "Photoshop.exe")):
            found.append((0.0, exe))
        found.sort(key=lambda f: f[1])
    if not found:
        return None
    found.sort(key=lambda f: f[0])
    return found[-1][1]


def _js_str(path):
    return '"' + path.replace("\\", "/").replace('"', '\\"') + '"'


def write_jsx(path, layers, psd_path):
    """layers: [(png path, layer name, visible)], top layer first."""
    order = list(reversed(layers))          # open the bottom one as the document
    items = ",\n    ".join("[%s, %s, %s]" % (_js_str(p), _js_str(n), "true" if v else "false")
                           for p, n, v in order)
    script = """#target photoshop
// Generated by G-Tools Photoshop Bridge
(function () {
  var old = app.displayDialogs;
  app.displayDialogs = DialogModes.NO;
  var layers = [
    %(items)s
  ];
  try {
    var doc = open(new File(layers[0][0]));
    if (doc.mode != DocumentMode.RGB) doc.changeMode(ChangeMode.RGB);
    var base = doc.activeLayer;
    if (base.isBackgroundLayer) base.isBackgroundLayer = false;
    base.name = layers[0][1];
    base.visible = layers[0][2];
    for (var i = 1; i < layers.length; i++) {
      var src = open(new File(layers[i][0]));
      src.activeLayer.duplicate(doc, ElementPlacement.PLACEATBEGINNING);
      src.close(SaveOptions.DONOTSAVECHANGES);
      app.activeDocument = doc;
      var lay = doc.layers[0];
      lay.name = layers[i][1];
      lay.visible = layers[i][2];
    }
    doc.activeLayer = doc.layers[0];
    var opts = new PhotoshopSaveOptions();
    opts.layers = true;
    opts.embedColorProfile = true;
    doc.saveAs(new File(%(psd)s), opts, false, Extension.LOWERCASE);
    app.bringToFront();
  } catch (e) {
    alert("G-Tools Photoshop Bridge:\\n" + e);
  }
  app.displayDialogs = old;
})();
""" % {"items": items, "psd": _js_str(psd_path)}
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)


# ---------------------------------------------------------------------------
# Operator

def _safe(name):
    keep = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return keep.strip("_") or "untitled"


def _export_folder(context, settings):
    base = settings.output_dir or "//Bridge/"
    if base.startswith("//") and not bpy.data.filepath:
        base = os.path.join(tempfile.gettempdir(), "GTools_Bridge")
    base = bpy.path.abspath(base)
    blend = bpy.path.display_name_from_filepath(bpy.data.filepath) or "untitled"
    cam = context.scene.camera.name
    folder = os.path.join(base, "%s_%s_%s" % (_safe(blend), _safe(cam), time.strftime("%Y%m%d_%H%M%S")))
    os.makedirs(folder, exist_ok=True)
    return folder


class GTOOLS_OT_photoshop_bridge(bpy.types.Operator):
    """Render the active camera with Beauty, Clown, Depth and Alpha passes (PNG) and stack
    them as layers in Photoshop"""
    bl_idname = "render.gtools_photoshop_bridge"
    bl_label = "Render to Photoshop"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if context.scene.camera is None:
            cls.poll_message_set("The scene has no active camera")
            return False
        return True

    def execute(self, context):
        s = _settings(context)
        if not (s.use_beauty or s.use_clown or s.use_depth or s.use_alpha):
            self.report({'WARNING'}, "No pass selected")
            return {'CANCELLED'}
        if context.window:
            context.window.cursor_set('WAIT')
        try:
            folder, layers = self.render_passes(context, s)
        except Exception as e:
            self.report({'ERROR'}, "Photoshop Bridge: %s" % e)
            return {'CANCELLED'}
        finally:
            if context.window:
                context.window.cursor_set('DEFAULT')

        if not s.open_photoshop:
            self.report({'INFO'}, "Passes saved in " + folder)
            return {'FINISHED'}
        ps = find_photoshop(context)
        if ps is None:
            self.report({'WARNING'}, "Photoshop not found (set its path in the G-Tools preferences). "
                                     "Passes saved in " + folder)
            self._open_folder(folder)
            return {'FINISHED'}
        base = os.path.basename(os.path.normpath(folder))
        jsx =os.path.join(folder, base + ".jsx")
        write_jsx(jsx, layers, os.path.join(folder, base + ".psd"))
        try:
            subprocess.Popen([ps, jsx])
        except OSError as e:
            self.report({'ERROR'}, "Could not start Photoshop: %s" % e)
            return {'CANCELLED'}
        self.report({'INFO'}, "Sent %d layers to Photoshop (%s)" % (len(layers), folder))
        return {'FINISHED'}

    @staticmethod
    def _open_folder(folder):
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:
            pass

    def render_passes(self, context, s):
        scene = context.scene
        view_layer = context.view_layer
        r = scene.render
        folder = _export_folder(context, s)
        stem = os.path.basename(folder)
        depth = s.color_depth
        layers = []                         # (path, name, visible), top first
        snap = _render_state(scene, view_layer)
        materials = []
        try:
            r.use_single_layer = True       # only the active view layer
            r.film_transparent = True

            if s.use_beauty or s.use_depth or s.use_alpha:
                view_layer.use_pass_combined = True
                if s.use_depth:
                    view_layer.use_pass_z = True
                need_exr = s.use_depth or s.use_alpha
                if need_exr:
                    im = r.image_settings
                    if hasattr(im, "media_type"):       # 5.x: multilayer is a media type
                        im.media_type = 'MULTI_LAYER_IMAGE'
                    else:
                        im.file_format = 'OPEN_EXR_MULTILAYER'
                    im.color_depth = '32'
                    r.use_file_extension = True
                    r.filepath = os.path.join(folder, "_passes_")
                bpy.ops.render.render(write_still=need_exr)
                result = bpy.data.images.get("Render Result")
                if result is None:
                    raise RuntimeError("The render did not finish")

                if s.use_beauty:
                    _set_png(scene, depth)
                    path = os.path.join(folder, stem + "_beauty.png")
                    result.save_render(filepath=path, scene=scene)
                    layers.append((path, "Beauty", True))

                if need_exr:
                    exrs = sorted(glob.glob(os.path.join(folder, "_passes_*.exr")))
                    if not exrs:
                        raise RuntimeError("The render passes were not written")
                    names, data = _read_exr(exrs[-1])
                    for f in exrs:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                    alpha = _channel(names, data, view_layer.name, 'A')
                    if alpha is None:
                        raise RuntimeError("No alpha in the render result")
                    if s.use_depth:
                        z = _channel(names, data, view_layer.name, 'Z')
                        if z is None:
                            raise RuntimeError("No depth (Z) pass in the render result (%s)" % ", ".join(names))
                        path = os.path.join(folder, stem + "_depth.png")
                        _write_gray(path, _depth_values(z, alpha, s.depth_invert), depth)
                        layers.append((path, "Depth", False))
                    if s.use_alpha:
                        path = os.path.join(folder, stem + "_alpha.png")
                        _write_gray(path, alpha, depth)
                        layers.append((path, "Alpha", False))

            if s.use_clown:
                r.engine = 'BLENDER_WORKBENCH'
                r.use_compositing = False
                r.use_sequencer = False
                r.dither_intensity = 0.0    # exact flat colours
                sh = scene.display.shading
                sh.light = 'FLAT'
                if s.clown_mode == 'MATERIAL':
                    sh.color_type = 'MATERIAL'
                    for mat in bpy.data.materials:
                        materials.append((mat, tuple(mat.diffuse_color)))
                        mat.diffuse_color = _material_color(mat.name)
                else:
                    sh.color_type = 'RANDOM'
                for name in ("show_shadows", "show_cavity", "show_object_outline", "show_specular_highlight",
                             "use_dof", "show_xray"):
                    if hasattr(sh, name):
                        setattr(sh, name, False)
                scene.display.render_aa = '8' if s.clown_aa else 'OFF'
                vs = scene.view_settings
                vs.view_transform = 'Standard'
                vs.look = 'None'
                vs.exposure = 0.0
                vs.gamma = 1.0
                vs.use_curve_mapping = False
                bpy.ops.render.render(write_still=False)
                result = bpy.data.images.get("Render Result")
                _set_png(scene, depth)
                path = os.path.join(folder, stem + "_clown.png")
                result.save_render(filepath=path, scene=scene)
                # just under the beauty
                layers.insert(1 if s.use_beauty else 0, (path, "Clown", False))
        finally:
            for mat, color in materials:
                mat.diffuse_color = color
            _restore(snap)
        return folder, layers


# ---------------------------------------------------------------------------
# Panel

class VIEW3D_PT_gtools_bridge(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "G-Tools"
    bl_label = "Photoshop Bridge"
    bl_order = 10

    def draw(self, context):
        s = _settings(context)
        layout = self.layout

        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(s, "use_beauty", toggle=True)
        row.prop(s, "use_clown", toggle=True)
        row = col.row(align=True)
        row.prop(s, "use_depth", toggle=True)
        row.prop(s, "use_alpha", toggle=True)

        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(s, "color_depth", expand=True)
        if s.use_clown:
            col.prop(s, "clown_mode")
            col.prop(s, "clown_aa")
        if s.use_depth:
            col.prop(s, "depth_invert")
        col.prop(s, "output_dir")
        col.prop(s, "open_photoshop")

        cam = context.scene.camera
        row = layout.row()
        row.scale_y = 1.5
        row.operator(GTOOLS_OT_photoshop_bridge.bl_idname, icon='RENDER_STILL')
        if cam is None:
            layout.label(text="No active camera", icon='ERROR')
        else:
            r = context.scene.render
            pct = r.resolution_percentage / 100.0
            layout.label(text="%s  ·  %d × %d" % (cam.name, int(r.resolution_x * pct), int(r.resolution_y * pct)),
                         icon='CAMERA_DATA')


classes = (GTOOLS_BridgeSettings, GTOOLS_OT_photoshop_bridge, VIEW3D_PT_gtools_bridge)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gtools_bridge = PointerProperty(type=GTOOLS_BridgeSettings)


def unregister():
    del bpy.types.Scene.gtools_bridge
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
