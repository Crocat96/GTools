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

The path is previewed in the viewport: before moving, every edge each vertex could
take; while sliding, the edge it follows (bright), that edge's line extended both
ways (faint, the way past the neighbour or past a border) and where it started (dot).

While sliding:
- Move the mouse: slide. Left click / Enter: confirm. Right click / Esc: cancel.
- E: Even (every vertex moves the same distance) or proportional (default: every
  vertex reaches its neighbour at the same time, like Blender's edge slide).
- Shift: precision. Ctrl: factor in steps of 0.1.
After confirming, the redo panel takes an exact factor.

Push (Face select mode): the selected faces are pushed along their edges. Each vertex
follows its edge that runs most along the faces' normal, as an infinite line, so the
faces ride up or down converging / diverging edges. By default the faces stay flat
and parallel (each vertex goes as far along its edge as the push needs); E switches
to Even (the same distance along every edge). A vertex with no edge near the normal
moves straight along the normal.
"""

import bmesh
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

# A real edge in the mouse's direction is preferred over an edge extended backwards,
# unless the backwards one lines up clearly better.
BACKWARD_WEIGHT = 0.8


# ---------------------------------------------------------------------------
# Rails and sliding (no UI: projection is passed in, so it can be tested)
# ---------------------------------------------------------------------------

class SlideVert:
    __slots__ = ("obj", "bm", "vert", "origin", "rails", "normal")

    def __init__(self, obj, bm, vert, origin, rails, normal=None):
        self.obj = obj
        self.bm = bm
        self.vert = vert
        self.origin = origin  # world space
        self.rails = rails    # world-space vectors to each unselected neighbour
        self.normal = normal  # world space, from the selected faces around it (push)


def collect(objects):
    """Selected, visible vertices of the meshes in Edit Mode, with their rails and the
    normal of the selected faces around them. Returns (verts, bmeshes), bmeshes = {obj: bm}."""
    verts, bmeshes = [], {}
    for obj in objects:
        if obj.type != 'MESH' or obj.mode != 'EDIT':
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bmeshes[obj] = bm
        mw = obj.matrix_world
        normal_matrix = mw.inverted_safe().transposed().to_3x3()
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
            normal = Vector()
            for f in v.link_faces:
                if f.select and not f.hide:
                    normal += (normal_matrix @ f.normal).normalized()
            verts.append(SlideVert(obj, bm, v, origin, rails,
                                   normal.normalized() if normal.length > 1e-9 else None))
    return verts, bmeshes


# ---------------------------------------------------------------------------
# Push (faces): every vertex follows its edge that runs most along the faces' normal
# ---------------------------------------------------------------------------

# An edge leaning more than this from the normal (cos ~76 degrees) isn't a guide: the
# vertex is pushed straight along the normal instead.
PUSH_MIN_COS = 0.25


def push_guide(sv):
    """(unit direction pointing out of the faces, cos to the normal, rail or None) for
    this vertex, or None if it has no selected face around it."""
    n = sv.normal
    if n is None:
        return None
    best = None
    for rail in sv.rails:
        d = rail.normalized()
        c = d.dot(n)
        if best is None or abs(c) > abs(best[1]):
            best = (d, c, rail)
    if best is None or abs(best[1]) < PUSH_MIN_COS:
        return n.copy(), 1.0, None
    d, c, rail = best
    if c < 0:
        d, c = -d, -c
    return d, c, rail


def plan_push(verts, distance, even):
    """[(vertex, unit direction or None, travel along it, rail or None)].
    Flat (default): the faces move `distance` along their normal and stay parallel,
    each vertex going as far along its edge as that takes (edges that spread apart
    make the faces grow, edges that close in make them shrink). Even: every vertex
    moves `distance` along its own edge."""
    out = []
    for sv in verts:
        g = push_guide(sv)
        if g is None:
            out.append((sv, None, 0.0, None))
            continue
        d, c, rail = g
        out.append((sv, d, distance if even else distance / c, rail))
    return out


def apply_push(verts, distance, even):
    for sv, d, travel, _rail in plan_push(verts, distance, even):
        target = sv.origin if d is None else sv.origin + d * travel
        sv.vert.co = sv.obj.matrix_world.inverted() @ target


def push_lines(verts, distance, even):
    """Preview for push: (edges, extensions, origins) like path_lines."""
    edges, extensions, origins = [], [], []
    for sv, d, travel, rail in plan_push(verts, distance, even):
        if d is None:
            continue
        origins.append(sv.origin)
        size = rail.length if rail is not None else max((r.length for r in sv.rails), default=1.0)
        reach = max(size * 1.5, abs(travel) + size * 0.5)
        if rail is not None:
            edges += [sv.origin, sv.origin + rail]
        extensions += [sv.origin - d * reach, sv.origin + d * reach]
    return edges, extensions, origins


_AXES = (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))


def push_from_mouse(ref, project, delta):
    """Push distance (world units, along the normal) for a mouse offset: dragging along
    the normal as seen on screen; if the normal points at the view, dragging up."""
    a = project(ref.origin)
    b = project(ref.origin + ref.normal)
    if a is not None and b is not None and (b - a).length > 5.0:
        s = b - a
        return delta.dot(s) / s.length_squared
    if a is None:
        return 0.0
    ppu = max(((project(ref.origin + ax) or a) - a).length for ax in _AXES) or 100.0
    return delta.y / ppu


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


def nearest(verts, project, point, need_rails=True):
    best = None
    for sv in verts:
        if need_rails and not sv.rails:
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


def plan_moves(verts, project, guide, factor, even, ref=None):
    """[(vertex, rail or None, signed distance along the rail's direction)] for the
    current slide: rail * factor (proportional), or the same world distance as the
    reference's rail * factor (even)."""
    if guide is None:
        return [(sv, None, 0.0) for sv in verts]
    dist = None
    if even:
        picked = choose_rail(ref, project, guide) if ref else None
        if picked is not None:
            dist = picked[0].length * factor
        else:
            lengths = [r.length for sv in verts for r in sv.rails]
            dist = (sum(lengths) / len(lengths) if lengths else 0.0) * factor
    moves = []
    for sv in verts:
        picked = choose_rail(sv, project, guide) if sv.rails else None
        if picked is None:
            moves.append((sv, None, 0.0))
            continue
        rail, sign, _ = picked
        moves.append((sv, rail, sign * (dist if even else rail.length * factor)))
    return moves


def apply(verts, project, guide, factor, even, ref=None):
    """Move every vertex along its rail (see plan_moves)."""
    for sv, rail, distance in plan_moves(verts, project, guide, factor, even, ref):
        target = sv.origin if rail is None else sv.origin + rail.normalized() * distance
        sv.vert.co = sv.obj.matrix_world.inverted() @ target


def path_lines(verts, project, guide, factor, even, ref=None):
    """What the preview draws, in world space: (edges, extensions, origins).
    Before the mouse moves (no guide): every rail each vertex could take.
    While sliding: the rail each vertex follows, plus that edge's line extended both
    ways past where the vertex is going (the path past the neighbour and past borders)."""
    edges, extensions, origins = [], [], []
    for sv, rail, distance in plan_moves(verts, project, guide, factor, even, ref):
        if not sv.rails:
            continue
        origins.append(sv.origin)
        if guide is None:
            for r in sv.rails:
                edges += [sv.origin, sv.origin + r]
            continue
        if rail is None:
            continue
        d = rail.normalized()
        reach = max(rail.length * 1.5, abs(distance) + rail.length * 0.5)
        edges += [sv.origin, sv.origin + rail]
        extensions += [sv.origin - d * reach, sv.origin + d * reach]
    return edges, extensions, origins


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


RAIL_COLOR = (0.3, 0.85, 1.0, 0.95)
LINE_COLOR = (0.3, 0.85, 1.0, 0.35)
ORIGIN_COLOR = (1.0, 0.8, 0.2, 1.0)


def _draw_path(op, context):
    edges, extensions, origins = op.lines
    if not (edges or extensions or origins):
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    line.uniform_float("viewportSize", (context.region.width, context.region.height))
    for segs, width, color in ((extensions, 1.5, LINE_COLOR), (edges, 2.5, RAIL_COLOR)):
        if segs:
            line.uniform_float("lineWidth", width)
            line.uniform_float("color", color)
            batch_for_shader(line, 'LINES', {"pos": [tuple(p) for p in segs]}).draw(line)
    if origins:
        point = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
        gpu.state.point_size_set(7.0)
        point.uniform_float("color", ORIGIN_COLOR)
        batch_for_shader(point, 'POINTS', {"pos": [tuple(p) for p in origins]}).draw(point)
        gpu.state.point_size_set(1.0)
    gpu.state.blend_set('NONE')


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
    push: bpy.props.BoolProperty(
        name="Push Faces", default=False,
        description="Push the selected faces along their edges (Face select mode turns this on)")
    distance: bpy.props.FloatProperty(
        name="Distance", default=0.0, subtype='DISTANCE',
        description="Push: how far the faces move along their normal")
    guide: bpy.props.FloatVectorProperty(
        name="Direction", size=2, default=(1.0, 0.0), options={'HIDDEN'})
    ref_object: bpy.props.StringProperty(options={'HIDDEN'})
    ref_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        if self.push:
            layout.prop(self, "distance")
            layout.prop(self, "even", text="Even (same distance along each edge)")
        else:
            layout.prop(self, "factor")
            layout.prop(self, "even")

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
        if self.push:
            apply_push(verts, self.distance, self.even)
        else:
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
        self.push = tuple(context.tool_settings.mesh_select_mode) == (False, False, True)
        if self.push:
            self.ref = nearest([sv for sv in self.verts if sv.normal is not None], self.project, mouse,
                               need_rails=False)
            self.distance = 0.0
        else:
            self.ref = nearest(self.verts, self.project, mouse)
        if self.ref is None:
            self.report({'WARNING'}, "Nothing to push: select faces" if self.push else
                        "Nothing to slide: select vertices or edges with unselected neighbours")
            return {'CANCELLED'}
        self.ref_object = self.ref.obj.name
        self.ref_index = self.ref.vert.index
        self.prev_mouse = mouse
        self.delta = Vector((0.0, 0.0))
        self.factor = 0.0
        self.current_guide = None
        self._refresh_lines()
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_path, (self, context), 'WINDOW', 'POST_VIEW')
        context.window_manager.modal_handler_add(self)
        self._header(context)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _refresh_lines(self):
        if self.push:
            self.lines = push_lines(self.verts, self.distance, self.even)
        else:
            self.lines = path_lines(self.verts, self.project, self.current_guide, self.factor, self.even, self.ref)

    def _header(self, context):
        if self.push:
            context.area.header_text_set(
                f"Smart Push   Distance: {self.distance:.3f}   "
                f"E: {'Even (same distance along each edge)' if self.even else 'Flat (faces stay parallel)'}   "
                "Shift: precision   Ctrl: steps of 0.1")
            return
        context.area.header_text_set(
            f"Smart Slide   Factor: {self.factor:.3f}   "
            f"E: Even ({'on' if self.even else 'off'})   Shift: precision   Ctrl: steps of 0.1")

    def _update(self, context, event):
        if self.push:
            d = push_from_mouse(self.ref, self.project, self.delta)
            if event.ctrl:
                d = round(d * 10.0) / 10.0
            self.distance = d
            apply_push(self.verts, d, self.even)
            update_meshes(self.bmeshes)
            self._refresh_lines()
            self._header(context)
            context.area.tag_redraw()
            return
        f, guide = factor_from_mouse(self.ref, self.project, self.delta)
        if event.ctrl:
            f = round(f * 10.0) / 10.0
        self.factor = f
        if guide is not None:
            self.current_guide = guide
            self.guide = guide
        apply(self.verts, self.project, self.current_guide, f, self.even, self.ref)
        update_meshes(self.bmeshes)
        self._refresh_lines()
        self._header(context)
        context.area.tag_redraw()

    def _finish(self, context):
        handle = getattr(self, "_draw_handle", None)
        if handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            self._draw_handle = None
        context.area.header_text_set(None)
        context.area.tag_redraw()

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
