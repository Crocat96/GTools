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

# v2: hard edges made sharp in the modifier, smooth corner normals. Older groups in a
# file are swapped for this one when Edge Decal is run on the object again.
NODE_GROUP_NAME = "GT_EdgeDecal_v2"
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
    # Off by default: on inside corners the fake rounding reads as a raised bead.
    s = iface.new_socket("Concave Edges", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = False
    s = iface.new_socket("Offset", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.subtype = 0.0004, 0.0, 'DISTANCE'
    # Edge wear (concrete-like chipped edges): 0 = smooth bevel.
    s = iface.new_socket("Wear", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value, s.subtype = 0.0, 0.0, 1.0, 'FACTOR'
    s = iface.new_socket("Wear Scale", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 6.0, 0.01
    s = iface.new_socket("Wear Detail", in_out='INPUT', socket_type='NodeSocketInt')
    s.default_value, s.min_value, s.max_value = 64, 4, 512
    # 0 = soft, blobby wear; 1 = sharp, angular chips (concrete)
    s = iface.new_socket("Wear Sharpness", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value, s.subtype = 0.8, 0.0, 1.0, 'FACTOR'

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
    by_mark = boolean('AND', boolean('NOT', _sock(smooth.outputs, "Smooth")), out["Use Sharp Edges"])
    # The convex / concave filter applies to edges found either way (Smooth by Angle
    # marks every hard edge sharp, so filtering only the angle test let concave edges
    # through).
    hard = boolean('AND', boolean('AND', boolean('OR', _sock(steep.outputs, "Result"), by_mark), side_ok),
                   _sock(two.outputs, "Result"))

    # The edges that get decals are made sharp on the object itself (inside the
    # modifier), so its shading breaks there whatever smoothing it uses (Shade Smooth,
    # Shade Auto Smooth...). The decal normals are read from this version.
    sharpened = b.node('GeometryNodeSetShadeSmooth', domain='EDGE')
    b.link(geo, _sock(sharpened.inputs, "Mesh"))
    b.link(hard, _sock(sharpened.inputs, "Selection"))
    _sock(sharpened.inputs, "Shade Smooth").default_value = False
    geo_sharp = _sock(sharpened.outputs, "Mesh")

    # Mitres: where the next edge of the same face is hard too, the strip's inner side
    # is cut back by width / tan(corner angle / 2), so both strips meet on the bisector
    # instead of overlapping (and flickering).
    def sample(domain, data_type, value, index, source=None):
        n = b.node('GeometryNodeSampleIndex', domain=domain, data_type=data_type)
        b.link(source if source is not None else geo, _sock(n.inputs, "Geometry"))
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

    def end_shift(V, V_other, other_edge, face_normal, at_a):
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
        sin = math_op('MAXIMUM', math_op('SQRT', math_op('SUBTRACT', 1.0, math_op('MULTIPLY', cos, cos))), 0.05)
        # The other edge of this face at the vertex decides where the strip's inner
        # side ends:
        #  - hard: another strip lies on this face too -> meet on the bisector,
        #    cut back by width * cot(angle / 2) = width * (1 + cos) / sin;
        #  - smooth: the strip carries on, on the next face -> end exactly on that edge,
        #    width * cot(angle) = width * cos / sin (0 when square, negative = longer),
        #    so strips on faces of a rounded surface meet on the edge between them.
        other_hard = sample('EDGE', 'BOOLEAN', hard, other_edge)
        mitre = math_op('DIVIDE', math_op('MULTIPLY', out["Width"], math_op('ADD', 1.0, cos)), sin)
        along = math_op('DIVIDE', math_op('MULTIPLY', out["Width"], cos), sin)
        # A reflex corner of the face (an inside corner of a concave n-gon, > 180
        # degrees): the inner side has to be extended, not cut back. The face's winding
        # says which: at its first corner a convex turn has (t x u) along the normal,
        # at the second corner against it.
        turn = b.node('ShaderNodeVectorMath', operation='CROSS_PRODUCT')
        b.link(t, turn.inputs[0])
        b.link(u, turn.inputs[1])
        side = b.node('ShaderNodeVectorMath', operation='DOT_PRODUCT')
        b.link(_sock(turn.outputs, "Vector"), side.inputs[0])
        b.link(face_normal, side.inputs[1])
        signed = math_op('MULTIPLY', _sock(side.outputs, "Value"), 1.0 if at_a else -1.0)
        convex_sign = b.switch('FLOAT', math_op('GREATER_THAN', signed, -1e-6), -1.0, 1.0)
        mitre = math_op('MULTIPLY', mitre, convex_sign)
        along = math_op('MULTIPLY', along, convex_sign)
        shift = b.switch('FLOAT', other_hard, along, mitre)
        return math_op('MAXIMUM', shift, math_op('MULTIPLY', out["Width"], -2.0)), other_hard

    b.column(-1050)
    shifts, smooth_normals, mitred, inwards = [], [], [], []
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
        # Which side of the edge the face is on: the face runs V_a -> V_b along this
        # edge, so it lies to the left (normal x edge direction). Exact for any polygon;
        # the face's centre is not (in a concave n-gon, like the C-shaped faces booleans
        # leave, it can sit across the edge and the strip would hang over the hole).
        inward = b.node('ShaderNodeVectorMath', operation='CROSS_PRODUCT')
        b.link(sides[k][0], inward.inputs[0])
        b.link(b.vec('SUBTRACT', V_b, V_a), inward.inputs[1])
        inwards.append(normalize(_sock(inward.outputs, "Vector")))
        eoc_a = b.node('GeometryNodeEdgesOfCorner')
        b.link(c_a, _sock(eoc_a.inputs, "Corner Index"))
        eoc_b = b.node('GeometryNodeEdgesOfCorner')
        b.link(c_b, _sock(eoc_b.inputs, "Corner Index"))
        shift_a, hard_a = end_shift(V_a, V_b, _sock(eoc_a.outputs, "Previous Edge Index"), sides[k][0], True)
        shift_b, hard_b = end_shift(V_b, V_a, _sock(eoc_b.outputs, "Next Edge Index"), sides[k][0], False)
        # which end of the edge (vertex 1 / vertex 2) corner a sits on
        dot = b.node('ShaderNodeVectorMath', operation='DOT_PRODUCT')
        b.link(b.vec('SUBTRACT', V_a, mid), dot.inputs[0])
        b.link(direction, dot.inputs[1])
        a_is_1 = math_op('LESS_THAN', _sock(dot.outputs, "Value"), 0.0)
        shifts.append((b.switch('FLOAT', a_is_1, shift_b, shift_a), b.switch('FLOAT', a_is_1, shift_a, shift_b)))
        mitred.append((b.switch('BOOLEAN', a_is_1, hard_b, hard_a), b.switch('BOOLEAN', a_is_1, hard_a, hard_b)))
        # The mesh's own normals at both ends on this face (corner normals: smooth
        # across smooth edges), so strips follow smooth shading instead of faceting it.
        corner_normals = []
        for c in (c_a, c_b):
            nn = b.node('GeometryNodeInputNormal')
            corner_normals.append(sample('CORNER', 'FLOAT_VECTOR', nn.outputs[0], c, geo_sharp))
        n_ca, n_cb = corner_normals
        smooth_normals.append((b.switch('VECTOR', a_is_1, n_cb, n_ca), b.switch('VECTOR', a_is_1, n_ca, n_cb)))

    # Edge data onto the mesh (a copy for the decals), then one point per hard edge
    b.column(-900)
    g = geo
    for name, value in (("gt_ed_dir", direction), ("gt_ed_n0", sides[0][0]), ("gt_ed_d0", inwards[0]),
                        ("gt_ed_n1", sides[1][0]), ("gt_ed_d1", inwards[1]), ("gt_ed_ne", edge_normal)):
        g = store(g, name, value, 'EDGE')
    g = store(g, "gt_ed_len", length, 'EDGE', 'FLOAT')
    for k in (0, 1):
        # Clamp overlap: if the cuts at both ends don't fit in the edge (a short edge,
        # a wide strip), the strip is narrowed so they just do, instead of folding over.
        cut = math_op('ADD', math_op('MAXIMUM', shifts[k][0], 0.0), math_op('MAXIMUM', shifts[k][1], 0.0))
        fit = math_op('MINIMUM', math_op('DIVIDE', length, math_op('MAXIMUM', cut, 1e-9)), 1.0)
        g = store(g, f"gt_ed_f{k}", fit, 'EDGE', 'FLOAT')
        g = store(g, f"gt_ed_s1{k}", math_op('MULTIPLY', shifts[k][0], fit), 'EDGE', 'FLOAT')
        g = store(g, f"gt_ed_s2{k}", math_op('MULTIPLY', shifts[k][1], fit), 'EDGE', 'FLOAT')
        g = store(g, f"gt_ed_m{k}1", smooth_normals[k][0], 'EDGE')
        g = store(g, f"gt_ed_m{k}2", smooth_normals[k][1], 'EDGE')
    # At the edge itself both strips share this normal: the average of their two faces'
    # normals there, or, where the end is mitred against another hard edge (a corner
    # where three faces meet), the vertex normal (average of all the faces around it),
    # which every strip reaching that corner shares, so the mitres don't crease.
    edge_verts = b.node('GeometryNodeInputMeshEdgeVertices')
    for end in (1, 2):
        vn_src = b.node('GeometryNodeInputNormal')
        vertex_normal = sample('POINT', 'FLOAT_VECTOR', vn_src.outputs[0],
                               _sock(edge_verts.outputs, f"Vertex Index {end}"), geo_sharp)
        pair = normalize(b.vec('ADD', smooth_normals[0][end - 1], smooth_normals[1][end - 1]))
        corner = boolean('OR', mitred[0][end - 1], mitred[1][end - 1])
        g = store(g, f"gt_ed_p{end}", pair, 'EDGE')
        g = store(g, f"gt_ed_e{end}", b.switch('VECTOR', corner, pair, normalize(vertex_normal)), 'EDGE')
    m2p = b.node('GeometryNodeMeshToPoints', mode='EDGES')
    b.link(g, _sock(m2p.inputs, "Mesh"))
    b.link(hard, _sock(m2p.inputs, "Selection"))
    points = _sock(m2p.outputs, "Points")

    # A unit strip: X along the edge (-0.5..0.5), Y inwards (0..1); gt_ed_t = Y
    b.column(-600)
    grid = b.node('GeometryNodeMeshGrid')
    for name, val in (("Size X", 1.0), ("Size Y", 1.0), ("Vertices Y", 2)):
        _sock(grid.inputs, name).default_value = val
    # Columns along the edge: smooth = 4 (two of them get placed one width in from
    # each end, so a corner's rounding stays near the corner); worn = Wear Detail.
    worn = b.node('FunctionNodeCompare', data_type='FLOAT', operation='GREATER_THAN')
    b.link(out["Wear"], _sock(worn.inputs, "A", 'VALUE'))
    _sock(worn.inputs, "B", 'VALUE').default_value = 0.0
    is_worn = _sock(worn.outputs, "Result")
    b.link(b.switch('INT', is_worn, 4, out["Wear Detail"]), _sock(grid.inputs, "Vertices X"))
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
        pts = store(pts, "gt_ed_ff", named(f"gt_ed_f{k}", 'FLOAT'), 'POINT', 'FLOAT')
        pts = store(pts, "gt_ed_mf1", named(f"gt_ed_m{k}1"), 'POINT')
        pts = store(pts, "gt_ed_mf2", named(f"gt_ed_m{k}2"), 'POINT')
        pts = store(pts, "gt_ed_df", named(f"gt_ed_d{k}"), 'POINT')
        rot = b.node('FunctionNodeAxesToRotation', primary_axis='X', secondary_axis='Y')
        b.link(named("gt_ed_dir"), _sock(rot.inputs, "Primary Axis"))
        b.link(named(f"gt_ed_d{k}"), _sock(rot.inputs, "Secondary Axis"))
        scale = b.node('ShaderNodeCombineXYZ')
        b.link(named("gt_ed_len", 'FLOAT'), scale.inputs[0])
        b.link(math_op('MULTIPLY', out["Width"], named(f"gt_ed_f{k}", 'FLOAT')), scale.inputs[1])
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
    t_in = named("gt_ed_t", 'FLOAT')        # 0 at the edge, 1 at the inner side
    length_p = named("gt_ed_len", 'FLOAT')
    u = math_op('ADD', named("gt_ed_x", 'FLOAT'), 0.5)  # 0..1 along the edge (vertex 1 -> 2)
    # Smooth: the 4 columns go to 0, a, 1 - a, 1 with a = width / length (at most
    # half), so a corner's rounding only bends the last width of the strip, like a
    # real rounded box. Worn: columns stay evenly spread (for the noise).
    a = math_op('MINIMUM', math_op('DIVIDE', out["Width"], math_op('MAXIMUM', length_p, 1e-6)), 0.5)
    seg1 = math_op('MULTIPLY', math_op('MULTIPLY', u, 3.0), a)
    seg2 = math_op('ADD', a, math_op('MULTIPLY', math_op('MULTIPLY', math_op('SUBTRACT', u, 1.0 / 3.0), 3.0),
                                     math_op('SUBTRACT', 1.0, math_op('MULTIPLY', a, 2.0))))
    seg3 = math_op('ADD', math_op('SUBTRACT', 1.0, a),
                   math_op('MULTIPLY', math_op('MULTIPLY', math_op('SUBTRACT', u, 2.0 / 3.0), 3.0), a))
    lo = math_op('LESS_THAN', u, 1.0 / 3.0 + 1e-4)
    hi = math_op('GREATER_THAN', u, 2.0 / 3.0 - 1e-4)
    mapped = b.switch('FLOAT', lo, b.switch('FLOAT', hi, seg2, seg3), seg1)
    s_par = b.switch('FLOAT', is_worn, mapped, u)
    shift_along = math_op('MULTIPLY', math_op('SUBTRACT', s_par, u), length_p)
    # Normals: at the edge, the average of both faces' (smooth) normals, turning into
    # the corner's vertex normal within one width of a mitred end; at the inner side,
    # this face's normal. Neighbouring strips share them: no steps or creases.
    p1, p2 = named("gt_ed_p1"), named("gt_ed_p2")
    e1, e2 = named("gt_ed_e1"), named("gt_ed_e2")
    reach = math_op('MAXIMUM', out["Width"], 1e-6)
    w_a = math_op('MAXIMUM', math_op('SUBTRACT', 1.0, math_op('DIVIDE', math_op('MULTIPLY', s_par, length_p), reach)), 0.0)
    w_b = math_op('MAXIMUM', math_op('SUBTRACT', 1.0, math_op('DIVIDE', math_op('MULTIPLY', math_op('SUBTRACT', 1.0, s_par), length_p), reach)), 0.0)

    def scaled(v, f):
        n = b.node('ShaderNodeVectorMath', operation='SCALE')
        b.link(v, n.inputs[0])
        b._set(_sock(n.inputs, "Scale"), f)
        return _sock(n.outputs, "Vector")

    n_edge = normalize(b.vec('ADD', b.lerp(p1, p2, s_par),
                             b.vec('ADD', scaled(b.vec('SUBTRACT', e1, p1), w_a), scaled(b.vec('SUBTRACT', e2, p2), w_b))))
    n_face = normalize(b.lerp(named("gt_ed_mf1"), named("gt_ed_mf2"), s_par))
    nrm_smooth = normalize(b.lerp(n_edge, n_face, t_in))
    # Wear (concrete-like): noise on the surface bites into the strip (narrower in
    # chips) and roughens its normals near the edge, fading out towards the face.
    noise = b.node('ShaderNodeTexNoise')
    if hasattr(noise, "noise_dimensions"):
        noise.noise_dimensions = '3D'
    nposition = b.node('GeometryNodeInputPosition')
    b.link(nposition.outputs[0], _sock(noise.inputs, "Vector"))
    b.link(out["Wear Scale"], _sock(noise.inputs, "Scale"))
    _sock(noise.inputs, "Detail").default_value = 6.0
    _sock(noise.inputs, "Roughness").default_value = 0.65
    fac = _sock(noise.outputs, "Factor")
    sharp = out["Wear Sharpness"]
    soft_chip = math_op('MINIMUM', math_op('MAXIMUM', math_op('MULTIPLY', math_op('SUBTRACT', fac, 0.42), 3.0), 0.0), 0.9)
    # Sharp chips: cells (Voronoi) are the broken-off pieces, each with its own depth
    # (or none) and its own tilt, so the chips have crisp edges and flat facets. The
    # soft noise wobbles the cells a little so their outlines aren't too regular.
    cells = b.node('ShaderNodeTexVoronoi')
    if hasattr(cells, "voronoi_dimensions"):
        cells.voronoi_dimensions = '3D'
    cells.feature = 'F1'
    wobble = scaled(b.vec('SUBTRACT', _sock(noise.outputs, "Color"), (0.5, 0.5, 0.5)),
                    math_op('DIVIDE', 0.6, math_op('MAXIMUM', out["Wear Scale"], 0.01)))
    b.link(b.vec('ADD', nposition.outputs[0], wobble), _sock(cells.inputs, "Vector"))
    b.link(out["Wear Scale"], _sock(cells.inputs, "Scale"))
    _sock(cells.inputs, "Randomness").default_value = 1.0
    cell_rgb = b.node('ShaderNodeSeparateXYZ')
    b.link(_sock(cells.outputs, "Color"), cell_rgb.inputs[0])
    cell_r, cell_g = cell_rgb.outputs[0], cell_rgb.outputs[1]
    broken = math_op('GREATER_THAN', cell_r, 0.6)                      # this piece is chipped
    depth = math_op('ADD', 0.3, math_op('MULTIPLY', cell_g, 0.6))      # how deep
    sharp_chip = math_op('MULTIPLY', broken, depth)
    chip = math_op('MULTIPLY', math_op('ADD', soft_chip, math_op('MULTIPLY', math_op('SUBTRACT', sharp_chip, soft_chip), sharp)),
                   out["Wear"])
    strip_width = math_op('MULTIPLY', out["Width"], named("gt_ed_ff", 'FLOAT'))
    bite = scaled(named("gt_ed_df"), math_op('MULTIPLY', math_op('MULTIPLY', strip_width, chip), math_op('MULTIPLY', t_in, -1.0)))
    soft_tilt = b.vec('SUBTRACT', _sock(noise.outputs, "Color"), (0.5, 0.5, 0.5))
    facet_tilt = b.vec('SUBTRACT', _sock(cells.outputs, "Color"), (0.5, 0.5, 0.5))
    tilt = b.lerp(soft_tilt, facet_tilt, sharp)
    # soft: fades out towards the face; sharp: a chip's facet keeps its tilt further in
    fade = math_op('SUBTRACT', 1.0, math_op('MULTIPLY', t_in, math_op('SUBTRACT', 1.0, math_op('MULTIPLY', sharp, 0.5))))
    rough = scaled(tilt, math_op('MULTIPLY', math_op('MULTIPLY', out["Wear"], 1.2), fade))
    nrm = normalize(b.vec('ADD', nrm_smooth, rough))
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
    # mitre: the inner side runs from the vertex-1 corner (slid forwards by sa) to the
    # vertex-2 corner (slid back by sb); the columns move to their place along the edge
    sa = named("gt_ed_sa", 'FLOAT')
    sb_back = math_op('MULTIPLY', named("gt_ed_sb", 'FLOAT'), -1.0)
    slide = math_op('MULTIPLY', math_op('ADD', sa, math_op('MULTIPLY', math_op('SUBTRACT', sb_back, sa), s_par)), t_in)
    # a chip pulls the inner side straight back towards the edge point it starts
    # from (the mitre slide shrinks with it), so near a corner it stays inside its own
    # mitre instead of crossing into the next strip
    slide = math_op('MULTIPLY', slide, math_op('SUBTRACT', 1.0, chip))
    along = scaled(named("gt_ed_dir"), math_op('ADD', slide, shift_along))
    setpos = b.node('GeometryNodeSetPosition')
    b.link(join.outputs[0], _sock(setpos.inputs, "Geometry"))
    b.link(b.vec('ADD', b.vec('ADD', lift_v, along), bite), _sock(setpos.inputs, "Offset"))
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
    b.link(geo_sharp, final.inputs[0])
    gout = b.node('NodeGroupOutput')
    b.link(final.outputs[0], gout.inputs[0])
    ng[VERSION_KEY] = GROUP_VERSION
    return ng


# Bump GROUP_VERSION whenever the node tree changes: files saved with an older group
# get it rebuilt and swapped in automatically (load, add-on enable, running the tool),
# otherwise they would keep the old tree forever (it is reused by name).
GROUP_VERSION = 10
# Inputs whose saved value is dropped when upgrading from older versions (they had
# no effect there, so the new default applies): {input name: first version it worked}.
_RESET_BEFORE = {"Concave Edges": 4}
VERSION_KEY = "gt_edge_decal_version"


def _is_edge_decal_group(ng):
    return ng.bl_idname == 'GeometryNodeTree' and ng.name.startswith("GT_EdgeDecal")


def get_node_group():
    """The current GT_EdgeDecal group: reused if up to date, otherwise built fresh and
    swapped in for every older Edge Decal group in the file (modifier settings kept:
    the inputs keep their identifiers)."""
    ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if ng is not None and _is_edge_decal_group(ng) and ng.get(VERSION_KEY, 0) >= GROUP_VERSION:
        return ng
    olds = [g for g in bpy.data.node_groups if _is_edge_decal_group(g) and g.get(VERSION_KEY, 0) < GROUP_VERSION]
    for old in olds:
        old.name = old.name + "_outdated"
    new = _build_node_group()
    new.name = NODE_GROUP_NAME
    for old in olds:
        _swap_group(old, new)
    return new


def _swap_group(old, new):
    """Point every modifier using `old` to `new`, keeping their input values."""
    for obj in bpy.data.objects:
        for mod in getattr(obj, "modifiers", []):
            if mod.type != 'NODES' or mod.node_group is not old:
                continue
            values = {}
            old_version = old.get(VERSION_KEY, 0)
            for item in old.interface.items_tree:
                if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.socket_type != 'NodeSocketGeometry':
                    if old_version < _RESET_BEFORE.get(item.name, 0):
                        # the modifier would keep its stored value by itself: set the new default
                        new_item = next((i for i in new.interface.items_tree if i.item_type == 'SOCKET'
                                         and i.in_out == 'INPUT' and i.name == item.name), None)
                        if new_item is not None:
                            values[item.name] = new_item.default_value
                        continue
                    try:
                        values[item.name] = _get_input(mod, item.identifier)
                    except (KeyError, AttributeError, TypeError):
                        pass
            mod.node_group = new
            for name, value in values.items():
                try:
                    _set_input(mod, _input_id(new, name), value)
                except (KeyError, AttributeError, TypeError):
                    pass
    if old.users == 0:
        bpy.data.node_groups.remove(old)


def upgrade_file():
    """Swap outdated Edge Decal groups in the open file (no-op if there are none)."""
    if any(_is_edge_decal_group(g) and g.get(VERSION_KEY, 0) < GROUP_VERSION for g in bpy.data.node_groups):
        get_node_group()


@bpy.app.handlers.persistent
def _on_load(_dummy):
    upgrade_file()


def _deferred_upgrade():
    try:
        upgrade_file()
    except Exception as ex:  # never break enabling the add-on
        print(f"G-Tools Edge Decal upgrade: {ex}")
    return None


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
        added = updated = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            mod = next((m for m in obj.modifiers if m.type == 'NODES' and m.node_group is not None
                        and m.node_group.name.startswith("GT_EdgeDecal")), None)
            if mod is not None:
                if mod.node_group is not ng:  # made by an older version: same settings, new group
                    values = {}
                    old = mod.node_group
                    for name in ("Width", "Angle", "Use Sharp Edges", "Concave Edges", "Offset"):
                        try:
                            values[name] = _get_input(mod, _input_id(old, name))
                        except KeyError:
                            pass
                    mod.node_group = ng
                    for name, value in values.items():
                        _set_input(mod, _input_id(ng, name), value)
                    updated += 1
                _put_last(obj, mod)
                continue
            mod = obj.modifiers.new(MODIFIER_NAME, 'NODES')
            mod.node_group = ng
            size = max(obj.dimensions) or 1.0
            _set_input(mod, _input_id(ng, "Width"), size * 0.01)
            _set_input(mod, _input_id(ng, "Offset"), size * 0.0002)
            _put_last(obj, mod)
            added += 1
        if updated:
            self.report({'INFO'}, f"Edge Decal: updated on {updated} object(s)")
        elif not added:
            self.report({'INFO'}, "Edge Decal: the selected objects already have it")
        return {'FINISHED'}


def _get_input(mod, identifier):
    props = getattr(mod, "properties", None)
    socket = getattr(getattr(props, "inputs", None), identifier, None)
    return socket.value if socket is not None else mod[identifier]


def _put_last(obj, mod):
    """Last in the stack, after modifiers pinned to last (Smooth by Angle from Shade
    Auto Smooth): it has to see the object's final shading and edges."""
    if hasattr(mod, "use_pin_to_last"):
        mod.use_pin_to_last = True
    index = obj.modifiers.find(mod.name)
    if index != len(obj.modifiers) - 1:
        obj.modifiers.move(index, len(obj.modifiers) - 1)


def register():
    bpy.utils.register_class(GTOOLS_OT_edge_decal)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    # the file already open (enabling or updating the add-on): bpy.data isn't
    # writable during registration, so do it right after
    bpy.app.timers.register(_deferred_upgrade, first_interval=0.1)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    if bpy.app.timers.is_registered(_deferred_upgrade):
        bpy.app.timers.unregister(_deferred_upgrade)
    bpy.utils.unregister_class(GTOOLS_OT_edge_decal)
