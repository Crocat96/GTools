"""Smart Slide (Edit Mode, Shift G with the full G-Tools hotkeys): vertex and edge
slide in one, without limits.

Every selected vertex slides along one of its edges that go to an unselected vertex
(its "rails"). A rail is used as an infinite line:

- past the neighbour: keep dragging and the vertex continues along the edge's
  direction, beyond the end of the edge;
- past a boundary: if there is no edge on the side the mouse goes to (a mesh
  border, the end of a strip), the vertex follows the opposite edge, extended
  backwards, so a border loop can slide outwards.

The mouse picks the side, like Blender's slide: the vertex nearest the mouse is the
reference, and every vertex takes the rail that points most along the reference's
direction on screen (a real edge in that direction wins over an extended one).

While sliding:
- Move the mouse: slide. Left click / Enter: confirm. Right click / Esc: cancel.
- E: Even (every vertex moves the same distance) or proportional (default: every
  vertex reaches its neighbour at the same time, like Blender's edge slide).
- Shift: precision. Ctrl: factor in steps of 0.1.
After confirming, the redo panel takes an exact factor.

In Face select mode Shift G keeps doing Push (shrink / fatten), see mesh.push_and_slide.
"""

import bmesh
import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

# A real edge in the mouse's direction is preferred over an edge extended backwards,
# unless the backwards one lines up clearly better.
BACKWARD_WEIGHT = 0.8


# ---------------------------------------------------------------------------
# Rails and sliding (no UI: projection is passed in, so it can be tested)
# ---------------------------------------------------------------------------

class SlideVert:
    __slots__ = ("obj", "bm", "vert", "origin", "rails")

    def __init__(self, obj, bm, vert, origin, rails):
        self.obj = obj
        self.bm = bm
        self.vert = vert
        self.origin = origin  # world space
        self.rails = rails    # world-space vectors to each unselected neighbour


def collect(objects):
    """Selected, visible vertices of the meshes in Edit Mode, with their rails.
    Returns (verts, bmeshes) where bmeshes is {obj: bm}."""
    verts, bmeshes = [], {}
    for obj in objects:
        if obj.type != 'MESH' or obj.mode != 'EDIT':
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bmeshes[obj] = bm
        mw = obj.matrix_world
        for v in bm.verts:
            if not v.select or v.hide:
                continue
            origin = mw @ v.co
            rails = []
            for e in v.link_edges:
                if e.hide:
                    continue
                other = e.other_vert(v)
                if other.select:
                    continue
                vec = mw @ other.co - origin
                if vec.length > 1e-9:
                    rails.append(vec)
            verts.append(SlideVert(obj, bm, v, origin, rails))
    return verts, bmeshes


def _screen_vec(project, origin, rail):
    a = project(origin)
    b = project(origin + rail)
    if a is None or b is None:
        return None
    return b - a


def choose_rail(sv, project, guide):
    """(rail, sign, screen vector of the rail) for the rail most along guide (a unit
    2D vector), or None. sign is -1 when the rail is followed backwards (extended)."""
    best = None
    for rail in sv.rails:
        s = _screen_vec(project, sv.origin, rail)
        if s is None or s.length < 1e-6:
            continue
        d = s.normalized().dot(guide)
        key = d if d >= 0 else -d * BACKWARD_WEIGHT
        if best is None or key > best[0]:
            best = (key, rail, 1.0 if d >= 0 else -1.0, s)
    if best is None:
        return None
    return best[1], best[2], best[3]


def nearest(verts, project, point):
    best = None
    for sv in verts:
        if not sv.rails:
            continue
        p = project(sv.origin)
        if p is None:
            continue
        d = (p - point).length
        if best is None or d < best[0]:
            best = (d, sv)
    return best[1] if best else None


def factor_from_mouse(ref, project, delta):
    """Slide factor of the reference vertex for a mouse offset (2D) and the guide
    direction it gives (the reference rail's on-screen direction)."""
    if delta.length < 1e-6:
        return 0.0, None
    picked = choose_rail(ref, project, delta.normalized())
    if picked is None:
        return 0.0, None
    _, sign, screen = picked
    screen = screen * sign
    return delta.dot(screen) / screen.length_squared, screen.normalized()


def apply(verts, project, guide, factor, even, ref=None):
    """Move every vertex: origin + rail * factor (proportional), or the same world
    distance as the reference's rail * factor (even)."""
    if guide is None:
        factor = 0.0
    dist = None
    if even:
        picked = choose_rail(ref, project, guide) if (ref and guide is not None) else None
        if picked is not None:
            dist = picked[0].length * factor
        else:
            lengths = [r.length for sv in verts for r in sv.rails]
            dist = (sum(lengths) / len(lengths) if lengths else 0.0) * factor
    for sv in verts:
        target = sv.origin
        if factor != 0.0 and sv.rails:
            picked = choose_rail(sv, project, guide)
            if picked is not None:
                rail, sign, _ = picked
                if even:
                    target = sv.origin + rail.normalized() * (sign * dist)
                else:
                    target = sv.origin + rail * (sign * factor)
        sv.vert.co = sv.obj.matrix_world.inverted() @ target


def restore(verts):
    for sv in verts:
        sv.vert.co = sv.obj.matrix_world.inverted() @ sv.origin


def update_meshes(bmeshes):
    for obj in bmeshes:
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

def _projector(region, rv3d):
    def project(co):
        return view3d_utils.location_3d_to_region_2d(region, rv3d, co)
    return project


class GTOOLS_OT_smart_slide(bpy.types.Operator):
    """Slide vertices or edge loops along their edges, past the neighbouring vertex and past mesh borders (edges extended as lines)"""
    bl_idname = "mesh.gtools_smart_slide"
    bl_label = "Smart Slide"
    bl_options = {'REGISTER', 'UNDO'}

    factor: bpy.props.FloatProperty(
        name="Factor", default=0.0, soft_min=-2.0, soft_max=2.0,
        description="How far to slide: 1 reaches the neighbouring vertex, more goes past it")
    even: bpy.props.BoolProperty(
        name="Even", default=False,
        description="Every vertex moves the same distance (off: each reaches its neighbour at the same time)")
    guide: bpy.props.FloatVectorProperty(
        name="Direction", size=2, default=(1.0, 0.0), options={'HIDDEN'})
    ref_object: bpy.props.StringProperty(options={'HIDDEN'})
    ref_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH' and context.area is not None
                and context.area.type == 'VIEW_3D')

    # -- redo / scripting ---------------------------------------------------

    def _ref(self, verts):
        for sv in verts:
            if sv.obj.name == self.ref_object and sv.vert.index == self.ref_index:
                return sv
        return None

    def execute(self, context):
        project = _projector(context.region, context.region_data)
        verts, bmeshes = collect(context.objects_in_mode_unique_data)
        if not verts:
            return {'CANCELLED'}
        guide = Vector(self.guide)
        guide = guide.normalized() if guide.length > 1e-6 else None
        apply(verts, project, guide, self.factor, self.even, self._ref(verts))
        update_meshes(bmeshes)
        return {'FINISHED'}

    # -- interactive ----------------------------------------------------------

    def invoke(self, context, event):
        self.project = _projector(context.region, context.region_data)
        self.verts, self.bmeshes = collect(context.objects_in_mode_unique_data)
        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        self.ref = nearest(self.verts, self.project, mouse)
        if self.ref is None:
            self.report({'WARNING'}, "Nothing to slide: select vertices or edges with unselected neighbours")
            return {'CANCELLED'}
        self.ref_object = self.ref.obj.name
        self.ref_index = self.ref.vert.index
        self.prev_mouse = mouse
        self.delta = Vector((0.0, 0.0))
        self.factor = 0.0
        self.current_guide = None
        context.window_manager.modal_handler_add(self)
        self._header(context)
        return {'RUNNING_MODAL'}

    def _header(self, context):
        context.area.header_text_set(
            f"Smart Slide   Factor: {self.factor:.3f}   "
            f"E: Even ({'on' if self.even else 'off'})   Shift: precision   Ctrl: steps of 0.1")

    def _update(self, context, event):
        f, guide = factor_from_mouse(self.ref, self.project, self.delta)
        if event.ctrl:
            f = round(f * 10.0) / 10.0
        self.factor = f
        if guide is not None:
            self.current_guide = guide
            self.guide = guide
        apply(self.verts, self.project, self.current_guide, f, self.even, self.ref)
        update_meshes(self.bmeshes)
        self._header(context)

    def _finish(self, context):
        context.area.header_text_set(None)

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            mouse = Vector((event.mouse_region_x, event.mouse_region_y))
            step = mouse - self.prev_mouse
            self.prev_mouse = mouse
            self.delta += step * (0.1 if event.shift else 1.0)
            self._update(context, event)
        elif event.type == 'E' and event.value == 'PRESS':
            self.even = not self.even
            self._update(context, event)
        elif event.type in {'LEFT_CTRL', 'RIGHT_CTRL'} and event.value in {'PRESS', 'RELEASE'}:
            self._update(context, event)
        elif event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._finish(context)
            return {'FINISHED'}
        elif event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            restore(self.verts)
            update_meshes(self.bmeshes)
            self._finish(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


classes = (GTOOLS_OT_smart_slide,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
