"""Ornament (Shift A > Ornament, Object Mode): adds a curve object with a Geometry
Nodes modifier (node group GT_Ornament_v2) that sweeps a moulding profile along it:
Crown (ogee and bead), Bead (half round), Baroque (tori, scotia and cyma).

Edit the curve like any curve. If a mesh was selected when adding it, that mesh is
the modifier's Target: the curve sticks to its surface (wherever its points are
moved) with the profile standing on it. Without a target the profile just follows
the curve. Width, height, profile, resolution and target are in the modifier. The
profiles are small curve objects (GT_Profile_*) in the GT_Ornament_Profiles
collection: edit them to change every ornament using them.

(object.gtools_ornament is still there as a draw-on-the-surface tool, not in a menu.)
"""

import math

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

NODE_GROUP = "GT_Ornament_v4"  # v4: smooth pass after projecting, preview of the drawn curve
MODIFIER = "Ornament"
PROFILE_COLLECTION = "GT_Ornament_Profiles"


# ---------------------------------------------------------------------------
# Profiles: (height, across) points, across -0.5..0.5, height 0..1, symmetric
# ---------------------------------------------------------------------------

def _arc(center, radius, a0, a1, steps):
    """Arc in profile space (height, across). Angle 0 points outwards (-across),
    pi/2 up (+height), pi inwards (+across)."""
    return [(center[0] + radius * math.sin(a0 + (a1 - a0) * i / steps),
             center[1] - radius * math.cos(a0 + (a1 - a0) * i / steps)) for i in range(steps + 1)]


def _cyma(start, end, steps=10):
    """An S-curve (ogee) from start to end."""
    (h0, a0), (h1, a1) = start, end
    return [(h0 + (h1 - h0) * i / steps, a0 + (a1 - a0) * (0.5 - 0.5 * math.cos(math.pi * i / steps)))
            for i in range(steps + 1)]


def _half_crown():
    # base fillet, ogee up and in, a small fillet, then the top bead
    pts = [(0.0, -0.5), (0.12, -0.5), (0.12, -0.46)]
    pts += _cyma((0.12, -0.46), (0.62, -0.24))[1:]
    pts += [(0.66, -0.24), (0.66, -0.2)]
    pts += _arc((0.66, 0.0), 0.2, 0.0, math.pi / 2, 8)[1:]
    return pts


def _half_bead():
    pts = [(0.0, -0.5), (0.08, -0.5), (0.08, -0.42)]
    pts += _arc((0.08, 0.0), 0.42, 0.0, math.pi / 2, 12)[1:]
    return pts


def _half_baroque():
    pts = _arc((0.1, -0.5), 0.1, -math.pi / 2, math.pi / 2, 8)          # base torus (bulging out)
    pts += [(0.2, -0.44)]
    pts += _arc((0.28, -0.44), 0.08, 3 * math.pi / 2, math.pi / 2, 8)[1:]  # scotia (cove, curving in)
    pts += [(0.36, -0.4)]
    pts += _arc((0.41, -0.4), 0.05, -math.pi / 2, math.pi / 2, 6)[1:]   # small bead
    pts += _cyma((0.46, -0.38), (0.76, -0.18))                          # cyma
    pts += _arc((0.76, 0.0), 0.18, 0.0, math.pi / 2, 8)[1:]             # top bead
    return pts


PROFILES = {
    'CROWN': ("Crown", _half_crown),
    'BEAD': ("Bead", _half_bead),
    'BAROQUE': ("Baroque", _half_baroque),
}


def profile_object(key):
    """The (shared) profile curve object for a profile type, made if missing."""
    label, build = PROFILES[key]
    name = f"GT_Profile_{label}"
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.type == 'CURVE':
        return obj
    half = build()
    full = half + [(h, -a) for h, a in reversed(half[:-1])]  # mirror across the middle
    data = bpy.data.curves.new(name, 'CURVE')
    data.dimensions = '3D'
    spline = data.splines.new('POLY')
    spline.points.add(len(full) - 1)
    for p, (h, a) in zip(spline.points, full):
        p.co = (h, a, 0.0, 1.0)  # profile X = height (along the surface normal), Y = across
    obj = bpy.data.objects.new(name, data)
    col = bpy.data.collections.get(PROFILE_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(PROFILE_COLLECTION)
        bpy.context.scene.collection.children.link(col)
    col.objects.link(obj)
    obj.hide_render = True
    obj.hide_set(True)
    return obj


# ---------------------------------------------------------------------------
# Node group
# ---------------------------------------------------------------------------

def _build_node_group():
    ng = bpy.data.node_groups.new(NODE_GROUP, 'GeometryNodeTree')
    if hasattr(ng, "is_modifier"):
        ng.is_modifier = True
    face = ng.interface
    face.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    face.new_socket("Target", in_out='INPUT', socket_type='NodeSocketObject')
    face.new_socket("Profile", in_out='INPUT', socket_type='NodeSocketMenu')
    face.new_socket("Custom Profile", in_out='INPUT', socket_type='NodeSocketObject')
    s = face.new_socket("Width", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 0.1, 0.0
    s = face.new_socket("Height", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 0.05, 0.0
    s = face.new_socket("Resolution", in_out='INPUT', socket_type='NodeSocketInt')
    s.default_value, s.min_value, s.max_value = 3, 1, 16
    s = face.new_socket("Offset", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 0.0, -1.0
    s = face.new_socket("Smooth", in_out='INPUT', socket_type='NodeSocketInt')
    s.default_value, s.min_value, s.max_value = 4, 0, 50
    s = face.new_socket("Show Curve", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = True
    face.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    N, L = ng.nodes, ng.links
    gin = N.new('NodeGroupInput')
    gout = N.new('NodeGroupOutput')

    def node(kind, **props):
        n = N.new(kind)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    target = node('GeometryNodeObjectInfo', transform_space='RELATIVE')
    L.new(gin.outputs["Target"], target.inputs["Object"])
    # Profile: a menu of the built-in ones, or Custom (any curve object you pick:
    # X = height above the surface, Y = across, like the GT_Profile_* curves).
    choose = node('GeometryNodeMenuSwitch', data_type='GEOMETRY')
    choose.enum_items.clear()
    for key, (label, _build) in PROFILES.items():
        choose.enum_items.new(label)
    choose.enum_items.new("Custom")
    for i, key in enumerate(PROFILES):
        info = node('GeometryNodeObjectInfo', transform_space='ORIGINAL')
        info.inputs["Object"].default_value = profile_object(key)
        L.new(info.outputs["Geometry"], choose.inputs[i + 1])
    custom = node('GeometryNodeObjectInfo', transform_space='ORIGINAL')
    L.new(gin.outputs["Custom Profile"], custom.inputs["Object"])
    L.new(custom.outputs["Geometry"], choose.inputs[len(PROFILES) + 1])
    L.new(gin.outputs["Profile"], choose.inputs["Menu"])
    position = node('GeometryNodeInputPosition')
    normal = node('GeometryNodeInputNormal')

    def sample(value):
        s = node('GeometryNodeSampleNearestSurface', data_type='FLOAT_VECTOR')
        L.new(target.outputs["Geometry"], s.inputs["Mesh"])
        L.new(value, s.inputs["Value"])
        L.new(position.outputs["Position"], s.inputs["Sample Position"])
        return s.outputs["Value"]

    # Smooth, evenly spaced points along the drawn line (Resolution per width).
    resample = node('GeometryNodeResampleCurve')
    L.new(gin.outputs["Geometry"], resample.inputs["Curve"])
    resample.inputs["Mode"].default_value = 'Length'
    step = node('ShaderNodeMath', operation='DIVIDE')
    L.new(gin.outputs["Width"], step.inputs[0])
    L.new(gin.outputs["Resolution"], step.inputs[1])
    L.new(step.outputs[0], resample.inputs["Length"])

    # On the surface (wherever the points were moved to), facing out of it.
    def snapped(geometry):
        up = node('ShaderNodeVectorMath', operation='NORMALIZE')
        L.new(sample(normal.outputs["Normal"]), up.inputs[0])
        lift = node('ShaderNodeVectorMath', operation='SCALE')
        L.new(up.outputs["Vector"], lift.inputs[0])
        L.new(gin.outputs["Offset"], lift.inputs["Scale"])
        onto = node('ShaderNodeVectorMath', operation='ADD')
        L.new(sample(position.outputs["Position"]), onto.inputs[0])
        L.new(lift.outputs["Vector"], onto.inputs[1])
        n = node('GeometryNodeSetPosition')
        L.new(geometry, n.inputs["Geometry"])
        L.new(onto.outputs["Vector"], n.inputs["Position"])
        return n.outputs["Geometry"], up.outputs["Vector"]

    first, _up = snapped(resample.outputs["Curve"])

    # Smooth pass: projecting can make the line wobble (facets, bumps). Blur the
    # points along the curve (the two ends stay put). Smooth = how many passes.
    blur = node('GeometryNodeBlurAttribute', data_type='FLOAT_VECTOR')
    L.new(position.outputs["Position"], blur.inputs["Value"])
    L.new(gin.outputs["Smooth"], blur.inputs["Iterations"])
    ends = node('GeometryNodeCurveEndpointSelection')
    ends.inputs["Start Size"].default_value = 1
    ends.inputs["End Size"].default_value = 1
    keep_end = node('GeometryNodeSwitch', input_type='VECTOR')
    L.new(ends.outputs["Selection"], keep_end.inputs["Switch"])
    L.new(blur.outputs["Value"], keep_end.inputs["False"])
    L.new(position.outputs["Position"], keep_end.inputs["True"])
    smoothed = node('GeometryNodeSetPosition')
    L.new(first, smoothed.inputs["Geometry"])
    L.new(keep_end.outputs[0], smoothed.inputs["Position"])
    # Don't press it back onto every facet (that would bring the wobble back): only
    # points the smoothing pulled under the surface are pushed back out to it.
    # The surface normal smoothed along the curve too, so the profile doesn't tilt
    # from facet to facet.
    up_blur = node('GeometryNodeBlurAttribute', data_type='FLOAT_VECTOR')
    L.new(sample(normal.outputs["Normal"]), up_blur.inputs["Value"])
    L.new(gin.outputs["Smooth"], up_blur.inputs["Iterations"])
    up_raw = node('ShaderNodeVectorMath', operation='NORMALIZE')
    L.new(up_blur.outputs["Value"], up_raw.inputs[0])
    up = up_raw.outputs["Vector"]
    under = node('ShaderNodeVectorMath', operation='SUBTRACT')
    L.new(position.outputs["Position"], under.inputs[0])
    L.new(sample(position.outputs["Position"]), under.inputs[1])
    above = node('ShaderNodeVectorMath', operation='DOT_PRODUCT')
    L.new(under.outputs["Vector"], above.inputs[0])
    L.new(up, above.inputs[1])
    missing = node('ShaderNodeMath', operation='SUBTRACT')
    L.new(gin.outputs["Offset"], missing.inputs[0])
    L.new(above.outputs["Value"], missing.inputs[1])
    push = node('ShaderNodeMath', operation='MAXIMUM')
    L.new(missing.outputs[0], push.inputs[0])
    push.inputs[1].default_value = 0.0
    out = node('ShaderNodeVectorMath', operation='SCALE')
    L.new(up, out.inputs[0])
    L.new(push.outputs[0], out.inputs["Scale"])
    pushed = node('GeometryNodeSetPosition')
    L.new(smoothed.outputs["Geometry"], pushed.inputs["Geometry"])
    L.new(out.outputs["Vector"], pushed.inputs["Offset"])
    snap_geo = pushed.outputs["Geometry"]

    facing = node('GeometryNodeSetCurveNormal')
    L.new(snap_geo, facing.inputs["Curve"])
    facing.inputs["Mode"].default_value = 'Free'
    L.new(up, facing.inputs["Normal"])

    # The profile, sized: X = height, Y = across.
    size = node('ShaderNodeCombineXYZ')
    L.new(gin.outputs["Height"], size.inputs[0])
    L.new(gin.outputs["Width"], size.inputs[1])
    size.inputs[2].default_value = 1.0
    sized = node('GeometryNodeTransform')
    L.new(choose.outputs[0], sized.inputs["Geometry"])
    L.new(size.outputs[0], sized.inputs["Scale"])

    # No target: just the curve as drawn, its profile standing up (+Z).
    free = node('GeometryNodeSetCurveNormal')
    L.new(resample.outputs["Curve"], free.inputs["Curve"])
    free.inputs["Mode"].default_value = 'Free'
    free.inputs["Normal"].default_value = (0.0, 0.0, 1.0)  # profile height straight up
    count = node('GeometryNodeAttributeDomainSize', component='MESH')
    L.new(target.outputs["Geometry"], count.inputs["Geometry"])
    has_target = node('FunctionNodeCompare', data_type='INT', operation='GREATER_THAN')
    L.new(count.outputs["Face Count"], has_target.inputs["A"])
    has_target.inputs["B"].default_value = 0
    which = node('GeometryNodeSwitch', input_type='GEOMETRY')
    L.new(has_target.outputs["Result"], which.inputs["Switch"])
    L.new(free.outputs["Curve"], which.inputs["False"])
    L.new(facing.outputs["Curve"], which.inputs["True"])

    sweep = node('GeometryNodeCurveToMesh')
    L.new(which.outputs[0], sweep.inputs["Curve"])
    radius = node('GeometryNodeInputRadius')  # each point's radius (Alt S) scales the moulding there
    L.new(radius.outputs["Radius"], sweep.inputs["Scale"])
    L.new(sized.outputs["Geometry"], sweep.inputs["Profile Curve"])

    # Smooth shading, sharp where the profile turns hard (fillets, steps).
    angle = node('GeometryNodeInputMeshEdgeAngle')
    soft = node('ShaderNodeMath', operation='LESS_THAN')
    L.new(angle.outputs["Unsigned Angle"], soft.inputs[0])
    soft.inputs[1].default_value = math.radians(40)
    shade = node('GeometryNodeSetShadeSmooth', domain='EDGE')
    L.new(sweep.outputs["Mesh"], shade.inputs["Mesh"] if "Mesh" in shade.inputs else shade.inputs[0])
    L.new(soft.outputs[0], shade.inputs["Shade Smooth"])
    faces_smooth = node('GeometryNodeSetShadeSmooth', domain='FACE')
    L.new(shade.outputs[0], faces_smooth.inputs[0])
    # The curve itself (as you drew / edit it, before projecting) shows too, as a wire
    # in the viewport; it doesn't render.
    with_curve = node('GeometryNodeJoinGeometry')
    L.new(gin.outputs["Geometry"], with_curve.inputs[0])  # the curve as drawn, before projecting
    L.new(faces_smooth.outputs[0], with_curve.inputs[0])
    show = node('GeometryNodeSwitch', input_type='GEOMETRY')
    L.new(gin.outputs["Show Curve"], show.inputs["Switch"])
    L.new(faces_smooth.outputs[0], show.inputs["False"])
    L.new(with_curve.outputs[0], show.inputs["True"])
    L.new(show.outputs[0], gout.inputs["Geometry"])
    return ng


def get_node_group():
    ng = bpy.data.node_groups.get(NODE_GROUP)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree':
        return ng
    return _build_node_group()


def _set_profile(mod, key):
    """Pick a built-in profile (key in PROFILES) or 'CUSTOM' in the modifier's menu."""
    label = "Custom" if key == 'CUSTOM' else PROFILES[key][0]
    _set(mod, "Profile", label)


def _set(mod, name, value):
    for item in mod.node_group.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == name:
            props = getattr(mod, "properties", None)
            socket = getattr(getattr(props, "inputs", None), item.identifier, None)
            if socket is not None:
                socket.value = value
            else:
                mod[item.identifier] = value
            return


def make_ornament(context, target, points_world, profile_key, width, height, smooth=True):
    data = bpy.data.curves.new(f"{target.name}_Ornament", 'CURVE')
    data.dimensions = '3D'
    inv = target.matrix_world.inverted()
    local = [inv @ p for p in points_world]  # the curve lives in the target's space
    if smooth:
        spline = data.splines.new('BEZIER')
        spline.bezier_points.add(len(local) - 1)
        for bp, p in zip(spline.bezier_points, local):
            bp.co = p
            bp.handle_left_type = bp.handle_right_type = 'AUTO'
    else:
        spline = data.splines.new('POLY')
        spline.points.add(len(local) - 1)
        for pt, p in zip(spline.points, local):
            pt.co = (p.x, p.y, p.z, 1.0)
    obj = bpy.data.objects.new(data.name, data)
    for col in target.users_collection:
        col.objects.link(obj)
    obj.parent = target
    obj.matrix_parent_inverse.identity()  # same space as the target, follows it
    if target.type == 'MESH' and target.data.materials:
        data.materials.append(target.data.materials[0])
    mod = obj.modifiers.new(MODIFIER, 'NODES')
    mod.node_group = get_node_group()
    scale = max(target.matrix_world.to_scale()) or 1.0
    _set(mod, "Target", target)
    _set_profile(mod, profile_key)
    _set(mod, "Width", width / scale)
    _set(mod, "Height", height / scale)
    return obj


# ---------------------------------------------------------------------------
# Drawing tool
# ---------------------------------------------------------------------------

def _hit(context, xy, skip=()):
    """(world point, object) under the mouse on a solid mesh, or (None, None)."""
    region, rv3d = context.region, context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, xy)
    ray = view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)
    depsgraph = context.evaluated_depsgraph_get()
    for _ in range(16):
        hit, location, _n, _i, obj, _m = context.scene.ray_cast(depsgraph, origin, ray)
        if not hit:
            return None, None
        original = obj.original
        if original.type == 'MESH' and original.display_type not in {'WIRE', 'BOUNDS'} and original not in skip:
            return location, original
        origin = location + ray * 1e-4
    return None, None


def _draw(op, context):
    try:
        _draw_overlay(op, context)
    except Exception as ex:  # never let the overlay break the tool
        if not getattr(op, "_draw_error_shown", False):
            op._draw_error_shown = True
            print(f"G-Tools Ornament overlay: {ex}")
        gpu.state.blend_set('NONE')


def _draw_overlay(op, context):
    import blf
    from .sharp_subtract import hint_panel_origin
    region, rv3d = context.region, context.region_data
    gpu.state.blend_set('ALPHA')
    pts = [view3d_utils.location_3d_to_region_2d(region, rv3d, p) for p in op.points]
    if op.stage == 'DRAW' and op.mouse_hit is not None:
        pts.append(view3d_utils.location_3d_to_region_2d(region, rv3d, op.mouse_hit))
    pts = [p for p in pts if p is not None]
    if len(pts) >= 2:
        line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        line.uniform_float("viewportSize", (region.width, region.height))
        line.uniform_float("lineWidth", 2.0)
        line.uniform_float("color", (0.3, 0.85, 1.0, 0.95))
        seg = []
        for a, b in zip(pts, pts[1:]):
            seg += [tuple(a), tuple(b)]
        batch_for_shader(line, 'LINES', {"pos": seg}).draw(line)
    placed = [tuple(p) for p in pts[:len(op.points)]]
    if placed:  # (an empty batch would make the whole overlay fail)
        dots = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
        gpu.state.point_size_set(8.0)
        dots.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
        batch_for_shader(dots, 'POINTS', {"pos": placed}).draw(dots)
    if op.stage == 'DRAW' and op.mouse_hit is not None:  # where the next point would go
        under = view3d_utils.location_3d_to_region_2d(region, rv3d, op.mouse_hit)
        if under is not None:
            ring = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
            gpu.state.point_size_set(12.0)
            ring.uniform_float("color", (0.3, 0.85, 1.0, 0.9))
            batch_for_shader(ring, 'POINTS', {"pos": [tuple(under)]}).draw(ring)

    if op.stage == 'DRAW':
        title = f"Ornament  ·  {len(op.points)} points"
        hints = [("Click on a surface", "add a point"), ("Ctrl Z / Backspace", "undo point"),
                 ("Enter / Right click / Double click", "finish"), ("Esc", "cancel")]
    else:
        title = (f"Ornament  ·  {PROFILES[op.profile][0]}  ·  width {op.width:.3f}  ·  height {op.height:.3f}")
        hints = [("Wheel", "width"), ("Ctrl Wheel", "height"), ("Shift Wheel", "profile"),
                 ("Enter / Click", "confirm"), ("Esc / Right click", "cancel")]
    font, size = 0, 12
    blf.size(font, size)
    line_h = size * 1.55
    key_w = max(blf.dimensions(font, k)[0] for k, _ in hints) + 14
    width = max(blf.dimensions(font, title)[0], key_w + max(blf.dimensions(font, a)[0] for _, a in hints)) + 20
    height = line_h * (len(hints) + 1) + 14
    x, y = hint_panel_origin(context, width)
    flat = gpu.shader.from_builtin('UNIFORM_COLOR')
    quad = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    batch = batch_for_shader(flat, 'TRIS', {"pos": quad}, indices=((0, 1, 2), (0, 2, 3)))
    flat.uniform_float("color", (0.0, 0.0, 0.0, 0.35))
    batch.draw(flat)
    ty = y + height - line_h
    blf.color(font, 1.0, 0.8, 0.2, 1.0)
    blf.position(font, x + 10, ty, 0)
    blf.draw(font, title)
    for key, action in hints:
        ty -= line_h
        blf.color(font, 1, 1, 1, 0.95)
        blf.position(font, x + 10, ty, 0)
        blf.draw(font, key)
        blf.color(font, 1, 1, 1, 0.6)
        blf.position(font, x + 10 + key_w, ty, 0)
        blf.draw(font, action)
    gpu.state.blend_set('NONE')


class GTOOLS_OT_ornament(bpy.types.Operator):
    """Draw a moulding (crown, bead, baroque...) on an object's surface"""
    bl_idname = "object.gtools_ornament"
    bl_label = "Ornament"
    bl_options = {'REGISTER', 'UNDO'}

    profile: bpy.props.EnumProperty(name="Profile", items=[(k, v[0], "") for k, v in PROFILES.items()],
                                    default='CROWN', options={'SKIP_SAVE'})
    width: bpy.props.FloatProperty(name="Width", default=0.0, min=0.0, options={'SKIP_SAVE'})
    height: bpy.props.FloatProperty(name="Height", default=0.0, min=0.0, options={'SKIP_SAVE'})
    smooth: bpy.props.BoolProperty(name="Smooth Curve", default=True, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and context.mode == 'OBJECT'

    def invoke(self, context, event):
        self.points = []
        self.target = None
        self.mouse_hit = None
        self.stage = 'DRAW'
        self.obj = None
        self._handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (self, context), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set("Ornament: click on a surface to add points  |  Enter: finish  |  "
                                          "Ctrl Z: undo point  |  Esc: cancel")
        self.report({'INFO'}, "Ornament: click points on an object's surface, then Enter")
        return {'RUNNING_MODAL'}

    def _end(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
        context.workspace.status_text_set(None)
        context.area.tag_redraw()

    def _create(self, context):
        size = max(self.target.dimensions) or 1.0
        if not self.width:
            self.width = size * 0.04
        if not self.height:
            self.height = self.width * 0.5
        self.obj = make_ornament(context, self.target, self.points, self.profile, self.width, self.height,
                                 self.smooth)
        self.stage = 'ADJUST'
        context.workspace.status_text_set("Ornament: wheel: width  |  Ctrl wheel: height  |  "
                                          "Shift wheel: profile  |  Enter / click: confirm  |  Esc: cancel")

    def _apply(self):
        mod = self.obj.modifiers.get(MODIFIER)
        scale = max(self.target.matrix_world.to_scale()) or 1.0
        _set(mod, "Width", self.width / scale)
        _set(mod, "Height", self.height / scale)
        _set_profile(mod, self.profile)
        self.obj.update_tag()

    def modal(self, context, event):
        context.area.tag_redraw()
        xy = (event.mouse_region_x, event.mouse_region_y)
        nav = {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}
        if self.stage == 'DRAW':
            if event.type == 'MOUSEMOVE':
                self.mouse_hit, _obj = _hit(context, xy)
            elif event.type == 'LEFTMOUSE' and event.value == 'DOUBLE_CLICK':
                return self._finish_drawing(context)
            elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                point, obj = _hit(context, xy)
                if point is not None and (self.target is None or obj is self.target):
                    self.target = self.target or obj
                    self.points.append(point)
            elif (event.type == 'BACK_SPACE' or (event.type == 'Z' and (event.ctrl or event.oskey))) \
                    and event.value == 'PRESS':
                if self.points:
                    self.points.pop()
            elif event.type in {'RET', 'NUMPAD_ENTER', 'RIGHTMOUSE', 'SPACE'} and event.value == 'PRESS':
                return self._finish_drawing(context)
            elif event.type == 'ESC' and event.value == 'PRESS':
                self._end(context)
                return {'CANCELLED'}
            elif event.type in nav or event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
                return {'PASS_THROUGH'}
            return {'RUNNING_MODAL'}

        up = event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS'} and event.value == 'PRESS'
        down = event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS'} and event.value == 'PRESS'
        if up or down:
            if event.shift:
                keys = list(PROFILES)
                self.profile = keys[(keys.index(self.profile) + (1 if up else -1)) % len(keys)]
            elif event.ctrl:
                self.height = max(1e-5, self.height * (1.15 if up else 1 / 1.15))
            else:
                self.width = max(1e-5, self.width * (1.15 if up else 1 / 1.15))
            self._apply()
        elif (event.type in {'RET', 'NUMPAD_ENTER'} or event.type == 'LEFTMOUSE') and event.value == 'PRESS':
            self._end(context)
            for o in context.selected_objects:
                o.select_set(False)
            self.obj.select_set(True)
            context.view_layer.objects.active = self.obj
            return {'FINISHED'}
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._end(context)
            data = self.obj.data
            bpy.data.objects.remove(self.obj)
            if data.users == 0:
                bpy.data.curves.remove(data)
            return {'CANCELLED'}
        elif event.type in nav:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}

    def _finish_drawing(self, context):
        if len(self.points) < 2 or self.target is None:
            self.report({'INFO'}, "Ornament: click at least two points on a surface")
            return {'RUNNING_MODAL'}
        self._create(context)
        return {'RUNNING_MODAL'}


class GTOOLS_OT_ornament_add(bpy.types.Operator):
    """Add an ornament: a curve with a moulding profile swept along it (Geometry Nodes). With a mesh
    selected it sticks to that mesh's surface"""
    bl_idname = "object.gtools_ornament_add"
    bl_label = "Ornament"
    bl_options = {'REGISTER', 'UNDO'}

    profile: bpy.props.EnumProperty(name="Profile", items=[(k, v[0], "") for k, v in PROFILES.items()],
                                    default='CROWN')
    width: bpy.props.FloatProperty(name="Width", default=0.1, min=0.0, unit='LENGTH')
    height: bpy.props.FloatProperty(name="Height", default=0.05, min=0.0, unit='LENGTH')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        active = context.active_object
        target = active if active is not None and active.type == 'MESH' and active.select_get() else None
        cursor = context.scene.cursor.location.copy()
        data = bpy.data.curves.new("Ornament", 'CURVE')
        data.dimensions = '3D'
        spline = data.splines.new('BEZIER')
        spline.bezier_points.add(2)
        for bp, x in zip(spline.bezier_points, (-1.0, 0.0, 1.0)):
            bp.co = (x, 0.0, 0.0)
            bp.handle_left_type = bp.handle_right_type = 'AUTO'
        obj = bpy.data.objects.new("Ornament", data)
        context.collection.objects.link(obj)
        obj.location = cursor
        mod = obj.modifiers.new(MODIFIER, 'NODES')
        mod.node_group = get_node_group()
        _set_profile(mod, self.profile)
        _set(mod, "Width", self.width)
        _set(mod, "Height", self.height)
        if target is not None:
            _set(mod, "Target", target)
            if target.data.materials:
                data.materials.append(target.data.materials[0])
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {'FINISHED'}


def _menu(self, context):
    self.layout.operator(GTOOLS_OT_ornament_add.bl_idname, icon='MOD_SCREW')


def register():
    bpy.utils.register_class(GTOOLS_OT_ornament)
    bpy.utils.register_class(GTOOLS_OT_ornament_add)
    bpy.types.VIEW3D_MT_add.append(_menu)


def unregister():
    bpy.types.VIEW3D_MT_add.remove(_menu)
    bpy.utils.unregister_class(GTOOLS_OT_ornament_add)
    bpy.utils.unregister_class(GTOOLS_OT_ornament)
