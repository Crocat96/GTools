"""Edge Decal (Object Mode, Boolean pie): fake bevels on every hard edge with floating
decal strips, as a non-destructive Geometry Nodes modifier (node group GT_EdgeDecal).

For every hard edge (angle above a threshold, optionally edges marked sharp) two thin
strips are laid on the faces either side, floating a hair above them. Their custom
normals go from the average of both faces (at the edge) to their own face's normal
(inwards), so the corner shades as if it were beveled while the silhouette stays
sharp. It sits last in the stack, so edges made by booleans get decals too.

Where two hard edges of the same face meet, both strips are mitred on the bisector
(their inner side is cut back by width / tan(corner angle / 2)) so they don't overlap.
Strips are wound to face like the face under them. Normals and lifts use the mesh's
own (corner) normals at each end, which neighbouring strips share, so along smooth
curved edges (a cylinder's rim) the rounding runs on with no steps or cracks. Inputs on the modifier: Width,
Angle (hard edge threshold), Use Sharp Edges (edges marked sharp count too), Concave
Edges, Offset (how far they float).
"""

import bpy

from .curve_patch import _Builder, _sock
from .sharp_subtract import _set_input

NODE_GROUP_NAME = "GT_EdgeDecal"
MODIFIER_NAME = "Edge Decal"


def _build_node_group():
    import math
    ng = bpy.data.node_groups.new(NODE_GROUP_NAME, 'GeometryNodeTree')
    if hasattr(ng, "is_modifier"):
        ng.is_modifier = True
    iface = ng.interface
    iface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    iface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    s = iface.new_socket("Width", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.subtype = 0.02, 0.0, 'DISTANCE'
    s = iface.new_socket("Angle", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value, s.subtype = math.radians(30), 0.0, math.pi, 'ANGLE'
    s = iface.new_socket("Use Sharp Edges", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = True
    s = iface.new_socket("Concave Edges", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = True
    s = iface.new_socket("Offset", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.subtype = 0.0004, 0.0, 'DISTANCE'

    b = _Builder(ng)
    b.column(-2400)
    gin = b.node('NodeGroupInput')
    out = {s.name: s for s in gin.outputs}
    geo = out["Geometry"]

    def named(name, data_type='FLOAT_VECTOR'):
        n = b.node('GeometryNodeInputNamedAttribute', data_type=data_type)
        _sock(n.inputs, "Name").default_value = name
        return _sock(n.outputs, "Attribute")

    def store(geometry, name, value, domain, data_type='FLOAT_VECTOR'):
        n = b.node('GeometryNodeStoreNamedAttribute', domain=domain, data_type=data_type)
        b.link(geometry, _sock(n.inputs, "Geometry"))
        _sock(n.inputs, "Name").default_value = name
        b.link(value, _sock(n.inputs, "Value"))
        return _sock(n.outputs, "Geometry")

    def normalize(v):
        n = b.node('ShaderNodeVectorMath', operation='NORMALIZE')
        b.link(v, n.inputs[0])
        return _sock(n.outputs, "Vector")

    # Per edge (evaluated on the edge domain)
    b.column(-2100)
    ev = b.node('GeometryNodeInputMeshEdgeVertices')
    p1, p2 = _sock(ev.outputs, "Position 1"), _sock(ev.outputs, "Position 2")
    span = b.vec('SUBTRACT', p2, p1)
    direction = normalize(span)
    length_n = b.node('ShaderNodeVectorMath', operation='LENGTH')
    b.link(span, length_n.inputs[0])
    length = _sock(length_n.outputs, "Value")
    mid_n = b.node('ShaderNodeVectorMath', operation='SCALE')
    b.link(b.vec('ADD', p1, p2), mid_n.inputs[0])
    _sock(mid_n.inputs, "Scale").default_value = 0.5
    mid = _sock(mid_n.outputs, "Vector")

    sides, corners = [], []
    for k in (0, 1):
        b.column(-1800 + 300 * k)
        coe = b.node('GeometryNodeCornersOfEdge')
        _sock(coe.inputs, "Sort Index").default_value = k
        corners.append(_sock(coe.outputs, "Corner Index"))
        foc = b.node('GeometryNodeFaceOfCorner')
        b.link(_sock(coe.outputs, "Corner Index"), _sock(foc.inputs, "Corner Index"))
        face = _sock(foc.outputs, "Face Index")
        samples = []
        for field in ('GeometryNodeInputNormal', 'GeometryNodeInputPosition'):
            src = b.node(field)
            si = b.node('GeometryNodeSampleIndex', domain='FACE', data_type='FLOAT_VECTOR')
            b.link(geo, _sock(si.inputs, "Geometry"))
            b.link(src.outputs[0], _sock(si.inputs, "Value", 'VECTOR'))
            b.link(face, _sock(si.inputs, "Index"))
            samples.append(_sock(si.outputs, "Value", 'VECTOR'))
        normal, center = samples
        # inward direction on that face, square to the edge
        to_center = b.vec('SUBTRACT', center, mid)
        along = b.node('ShaderNodeVectorMath', operation='PROJECT')
        b.link(to_center, along.inputs[0])
        b.link(direction, along.inputs[1])
        inward = normalize(b.vec('SUBTRACT', to_center, _sock(along.outputs, "Vector")))
        sides.append((normal, inward))
    edge_normal = normalize(b.vec('ADD', sides[0][0], sides[1][0]))

    # Hard edges
    b.column(-1200)
    angle = b.node('GeometryNodeInputMeshEdgeAngle')
    neighbors = b.node('GeometryNodeInputMeshEdgeNeighbors')
    two = b.node('FunctionNodeCompare', data_type='INT', operation='EQUAL')
    b.link(_sock(neighbors.outputs, "Face Count"), _sock(two.inputs, "A", 'INT'))
    _sock(two.inputs, "B", 'INT').default_value = 2
    steep = b.node('FunctionNodeCompare', data_type='FLOAT', operation='GREATER_EQUAL')
    b.link(_sock(angle.outputs, "Unsigned Angle"), _sock(steep.inputs, "A", 'VALUE'))
    b.link(out["Angle"], _sock(steep.inputs, "B", 'VALUE'))
    convex = b.node('FunctionNodeCompare', data_type='FLOAT', operation='GREATER_THAN')
    b.link(_sock(angle.outputs, "Signed Angle"), _sock(convex.inputs, "A", 'VALUE'))
    _sock(convex.inputs, "B", 'VALUE').default_value = 0.0

    def boolean(op, a, c=None):
        n = b.node('FunctionNodeBooleanMath', operation=op)
        b.link(a, n.inputs[0])
        if c is not None:
            b.link(c, n.inputs[1])
        return n.outputs[0]

    smooth = b.node('GeometryNodeInputEdgeSmooth')
    side_ok = boolean('OR', _sock(convex.outputs, "Result"), out["Concave Edges"])
    by_angle = boolean('AND', _sock(steep.outputs, "Result"), side_ok)
    by_mark = boolean('AND', boolean('NOT', _sock(smooth.outputs, "Smooth")), out["Use Sharp Edges"])
    hard = boolean('AND', boolean('OR', by_angle, by_mark), _sock(two.outputs, "Result"))

    # Mitres: where the next edge of the same face is hard too, the strip's inner side
    # is cut back by width / tan(corner angle / 2), so both strips meet on the bisector
    # instead of overlapping (and flickering).
    def sample(domain, data_type, value, index):
        n = b.node('GeometryNodeSampleIndex', domain=domain, data_type=data_type)
        b.link(geo, _sock(n.inputs, "Geometry"))
        stype = {'FLOAT_VECTOR': 'VECTOR', 'FLOAT': 'VALUE', 'BOOLEAN': 'BOOLEAN'}[data_type]
        b.link(value, _sock(n.inputs, "Value", stype))
        b.link(index, _sock(n.inputs, "Index"))
        return _sock(n.outputs, "Value", stype)

    def math_op(op, a, c=None):
        n = b.node('ShaderNodeMath', operation=op)
        b._set(n.inputs[0], a)
        if c is not None:
            b._set(n.inputs[1], c)
        return n.outputs[0]

    def end_shift(V, V_other, other_edge):
        evs = b.node('GeometryNodeInputMeshEdgeVertices')
        q1 = sample('EDGE', 'FLOAT_VECTOR', _sock(evs.outputs, "Position 1"), other_edge)
        q2 = sample('EDGE', 'FLOAT_VECTOR', _sock(evs.outputs, "Position 2"), other_edge)
        twice = b.node('ShaderNodeVectorMath', operation='SCALE')
        b.link(V, twice.inputs[0])
        _sock(twice.inputs, "Scale").default_value = 2.0
        u = normalize(b.vec('SUBTRACT', b.vec('ADD', q1, q2), _sock(twice.outputs, "Vector")))  # along the other edge
        t = normalize(b.vec('SUBTRACT', V_other, V))                                            # along this edge
        dot = b.node('ShaderNodeVectorMath', operation='DOT_PRODUCT')
        b.link(t, dot.inputs[0])
        b.link(u, dot.inputs[1])
        cos = math_op('MAXIMUM', math_op('MINIMUM', _sock(dot.outputs, "Value"), 1.0), -1.0)
        tan_half = math_op('MAXIMUM', math_op('TANGENT', math_op('MULTIPLY', math_op('ARCCOSINE', cos), 0.5)), 0.05)
        shift = math_op('MINIMUM', math_op('DIVIDE', out["Width"], tan_half), math_op('MULTIPLY', length, 0.5))
        other_hard = sample('EDGE', 'BOOLEAN', hard, other_edge)
        return math_op('MULTIPLY', shift, other_hard)

    b.column(-1050)
    shifts, smooth_normals = [], []
    for k in (0, 1):
        c_a = corners[k]
        nxt = b.node('GeometryNodeOffsetCornerInFace')
        b.link(c_a, _sock(nxt.inputs, "Corner Index"))
        _sock(nxt.inputs, "Offset").default_value = 1
        c_b = _sock(nxt.outputs, "Corner Index")
        verts = []
        for c in (c_a, c_b):
            voc = b.node('GeometryNodeVertexOfCorner')
            b.link(c, _sock(voc.inputs, "Corner Index"))
            p = b.node('GeometryNodeInputPosition')
            verts.append(sample('POINT', 'FLOAT_VECTOR', p.outputs[0], _sock(voc.outputs, "Vertex Index")))
        V_a, V_b = verts
        eoc_a = b.node('GeometryNodeEdgesOfCorner')
        b.link(c_a, _sock(eoc_a.inputs, "Corner Index"))
        eoc_b = b.node('GeometryNodeEdgesOfCorner')
        b.link(c_b, _sock(eoc_b.inputs, "Corner Index"))
        shift_a = end_shift(V_a, V_b, _sock(eoc_a.outputs, "Previous Edge Index"))
        shift_b = end_shift(V_b, V_a, _sock(eoc_b.outputs, "Next Edge Index"))
        # which end of the edge (vertex 1 / vertex 2) corner a sits on
        dot = b.node('ShaderNodeVectorMath', operation='DOT_PRODUCT')
        b.link(b.vec('SUBTRACT', V_a, mid), dot.inputs[0])
        b.link(direction, dot.inputs[1])
        a_is_1 = math_op('LESS_THAN', _sock(dot.outputs, "Value"), 0.0)
        shifts.append((b.switch('FLOAT', a_is_1, shift_b, shift_a), b.switch('FLOAT', a_is_1, shift_a, shift_b)))
        # The mesh's own normals at both ends on this face (corner normals: smooth
        # across smooth edges), so strips follow smooth shading instead of faceting it.
        corner_normals = []
        for c in (c_a, c_b):
            nn = b.node('GeometryNodeInputNormal')
            corner_normals.append(sample('CORNER', 'FLOAT_VECTOR', nn.outputs[0], c))
        n_ca, n_cb = corner_normals
        smooth_normals.append((b.switch('VECTOR', a_is_1, n_cb, n_ca), b.switch('VECTOR', a_is_1, n_ca, n_cb)))

    # Edge data onto the mesh (a copy for the decals), then one point per hard edge
    b.column(-900)
    g = geo
    for name, value in (("gt_ed_dir", direction), ("gt_ed_n0", sides[0][0]), ("gt_ed_d0", sides[0][1]),
                        ("gt_ed_n1", sides[1][0]), ("gt_ed_d1", sides[1][1]), ("gt_ed_ne", edge_normal)):
        g = store(g, name, value, 'EDGE')
    g = store(g, "gt_ed_len", length, 'EDGE', 'FLOAT')
    for k in (0, 1):
        g = store(g, f"gt_ed_s1{k}", shifts[k][0], 'EDGE', 'FLOAT')
        g = store(g, f"gt_ed_s2{k}", shifts[k][1], 'EDGE', 'FLOAT')
        g = store(g, f"gt_ed_m{k}1", smooth_normals[k][0], 'EDGE')
        g = store(g, f"gt_ed_m{k}2", smooth_normals[k][1], 'EDGE')
    for end in (1, 2):  # at the edge itself both strips share this normal
        g = store(g, f"gt_ed_e{end}", normalize(b.vec('ADD', smooth_normals[0][end - 1], smooth_normals[1][end - 1])), 'EDGE')
    m2p = b.node('GeometryNodeMeshToPoints', mode='EDGES')
    b.link(g, _sock(m2p.inputs, "Mesh"))
    b.link(hard, _sock(m2p.inputs, "Selection"))
    points = _sock(m2p.outputs, "Points")

    # A unit strip: X along the edge (-0.5..0.5), Y inwards (0..1); gt_ed_t = Y
    b.column(-600)
    grid = b.node('GeometryNodeMeshGrid')
    for name, val in (("Size X", 1.0), ("Size Y", 1.0), ("Vertices X", 2), ("Vertices Y", 2)):
        _sock(grid.inputs, name).default_value = val
    move = b.node('GeometryNodeTransform')
    b.link(_sock(grid.outputs, "Mesh"), _sock(move.inputs, "Geometry"))
    _sock(move.inputs, "Translation").default_value = (0.0, 0.5, 0.0)
    pos = b.node('GeometryNodeInputPosition')
    sep = b.node('ShaderNodeSeparateXYZ')
    b.link(pos.outputs[0], sep.inputs[0])
    strip = store(_sock(move.outputs, "Geometry"), "gt_ed_t", _sock(sep.outputs, "Y"), 'POINT', 'FLOAT')
    strip = store(strip, "gt_ed_x", _sock(sep.outputs, "X"), 'POINT', 'FLOAT')

    b.column(-300)
    halves = []
    for k in (0, 1):
        pts = store(points, "gt_ed_nf", named(f"gt_ed_n{k}"), 'POINT')
        pts = store(pts, "gt_ed_sa", named(f"gt_ed_s1{k}", 'FLOAT'), 'POINT', 'FLOAT')
        pts = store(pts, "gt_ed_sb", named(f"gt_ed_s2{k}", 'FLOAT'), 'POINT', 'FLOAT')
        pts = store(pts, "gt_ed_mf1", named(f"gt_ed_m{k}1"), 'POINT')
        pts = store(pts, "gt_ed_mf2", named(f"gt_ed_m{k}2"), 'POINT')
        rot = b.node('FunctionNodeAxesToRotation', primary_axis='X', secondary_axis='Y')
        b.link(named("gt_ed_dir"), _sock(rot.inputs, "Primary Axis"))
        b.link(named(f"gt_ed_d{k}"), _sock(rot.inputs, "Secondary Axis"))
        scale = b.node('ShaderNodeCombineXYZ')
        b.link(named("gt_ed_len", 'FLOAT'), scale.inputs[0])
        b.link(out["Width"], scale.inputs[1])
        scale.inputs[2].default_value = 1.0
        inst = b.node('GeometryNodeInstanceOnPoints')
        b.link(pts, _sock(inst.inputs, "Points"))
        b.link(strip, _sock(inst.inputs, "Instance"))
        b.link(_sock(rot.outputs, "Rotation"), _sock(inst.inputs, "Rotation"))
        b.link(scale.outputs[0], _sock(inst.inputs, "Scale"))
        real = b.node('GeometryNodeRealizeInstances')
        b.link(_sock(inst.outputs, "Instances"), _sock(real.inputs, "Geometry"))
        halves.append(_sock(real.outputs, "Geometry"))

    b.column(0)
    join = b.node('GeometryNodeJoinGeometry')
    for h in reversed(halves):
        b.link(h, join.inputs[0])
    at_1 = b.node('ShaderNodeMath', operation='LESS_THAN')  # vertex at the edge's vertex-1 end
    b.link(named("gt_ed_x", 'FLOAT'), at_1.inputs[0])
    at_1.inputs[1].default_value = 0.0
    # Normals: at the edge, the average of both faces' (smooth) normals there; at the
    # inner side, this face's normal there. Neighbouring strips share them, so the
    # rounding runs on without steps along curved edges.
    n_edge = b.switch('VECTOR', at_1.outputs[0], named("gt_ed_e2"), named("gt_ed_e1"))
    n_face = b.switch('VECTOR', at_1.outputs[0], named("gt_ed_mf2"), named("gt_ed_mf1"))
    nrm = normalize(b.lerp(n_edge, n_face, named("gt_ed_t", 'FLOAT')))
    # Lift (float above the surface) along normals the neighbouring strips share, so
    # they meet exactly instead of leaving hairline cracks where the edge shows through:
    # at the edge along the edge normal, far enough to clear both faces by Offset; at
    # the inner side along the mesh's normal there.
    cos_half = b.node('ShaderNodeVectorMath', operation='DOT_PRODUCT')
    b.link(n_edge, cos_half.inputs[0])
    b.link(n_face, cos_half.inputs[1])
    clamp = b.node('ShaderNodeMath', operation='MAXIMUM')
    b.link(_sock(cos_half.outputs, "Value"), clamp.inputs[0])
    clamp.inputs[1].default_value = 0.2
    edge_lift = b.node('ShaderNodeMath', operation='DIVIDE')
    b.link(out["Offset"], edge_lift.inputs[0])
    b.link(clamp.outputs[0], edge_lift.inputs[1])
    at_edge = b.node('ShaderNodeVectorMath', operation='SCALE')
    b.link(n_edge, at_edge.inputs[0])
    b.link(edge_lift.outputs[0], _sock(at_edge.inputs, "Scale"))
    at_inner = b.node('ShaderNodeVectorMath', operation='SCALE')
    b.link(n_face, at_inner.inputs[0])
    b.link(out["Offset"], _sock(at_inner.inputs, "Scale"))
    lift_v = b.lerp(_sock(at_edge.outputs, "Vector"), _sock(at_inner.outputs, "Vector"), named("gt_ed_t", 'FLOAT'))
    # mitre: inner corners slide along the edge (vertex-1 end forwards, vertex-2 end back)
    back = b.node('ShaderNodeMath', operation='MULTIPLY')
    b.link(named("gt_ed_sb", 'FLOAT'), back.inputs[0])
    back.inputs[1].default_value = -1.0
    slide = b.switch('FLOAT', at_1.outputs[0], back.outputs[0], named("gt_ed_sa", 'FLOAT'))
    slide_t = b.node('ShaderNodeMath', operation='MULTIPLY')
    b.link(slide, slide_t.inputs[0])
    b.link(named("gt_ed_t", 'FLOAT'), slide_t.inputs[1])
    along = b.node('ShaderNodeVectorMath', operation='SCALE')
    b.link(named("gt_ed_dir"), along.inputs[0])
    b.link(slide_t.outputs[0], _sock(along.inputs, "Scale"))
    setpos = b.node('GeometryNodeSetPosition')
    b.link(join.outputs[0], _sock(setpos.inputs, "Geometry"))
    b.link(b.vec('ADD', lift_v, _sock(along.outputs, "Vector")), _sock(setpos.inputs, "Offset"))
    b.column(300)
    # Strips must face the same way as the face under them (their winding depends on
    # which of the edge's two faces they lie on); back-facing ones would shade dark.
    face_n = b.node('GeometryNodeInputNormal')
    facing = b.node('ShaderNodeVectorMath', operation='DOT_PRODUCT')
    b.link(face_n.outputs[0], facing.inputs[0])
    b.link(named("gt_ed_nf"), facing.inputs[1])
    backwards = b.node('ShaderNodeMath', operation='LESS_THAN')
    b.link(_sock(facing.outputs, "Value"), backwards.inputs[0])
    backwards.inputs[1].default_value = 0.0
    flip = b.node('GeometryNodeFlipFaces')
    b.link(_sock(setpos.outputs, "Geometry"), _sock(flip.inputs, "Mesh"))
    b.link(backwards.outputs[0], _sock(flip.inputs, "Selection"))
    shade = b.node('GeometryNodeSetShadeSmooth')
    b.link(_sock(flip.outputs, "Mesh"), _sock(shade.inputs, "Mesh"))
    _sock(shade.inputs, "Shade Smooth").default_value = True
    setn = b.node('GeometryNodeSetMeshNormal', mode='FREE', domain='POINT')
    b.link(_sock(shade.outputs, "Mesh"), _sock(setn.inputs, "Mesh"))
    b.link(nrm, _sock(setn.inputs, "Custom Normal"))
    clean = b.node('GeometryNodeRemoveAttribute')
    b.link(_sock(setn.outputs, "Mesh"), _sock(clean.inputs, "Geometry"))
    try:
        _sock(clean.inputs, "Pattern Mode").default_value = 'Wildcard'
    except (TypeError, KeyError):
        pass
    _sock(clean.inputs, "Name").default_value = "gt_ed_*"

    b.column(600)
    final = b.node('GeometryNodeJoinGeometry')
    b.link(_sock(clean.outputs, "Geometry"), final.inputs[0])
    b.link(geo, final.inputs[0])
    gout = b.node('NodeGroupOutput')
    b.link(final.outputs[0], gout.inputs[0])
    return ng


def get_node_group():
    ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree':
        return ng
    return _build_node_group()


def _input_id(ng, name):
    for item in ng.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == name:
            return item.identifier
    raise KeyError(name)


class GTOOLS_OT_edge_decal(bpy.types.Operator):
    """Add floating edge decals on every hard edge so the corners shade as if beveled (non-destructive modifier)"""
    bl_idname = "object.gtools_edge_decal"
    bl_label = "Edge Decal"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        ng = get_node_group()
        added = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH' or any(m.type == 'NODES' and m.node_group is ng for m in obj.modifiers):
                continue
            mod = obj.modifiers.new(MODIFIER_NAME, 'NODES')
            mod.node_group = ng
            size = max(obj.dimensions) or 1.0
            _set_input(mod, _input_id(ng, "Width"), size * 0.01)
            _set_input(mod, _input_id(ng, "Offset"), size * 0.0002)
            added += 1
        if not added:
            self.report({'INFO'}, "Edge Decal: the selected objects already have it")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(GTOOLS_OT_edge_decal)


def unregister():
    bpy.utils.unregister_class(GTOOLS_OT_edge_decal)
