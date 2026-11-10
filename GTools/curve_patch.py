"""Curve Patch (Object Mode, Select pie): a live surface between 2, 3 or 4 curves,
like the boundary / patch tools of CAD software (Plasticity, Alias, Rhino).

Select 2 to 4 open curve objects and run "Create Patch from Curves" (it only shows in
the Select pie when such a selection exists). A new mesh object "Patch" gets a
Geometry Nodes modifier (node group GT_CurvePatch) that reads the curves through
Object Info nodes, so moving the curves or editing their points (also in Edit Mode)
updates the patch in real time.

The surface is a bilinearly blended Coons patch:

    S(u, v) = lerp(Bottom(u), Top(u), v) + lerp(Left(v), Right(v), u)
              - lerp(lerp(P00, P10, u), lerp(P01, P11, u), v)

- 4 curves: Bottom, Right, Top, Left around the patch. The patch passes exactly
  through all four curves (if they meet at the corners).
- 3 curves: Bottom, Left and Right; Top collapses to the point where Left and Right
  meet (a triangular patch).
- 2 curves: Bottom and Top; the sides are straight lines (a ruled loft).

Which curve is which, and which way each one runs, is worked out once when the patch
is created, from where the curve ends meet. It is stored in the modifier (Bottom /
Top / Left / Right objects and the Reverse switches), where it can be changed by
hand, together with the resolution, flipping the normals and smooth shading.
Only the first spline of each curve is used.
"""

import bpy

from .sharp_subtract import _set_input

NODE_GROUP_NAME = "GT_CurvePatch"
MODIFIER_NAME = "Curve Patch"
SIDES = ("Bottom", "Top", "Left", "Right")


# ---------------------------------------------------------------------------
# Node group
# ---------------------------------------------------------------------------

def _sock(sockets, name, stype=None):
    """Enabled socket by name (and type: some nodes, like Mix, repeat names per type)."""
    for s in sockets:
        if s.name == name and s.enabled and (stype is None or s.type == stype):
            return s
    for s in sockets:
        if s.name == name and (stype is None or s.type == stype):
            return s
    raise KeyError(f"Socket {name!r} ({stype}) not found")


class _Builder:
    """Small helper to write the node tree as expressions."""

    def __init__(self, ng):
        self.ng = ng
        self.nodes = ng.nodes
        self.links = ng.links
        self.x = 0
        self.y = 0

    def node(self, bl_idname, **props):
        n = self.nodes.new(bl_idname)
        for k, v in props.items():
            setattr(n, k, v)
        n.location = (self.x, self.y)
        self.y -= 160
        return n

    def column(self, x):
        self.x, self.y = x, 0

    def link(self, out, inp):
        self.links.new(out, inp)

    def _set(self, socket, value):
        if isinstance(value, bpy.types.NodeSocket):
            self.link(value, socket)
        else:
            socket.default_value = value

    def math(self, op, a, b=0.0):
        n = self.node('ShaderNodeMath', operation=op)
        self._set(n.inputs[0], a)
        self._set(n.inputs[1], b)
        return n.outputs[0]

    def vec(self, op, a, b):
        n = self.node('ShaderNodeVectorMath', operation=op)
        self._set(n.inputs[0], a)
        self._set(n.inputs[1], b)
        return _sock(n.outputs, "Vector")

    def lerp(self, a, b, t):
        n = self.node('ShaderNodeMix', data_type='VECTOR', factor_mode='UNIFORM')
        self._set(_sock(n.inputs, "Factor", 'VALUE'), t)
        self._set(_sock(n.inputs, "A", 'VECTOR'), a)
        self._set(_sock(n.inputs, "B", 'VECTOR'), b)
        return _sock(n.outputs, "Result", 'VECTOR')

    def switch(self, input_type, cond, false, true):
        n = self.node('GeometryNodeSwitch', input_type=input_type)
        self._set(_sock(n.inputs, "Switch"), cond)
        self._set(_sock(n.inputs, "False"), false)
        self._set(_sock(n.inputs, "True"), true)
        return _sock(n.outputs, "Output")


def _build_node_group():
    ng = bpy.data.node_groups.new(NODE_GROUP_NAME, 'GeometryNodeTree')
    if hasattr(ng, "is_modifier"):
        ng.is_modifier = True
    iface = ng.interface
    iface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    iface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    for side in SIDES:
        iface.new_socket(side, in_out='INPUT', socket_type='NodeSocketObject')
    for side in SIDES:
        s = iface.new_socket(f"Reverse {side}", in_out='INPUT', socket_type='NodeSocketBool')
        s.default_value = False
    for axis in ("U", "V"):
        s = iface.new_socket(f"Resolution {axis}", in_out='INPUT', socket_type='NodeSocketInt')
        s.default_value, s.min_value, s.max_value = 32, 2, 1024
    s = iface.new_socket("Flip Normals", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = False
    s = iface.new_socket("Shade Smooth", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = True

    b = _Builder(ng)
    b.column(-1800)
    gin = b.node('NodeGroupInput')
    out = {s.name: s for s in gin.outputs}

    # Grid in [-0.5, 0.5]^2 -> (u, v) in [0, 1]^2
    b.column(-1500)
    grid = b.node('GeometryNodeMeshGrid')
    _sock(grid.inputs, "Size X").default_value = 1.0
    _sock(grid.inputs, "Size Y").default_value = 1.0
    b.link(out["Resolution U"], _sock(grid.inputs, "Vertices X"))
    b.link(out["Resolution V"], _sock(grid.inputs, "Vertices Y"))
    pos = b.node('GeometryNodeInputPosition')
    sep = b.node('ShaderNodeSeparateXYZ')
    b.link(_sock(pos.outputs, "Position"), sep.inputs[0])
    b.column(-1300)
    u = b.math('ADD', _sock(sep.outputs, "X"), 0.5)
    v = b.math('ADD', _sock(sep.outputs, "Y"), 0.5)

    # Each side: its curve geometry, whether it is set, and a sampler.
    curves, present = {}, {}
    for side in SIDES:
        b.column(-1300)
        b.y = -400 - 500 * SIDES.index(side)
        info = b.node('GeometryNodeObjectInfo', transform_space='RELATIVE')
        b.link(out[side], _sock(info.inputs, "Object"))
        size = b.node('GeometryNodeAttributeDomainSize', component='CURVE')
        b.link(_sock(info.outputs, "Geometry"), size.inputs[0])
        cmp = b.node('FunctionNodeCompare', data_type='INT', operation='GREATER_THAN')
        b.link(_sock(size.outputs, "Point Count"), _sock(cmp.inputs, "A", 'INT'))
        _sock(cmp.inputs, "B", 'INT').default_value = 0
        curves[side] = _sock(info.outputs, "Geometry")
        present[side] = _sock(cmp.outputs, "Result")

    def sample(side, t):
        """Position on a side's curve at factor t (0..1), honouring its Reverse switch."""
        f = b.switch('FLOAT', out[f"Reverse {side}"], t, b.math('SUBTRACT', 1.0, t))
        n = b.node('GeometryNodeSampleCurve', mode='FACTOR')
        b.link(curves[side], _sock(n.inputs, "Curves"))
        b.link(f, _sock(n.inputs, "Factor"))
        return _sock(n.outputs, "Position")

    b.column(-1000)
    bottom_u = sample("Bottom", u)
    p00 = sample("Bottom", 0.0)
    p10 = sample("Bottom", 1.0)
    left_end = sample("Left", 1.0)
    right_end = sample("Right", 1.0)
    b.column(-700)
    # No Top curve (3 curves): the top corners are where Left and Right end.
    p01 = b.switch('VECTOR', present["Top"], left_end, sample("Top", 0.0))
    p11 = b.switch('VECTOR', present["Top"], right_end, sample("Top", 1.0))
    b.column(-400)
    top_u = b.switch('VECTOR', present["Top"], b.lerp(p01, p11, u), sample("Top", u))
    # No side curves (2 curves): straight sides between the corners.
    left_v = b.switch('VECTOR', present["Left"], b.lerp(p00, p01, v), sample("Left", v))
    right_v = b.switch('VECTOR', present["Right"], b.lerp(p10, p11, v), sample("Right", v))

    b.column(-100)
    ruled_uv = b.lerp(bottom_u, top_u, v)
    ruled_vu = b.lerp(left_v, right_v, u)
    corners = b.lerp(b.lerp(p00, p10, u), b.lerp(p01, p11, u), v)
    b.column(200)
    surface = b.vec('SUBTRACT', b.vec('ADD', ruled_uv, ruled_vu), corners)

    b.column(500)
    setpos = b.node('GeometryNodeSetPosition')
    b.link(_sock(grid.outputs, "Mesh"), _sock(setpos.inputs, "Geometry"))
    b.link(surface, _sock(setpos.inputs, "Position"))
    # A collapsed side (3 curves) leaves a row of points on top of each other.
    merge = b.node('GeometryNodeMergeByDistance')
    b.link(_sock(setpos.outputs, "Geometry"), merge.inputs[0])
    _sock(merge.inputs, "Distance").default_value = 1e-5
    b.column(800)
    uv = b.node('GeometryNodeStoreNamedAttribute', data_type='FLOAT2', domain='CORNER')
    b.link(_sock(merge.outputs, "Geometry"), _sock(uv.inputs, "Geometry"))
    _sock(uv.inputs, "Name").default_value = "UVMap"
    b.link(_sock(grid.outputs, "UV Map"), _sock(uv.inputs, "Value", 'VECTOR'))
    flip = b.node('GeometryNodeFlipFaces')
    b.link(_sock(uv.outputs, "Geometry"), _sock(flip.inputs, "Mesh"))
    b.link(out["Flip Normals"], _sock(flip.inputs, "Selection"))
    smooth = b.node('GeometryNodeSetShadeSmooth')
    b.link(_sock(flip.outputs, "Mesh"), _sock(smooth.inputs, "Mesh"))
    b.link(out["Shade Smooth"], _sock(smooth.inputs, "Shade Smooth"))
    b.column(1100)
    gout = b.node('NodeGroupOutput')
    b.link(_sock(smooth.outputs, "Mesh"), gout.inputs[0])
    return ng


def _has_inputs(ng):
    names = {i.name for i in ng.interface.items_tree if getattr(i, "in_out", None) == 'INPUT'}
    return all(s in names for s in SIDES)


def get_node_group():
    """Reuse GT_CurvePatch if it exists (even if you edited it), otherwise build it."""
    ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree' and _has_inputs(ng):
        return ng
    return _build_node_group()


def _input_id(ng, name):
    for item in ng.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == name:
            return item.identifier
    raise KeyError(name)


# ---------------------------------------------------------------------------
# Working out the layout from the curve ends
# ---------------------------------------------------------------------------

def curve_ends(obj):
    """World-space start and end of the first spline, or None if it can't be used."""
    splines = obj.data.splines
    if not splines:
        return None
    sp = splines[0]
    if sp.use_cyclic_u:
        return None
    if sp.type == 'BEZIER':
        if len(sp.bezier_points) < 2:
            return None
        a, z = sp.bezier_points[0].co, sp.bezier_points[-1].co
    else:
        if len(sp.points) < 2:
            return None
        a, z = sp.points[0].co.xyz, sp.points[-1].co.xyz
    m = obj.matrix_world
    return m @ a, m @ z


def _nearest(point, candidates, ends):
    """The candidate with an end closest to point, and whether that end is its last
    point (so the curve must be reversed to start there)."""
    best = None
    for c in candidates:
        s, e = ends[c]
        for d, rev in (((s - point).length, False), ((e - point).length, True)):
            if best is None or d < best[0]:
                best = (d, c, rev)
    return best[1], best[2], best[0]


def solve_layout(objs, ends):
    """Assign Bottom / Top / Left / Right and Reverse flags. Returns (layout, max_gap).
    layout: {side: (obj, reverse)} with every curve running Bottom: P00->P10,
    Top: P01->P11, Left: P00->P01, Right: P10->P11."""
    base, others = objs[0], list(objs[1:])
    bs, be = ends[base]
    layout = {"Bottom": (base, False)}

    if len(objs) == 2:
        top = others[0]
        ts, te = ends[top]
        straight = (ts - bs).length + (te - be).length
        crossed = (te - bs).length + (ts - be).length
        layout["Top"] = (top, crossed < straight)
        return layout, 0.0

    left, rev_l, gap_l = _nearest(bs, others, ends)
    others.remove(left)
    right, rev_r, gap_r = _nearest(be, others, ends)
    others.remove(right)
    layout["Left"] = (left, rev_l)
    layout["Right"] = (right, rev_r)
    left_far = ends[left][0 if rev_l else 1]
    right_far = ends[right][0 if rev_r else 1]
    gaps = [gap_l, gap_r]

    if others:  # 4 curves
        top = others[0]
        ts, te = ends[top]
        straight = (ts - left_far).length + (te - right_far).length
        crossed = (te - left_far).length + (ts - right_far).length
        layout["Top"] = (top, crossed < straight)
        gaps.append(min(straight, crossed) / 2)
    else:  # 3 curves: Left and Right meet at the apex
        gaps.append((left_far - right_far).length)
    return layout, max(gaps)


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

def patch_curves(context):
    """The selected curve objects if they can make a patch (2 to 4 curves), else None."""
    sel = [o for o in context.selected_objects if o is not None]
    if not 2 <= len(sel) <= 4 or any(o.type != 'CURVE' for o in sel):
        return None
    return sel


class GTOOLS_OT_curve_patch(bpy.types.Operator):
    """Create a live patch surface between 2, 3 or 4 selected curves (it follows the curves as you edit them)"""
    bl_idname = "object.gtools_curve_patch"
    bl_label = "Create Patch from Curves"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and patch_curves(context) is not None

    def execute(self, context):
        objs = patch_curves(context)
        active = context.active_object
        if active in objs:  # the active curve is the bottom edge
            objs.remove(active)
            objs.insert(0, active)

        ends = {}
        for o in objs:
            e = curve_ends(o)
            if e is None:
                self.report({'WARNING'}, f"'{o.name}' needs an open spline with at least 2 points")
                return {'CANCELLED'}
            ends[o] = e

        layout, gap = solve_layout(objs, ends)

        mesh = bpy.data.meshes.new("Patch")
        patch = bpy.data.objects.new("Patch", mesh)
        context.collection.objects.link(patch)

        ng = get_node_group()
        mod = patch.modifiers.new(MODIFIER_NAME, 'NODES')
        mod.node_group = ng
        for side, (obj, rev) in layout.items():
            _set_input(mod, _input_id(ng, side), obj)
            _set_input(mod, _input_id(ng, f"Reverse {side}"), rev)

        for o in context.selected_objects:
            o.select_set(False)
        patch.select_set(True)
        context.view_layer.objects.active = patch

        pts = [p for e in ends.values() for p in e]
        size = max((a - b).length for a in pts for b in pts)
        if gap > 0.01 * max(size, 1e-6):
            self.report({'WARNING'}, f"The curve ends don't meet (gap {gap:.3g}): snap them for a closed patch")
        return {'FINISHED'}


classes = (GTOOLS_OT_curve_patch,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
