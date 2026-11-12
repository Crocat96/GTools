"""Photoshop Bridge: render the active camera with compositing passes and open them
in Photoshop as layers of one document.

Passes (PNG, 8 or 16 bit, transparent background), all from ONE render:
  Beauty          the scene's own engine and color management, film transparent
  Clown Object    flat random colour per object    } from the Cryptomatte passes, so
  Clown Material  flat random colour per material  } alpha cut-outs and transparency
                  match the beauty (alpha = beauty alpha); hard edges by default
  Depth           Z, near = white; range automatic (visible objects), manual
                  (near / far) or the camera clipping, plus a gamma
  Alpha           alpha of the beauty render (white = object)

The render is written as a multilayer EXR (Blender 5: every pass is its own part)
read with OpenImageIO, bundled with Blender, and the beauty is saved again as PNG
through `save_render`, so it keeps the scene's view transform. Every render setting
touched is restored afterwards.

Photoshop is driven with a generated ExtendScript (.jsx) passed to Photoshop.exe:
it opens the PNGs, stacks them as layers (Beauty on top, the rest hidden) and saves
a PSD next to them.
"""

import glob
import os
import subprocess
import sys
import tempfile
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from mathutils import Vector

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
    use_clown_object: BoolProperty(
        name="Clown Object", description="Flat random colour per object, for selections", default=True)
    use_clown_material: BoolProperty(
        name="Clown Material", description="Flat random colour per material, for selections", default=False)
    use_depth: BoolProperty(name="Depth", description="Distance to the camera, near = white", default=True)
    use_alpha: BoolProperty(name="Alpha", description="Object mask, white = object", default=True)
    clown_aa: BoolProperty(
        name="Clown Anti-Aliasing",
        description="Blend the colours where objects meet. Off gives one exact colour per pixel, so the "
                    "Magic Wand picks clean selections (the alpha always follows the beauty)",
        default=False,
    )
    depth_range: EnumProperty(
        name="Depth Range",
        items=(
            ('AUTO', "Auto", "From the nearest to the farthest visible point"),
            ('MANUAL', "Manual", "Between the Near and Far distances"),
            ('CAMERA', "Camera Clip", "Between the camera's Clip Start and Clip End"),
        ),
        default='AUTO',
    )
    depth_near: FloatProperty(
        name="Near", description="Distance from the camera that is white (Auto fills it after each render)",
        default=0.1, min=0.0, soft_max=1000.0, subtype='DISTANCE', unit='LENGTH',
    )
    depth_far: FloatProperty(
        name="Far", description="Distance from the camera that is black (Auto fills it after each render)",
        default=100.0, min=0.0, soft_max=1000.0, subtype='DISTANCE', unit='LENGTH',
    )
    depth_gamma: FloatProperty(
        name="Gamma", description="Above 1 keeps more contrast close to the camera, below 1 farther away",
        default=1.0, min=0.05, max=20.0, soft_min=0.2, soft_max=5.0,
    )
    depth_invert: BoolProperty(name="Invert Depth", description="Near = black, far = white", default=False)
    open_photoshop: BoolProperty(
        name="Open in Photoshop",
        description="Stack the passes as layers in Photoshop and save a PSD next to the PNGs",
        default=True,
    )

    @property
    def use_clown(self):
        return self.use_clown_object or self.use_clown_material


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
    snap = []
    snap += _snapshot(r, ("film_transparent", "filepath", "use_single_layer", "use_file_extension"))
    # media_type / file_format first: changing them resets the depth and mode
    snap += _snapshot(r.image_settings, ("media_type", "file_format", "color_mode", "color_depth", "exr_codec",
                                         "compression"))
    snap += _snapshot(view_layer, ("use_pass_z", "use_pass_combined", "use_pass_cryptomatte_object",
                                   "use_pass_cryptomatte_material", "pass_cryptomatte_depth"))
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
    """{channel name: 2D float array, top row first}."""
    import OpenImageIO as oiio
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise RuntimeError("Could not read the render passes: " + oiio.geterror())
    # Blender 5 writes every pass as its own part (subimage) of the EXR
    channels = {}
    sub = 0
    while inp.seek_subimage(sub, 0):
        spec = inp.spec()
        data = inp.read_image(sub, 0, 0, spec.nchannels, oiio.FLOAT)
        if data is not None:
            data = np.asarray(data, dtype=np.float32).reshape(spec.height, spec.width, spec.nchannels)
            for c, n in enumerate(spec.channelnames):
                channels[n] = data[:, :, c]
        sub += 1
    inp.close()
    if not channels:
        raise RuntimeError("The render passes are empty")
    return channels


def _find(channels, view_layer, suffix):
    """Channel of the active view layer ending in `suffix` (case-insensitive)."""
    suffix = suffix.lower()
    prefix = view_layer + "."
    for strict in (True, False):
        for name, data in channels.items():
            if strict and not name.startswith(prefix):
                continue
            if name.lower().endswith(suffix):
                return data
    return None


def _write_png(path, values, depth):
    """values: float array 0..1, (h, w) or (h, w, channels), top row first."""
    import OpenImageIO as oiio
    values = np.clip(np.nan_to_num(values), 0.0, 1.0)
    if values.ndim == 2:
        values = values[:, :, None]
    if depth == '16':
        arr, fmt = (values * 65535.0 + 0.5).astype(np.uint16), oiio.UINT16
    else:
        arr, fmt = (values * 255.0 + 0.5).astype(np.uint8), oiio.UINT8
    h, w, n = arr.shape
    spec = oiio.ImageSpec(w, h, n, fmt)
    if n == 4:
        spec.alpha_channel = 3
        spec.attribute("oiio:UnassociatedAlpha", 1)    # colours are not premultiplied
    out = oiio.ImageOutput.create(path)
    if out is None or not out.open(path, spec):
        raise RuntimeError("Could not write " + path)
    out.write_image(np.ascontiguousarray(arr))
    out.close()


# ---------------------------------------------------------------------------
# Depth

def _fill_holes(z, valid, holes, iterations=12):
    """Give `holes` pixels the mean depth of their valid neighbours, growing inwards.
    EEVEE's dithered transparency leaves background depth in pixels that have alpha."""
    z = np.where(valid, z, 0.0)
    valid = valid.copy()
    for _ in range(iterations):
        todo = holes & ~valid
        if not todo.any():
            break
        acc = np.zeros_like(z)
        cnt = np.zeros_like(z)
        zv = np.where(valid, z, 0.0)
        vv = valid.astype(z.dtype)
        p = np.pad(zv, 1)
        c = np.pad(vv, 1)
        h, w = z.shape
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                acc += p[dy:dy + h, dx:dx + w]
                cnt += c[dy:dy + h, dx:dx + w]
        grow = todo & (cnt > 0)
        z[grow] = acc[grow] / cnt[grow]
        valid |= grow
    return z, valid


def depth_values(z, alpha, mode, near, far, gamma=1.0, invert=False, mixed=None):
    """Depth image 0..1 (near = white) and the (near, far) actually used.
    `mixed`: pixels shared by several surfaces (from Cryptomatte coverage); without it,
    semi-transparent alpha is used."""
    covered = alpha > 1e-4
    valid = covered & np.isfinite(z) & (z < 1e9)
    z, valid = _fill_holes(z, valid, covered)
    # Dithered transparency also mixes front faces, back faces and what is behind from
    # pixel to pixel: semi-transparent pixels take the nearest surface around them.
    partial = valid & ((alpha < 0.99) if mixed is None else (mixed | (alpha < 0.99)))
    if partial.any():
        near_z = np.where(valid, z, np.inf)
        h, w = z.shape
        for _ in range(2):                  # 2 x 3x3 = 5x5 neighbourhood
            p = np.pad(near_z, 1, constant_values=np.inf)
            near_z = np.minimum.reduce([p[dy:dy + h, dx:dx + w] for dy in (0, 1, 2) for dx in (0, 1, 2)])
        z = np.where(partial & np.isfinite(near_z), near_z, z)
    mask = valid
    if mode == 'AUTO':
        if not mask.any():
            return np.zeros_like(z), (near, far)
        near, far = float(z[mask].min()), float(z[mask].max())
    t = np.clip((z - near) / max(far - near, 1e-8), 0.0, 1.0)
    out = 1.0 - t
    if abs(gamma - 1.0) > 1e-6:
        out = out ** gamma
    if invert:
        out = 1.0 - out
    out[~mask] = 0.0
    return out, (near, far)


def selection_depth_range(scene, objects):
    """(near, far) of the objects' bounding boxes along the camera's view axis."""
    cam = scene.camera
    m = cam.matrix_world
    origin = m.translation
    forward = (m.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()
    dists = []
    for ob in objects:
        corners = ob.bound_box if ob.type not in {'EMPTY', 'LIGHT', 'CAMERA'} else [(0.0, 0.0, 0.0)]
        for c in corners:
            dists.append((ob.matrix_world @ Vector(c) - origin).dot(forward))
    if not dists:
        return None
    return max(min(dists), 0.0), max(dists)


# ---------------------------------------------------------------------------
# Clown passes from Cryptomatte

def _id_colors(ids):
    """Bright random colour per Cryptomatte ID (float hash), as (n, 3)."""
    bits = np.ascontiguousarray(ids, dtype=np.float32).view(np.uint32).astype(np.uint64)
    h = (bits * np.uint64(2654435761)) & np.uint64(0xFFFFFFFF)
    h ^= h >> np.uint64(15)
    h = (h * np.uint64(2246822519)) & np.uint64(0xFFFFFFFF)
    h ^= h >> np.uint64(13)
    hue = (h & np.uint64(0xFFFF)).astype(np.float64) / 65536.0
    sat = 0.55 + ((h >> np.uint64(16)) & np.uint64(0xFF)).astype(np.float64) / 255.0 * 0.45
    val = 0.65 + ((h >> np.uint64(24)) & np.uint64(0xFF)).astype(np.float64) / 255.0 * 0.35
    i = np.floor(hue * 6.0).astype(np.int64) % 6
    f = hue * 6.0 - np.floor(hue * 6.0)
    p, q, t = val * (1 - sat), val * (1 - f * sat), val * (1 - (1 - f) * sat)
    r = np.choose(i, [val, q, p, p, t, val])
    g = np.choose(i, [t, val, val, q, p, p])
    b = np.choose(i, [p, p, t, val, val, q])
    return np.stack([r, g, b], axis=1).astype(np.float32)


def _crypto_ranks(channels, view_layer, kind):
    """(ids, coverage) arrays (h, w, ranks); coverage is 0 for ID 0 (nothing)."""
    ranks = []
    for level in range(16):
        r = _find(channels, view_layer, "Crypto%s%02d.r" % (kind, level))
        if r is None:
            break
        ranks.append((r, _find(channels, view_layer, "Crypto%s%02d.g" % (kind, level))))
        ranks.append((_find(channels, view_layer, "Crypto%s%02d.b" % (kind, level)),
                      _find(channels, view_layer, "Crypto%s%02d.a" % (kind, level))))
    ranks = [(i, c) for i, c in ranks if i is not None and c is not None]
    if not ranks:
        return None, None
    ids = np.stack([i for i, _ in ranks], axis=2)
    cov = np.stack([c for _, c in ranks], axis=2)
    cov = np.where(ids == 0.0, 0.0, np.maximum(cov, 0.0))     # ID 0 = nothing / background
    return ids, cov


def mixed_pixels(channels, view_layer):
    """Pixels no single object covers fully (edges, transparency), or None."""
    ids, cov = _crypto_ranks(channels, view_layer, "Object")
    if ids is None:
        return None
    return (cov.sum(axis=2) > 0.0) & (cov.max(axis=2) < 0.99)


def clown_image(channels, view_layer, kind, alpha, antialias):
    """RGBA clown image from the Crypto<kind>NN layers. kind = 'Object' or 'Material'."""
    ids, cov = _crypto_ranks(channels, view_layer, kind)
    if ids is None:
        return None
    uniq, inverse = np.unique(ids, return_inverse=True)
    palette = _id_colors(uniq)
    colors = palette[inverse.reshape(ids.shape)]                # (h, w, ranks, 3)
    if antialias:
        total = cov.sum(axis=2, keepdims=True)
        rgb = (colors * cov[..., None]).sum(axis=2) / np.maximum(total, 1e-8)
    else:
        best = np.argmax(cov, axis=2)                           # strongest ID of the pixel
        rgb = np.take_along_axis(colors, best[:, :, None, None], axis=2)[:, :, 0, :]
    covered = cov.sum(axis=2) > 0.0
    a = np.where(covered, alpha, 0.0)
    rgb[~covered] = 0.0
    return np.concatenate([rgb, a[:, :, None]], axis=2)


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
# Operators

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
        if s.use_clown and context.scene.render.engine == 'BLENDER_WORKBENCH':
            self.report({'ERROR'}, "Clown passes need EEVEE or Cycles (they use Cryptomatte)")
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
        jsx = os.path.join(folder, base + ".jsx")
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
        stem = os.path.basename(os.path.normpath(folder))
        depth = s.color_depth
        layers = []                         # (path, name, visible), top first
        snap = _render_state(scene, view_layer)
        try:
            r.use_single_layer = True       # only the active view layer
            r.film_transparent = True
            view_layer.use_pass_combined = True
            if s.use_depth:
                view_layer.use_pass_z = True
            if s.use_clown_object or s.use_depth:   # depth uses it to find mixed pixels
                view_layer.use_pass_cryptomatte_object = True
            if s.use_clown_material:
                view_layer.use_pass_cryptomatte_material = True
            need_exr = s.use_depth or s.use_alpha or s.use_clown
            if need_exr:
                im = r.image_settings
                if hasattr(im, "media_type"):           # 5.x: multilayer is a media type
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
                channels = _read_exr(exrs[-1])
                for f in exrs:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                vl = view_layer.name
                alpha = _find(channels, vl, "Combined.A")
                if alpha is None:
                    raise RuntimeError("No alpha in the render result")

                for use, kind in ((s.use_clown_object, "Object"), (s.use_clown_material, "Material")):
                    if not use:
                        continue
                    img = clown_image(channels, vl, kind, alpha, s.clown_aa)
                    if img is None:
                        raise RuntimeError("No Cryptomatte %s pass in the render result" % kind.lower())
                    path = os.path.join(folder, "%s_clown_%s.png" % (stem, kind.lower()))
                    _write_png(path, img, depth)
                    layers.append((path, "Clown " + kind, False))

                if s.use_depth:
                    z = _find(channels, vl, "Depth.Z")
                    if z is None:
                        raise RuntimeError("No depth (Z) pass in the render result (%s)" % ", ".join(channels))
                    if s.depth_range == 'CAMERA':
                        cam = scene.camera.data
                        near, far = cam.clip_start, cam.clip_end
                    else:
                        near, far = s.depth_near, s.depth_far
                    values, used = depth_values(z, alpha, s.depth_range, near, far, s.depth_gamma, s.depth_invert,
                                                mixed=mixed_pixels(channels, vl))
                    if s.depth_range == 'AUTO':     # start Manual from what Auto found
                        s.depth_near, s.depth_far = used
                    path = os.path.join(folder, stem + "_depth.png")
                    _write_png(path, values, depth)
                    layers.append((path, "Depth", False))

                if s.use_alpha:
                    path = os.path.join(folder, stem + "_alpha.png")
                    _write_png(path, alpha, depth)
                    layers.append((path, "Alpha", False))
        finally:
            _restore(snap)
        return folder, layers


class GTOOLS_OT_bridge_depth_from_selection(bpy.types.Operator):
    """Set the depth Near / Far to the selected objects (distance along the camera's view)"""
    bl_idname = "render.gtools_bridge_depth_from_selection"
    bl_label = "Depth Range from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.camera is not None and bool(context.selected_objects)

    def execute(self, context):
        rng = selection_depth_range(context.scene, context.selected_objects)
        if rng is None or rng[1] <= 0.0:
            self.report({'WARNING'}, "The selection is behind the camera")
            return {'CANCELLED'}
        s = _settings(context)
        s.depth_range = 'MANUAL'
        s.depth_near, s.depth_far = rng
        return {'FINISHED'}


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
        row.prop(s, "use_alpha", toggle=True)
        row.prop(s, "use_depth", toggle=True)
        row = col.row(align=True)
        row.prop(s, "use_clown_object", text="Clown Object", toggle=True)
        row.prop(s, "use_clown_material", text="Clown Material", toggle=True)

        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(s, "color_depth", expand=True)
        if s.use_clown:
            col.prop(s, "clown_aa")
            if context.scene.render.engine == 'BLENDER_WORKBENCH':
                col.label(text="Clown needs EEVEE or Cycles", icon='ERROR')
        col.prop(s, "output_dir")
        col.prop(s, "open_photoshop")

        if s.use_depth:
            box = layout.box()
            box.label(text="Depth", icon='MOD_THICKNESS')
            bcol = box.column()
            bcol.use_property_split = True
            bcol.use_property_decorate = False
            bcol.prop(s, "depth_range")
            if s.depth_range == 'CAMERA' and context.scene.camera:
                cam = context.scene.camera.data
                sub = bcol.column(align=True)
                sub.prop(cam, "clip_start", text="Near")
                sub.prop(cam, "clip_end", text="Far")
            else:
                sub = bcol.column(align=True)
                sub.active = s.depth_range == 'MANUAL'
                sub.prop(s, "depth_near")
                sub.prop(s, "depth_far")
            bcol.prop(s, "depth_gamma")
            bcol.prop(s, "depth_invert")
            box.operator(GTOOLS_OT_bridge_depth_from_selection.bl_idname, text="Range from Selection",
                         icon='RESTRICT_SELECT_OFF')

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


classes = (GTOOLS_BridgeSettings, GTOOLS_OT_photoshop_bridge, GTOOLS_OT_bridge_depth_from_selection,
           VIEW3D_PT_gtools_bridge)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gtools_bridge = PointerProperty(type=GTOOLS_BridgeSettings)


def unregister():
    del bpy.types.Scene.gtools_bridge
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
