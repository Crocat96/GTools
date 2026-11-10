"""Quick Decal (Object Mode, Boolean pie): press an object onto another like a decal,
keeping the look of its volume (like Normal Magic / DECALmachine).

Select the decal object(s), then the target (active) and run Quick Decal. Each decal
gets one Geometry Nodes modifier (node group GT_Decal) that:

- squashes it onto the target's surface (Height 0 = completely flat, 1 = its full
  relief kept, following the surface), with a tiny offset against flicker,
- keeps its shading: every face keeps the normal it had before being flattened,
  turned to follow the surface underneath, so a flattened screw still looks round,
- blends the outer border into the target's normals, so the edge disappears into
  the surface: over a number of edge loops from the border inwards (vertex group
  GT_DecalBorder: 1 at the border, fading out inwards).

The modifier is kept last on the decal (pinned), so a Smooth by Angle can't undo its
normals. Every run starts from the default settings.

The decal's "up" is the target's surface normal under it, worked out live in the
node group, so moving or rotating the decal afterwards keeps the normals right. It is
parented to the target so it moves with it. Height, offset and border blend can be
changed in the Adjust Last Operation panel or later in the modifier.
"""

import bmesh
import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty
from mathutils import Vector
from mathutils.geometry import convex_hull_2d

GROUP = "GT_DecalBorder"
MODIFIER = "Decal"
NODE_GROUP = "GT_Decal_v5"  # v5: parallel projection along the decal up


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
    s = face.new_socket("Offset", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 0.001, 0.0
    s = face.new_socket("Height", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value = 0.0, 0.0, 1.0
    s = face.new_socket("Border Blend", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value = 1.0, 0.0, 1.0
    s = face.new_socket("Layering", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value = 0.02, 0.0, 1.0
    s = face.new_socket("Decal Up", in_out='INPUT', socket_type='NodeSocketVector')
    s.default_value = (0.0, 0.0, 1.0)
    face.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    N, L = ng.nodes, ng.links
    gin = N.new('NodeGroupInput')
    gout = N.new('NodeGroupOutput')

    def node(kind, **props):
        n = N.new(kind)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def by_id(sockets, identifier):
        return next(sock for sock in sockets if sock.identifier == identifier)

    def vmath(op, a, b=None, **kw):
        n = node('ShaderNodeVectorMath', operation=op)
        L.new(a, n.inputs[0])
        if b is not None:
            L.new(b, n.inputs[1])
        return n

    def fmath(op, a, b=None, **kw):
        n = node('ShaderNodeMath', operation=op, **kw)
        L.new(a, n.inputs[0])
        if b is not None:
            L.new(b, n.inputs[1])
        return n.outputs[0]

    # The decal's own normals, before anything moves (per face corner: keeps hard edges).
    normal = node('GeometryNodeInputNormal')
    capture = node('GeometryNodeCaptureAttribute', domain='CORNER')
    capture.capture_items.new('VECTOR', "Normal")
    L.new(gin.outputs["Geometry"], capture.inputs["Geometry"])
    L.new(normal.outputs["Normal"], capture.inputs["Value"])
    own_normal = capture.outputs["Attribute"]

    position = node('GeometryNodeInputPosition')
    target = node('GeometryNodeObjectInfo', transform_space='RELATIVE')
    L.new(gin.outputs["Target"], target.inputs["Object"])
    target_normal = node('GeometryNodeInputNormal')

    def sample(value_socket, where):
        s = node('GeometryNodeSampleNearestSurface', data_type='FLOAT_VECTOR')
        L.new(target.outputs["Geometry"], s.inputs["Mesh"])
        L.new(value_socket, s.inputs["Value"])
        L.new(where, s.inputs["Sample Position"])
        return s.outputs["Value"]


    # The decal's "up", worked out live: the surface normal under its middle, in its
    # own space. Rotating or moving the decal keeps everything right.
    bbox = node('GeometryNodeBoundBox')
    L.new(gin.outputs["Geometry"], bbox.inputs["Geometry"])
    middle = vmath('ADD', bbox.outputs["Min"], bbox.outputs["Max"])
    center = vmath('SCALE', middle.outputs["Vector"])
    center.inputs["Scale"].default_value = 0.5
    up = vmath('NORMALIZE', sample(target_normal.outputs["Normal"], center.outputs["Vector"])).outputs["Vector"]

    def rotation_between(a, b):
        """Smallest rotation turning direction a into direction b."""
        axis = vmath('NORMALIZE', vmath('CROSS_PRODUCT', a, b).outputs["Vector"]).outputs["Vector"]
        cos = vmath('DOT_PRODUCT', a, b).outputs["Value"]
        low = node('ShaderNodeMath', operation='MAXIMUM')
        L.new(cos, low.inputs[0])
        low.inputs[1].default_value = -1.0
        high = node('ShaderNodeMath', operation='MINIMUM')
        L.new(low.outputs[0], high.inputs[0])
        high.inputs[1].default_value = 1.0
        rot = node('FunctionNodeAxisAngleToRotation')
        L.new(axis, rot.inputs["Axis"])
        L.new(fmath('ARCCOSINE', high.outputs[0]), rot.inputs["Angle"])
        return rot.outputs["Rotation"]

    # Lay the decal flat on the surface first: turn it (around its middle) so its own
    # up matches the surface's. A decal that sits tilted would otherwise be squashed
    # at an angle and come out oval.
    decal_up = vmath('NORMALIZE', gin.outputs["Decal Up"]).outputs["Vector"]
    lay_flat = rotation_between(decal_up, up)
    rel = vmath('SUBTRACT', position.outputs["Position"], center.outputs["Vector"])
    turned_pos = node('FunctionNodeRotateVector')
    L.new(rel.outputs["Vector"], turned_pos.inputs["Vector"])
    L.new(lay_flat, turned_pos.inputs["Rotation"])
    aligned_pos = vmath('ADD', turned_pos.outputs["Vector"], center.outputs["Vector"])
    align = node('GeometryNodeSetPosition')
    L.new(capture.outputs["Geometry"], align.inputs["Geometry"])
    L.new(aligned_pos.outputs["Vector"], align.inputs["Position"])
    aligned = align.outputs["Geometry"]

    # Where each vertex lands: straight down along the decal's up, all rays parallel
    # (like a projector). Following each vertex's own nearest surface point instead
    # makes the rays fan out or bunch up on curved surfaces, and the decal goes oval.
    # Nearest point is only the fallback for rays that miss.
    diag = vmath('LENGTH', vmath('SUBTRACT', bbox.outputs["Max"], bbox.outputs["Min"]).outputs["Vector"]).outputs["Value"]
    reach = node('ShaderNodeMath', operation='MULTIPLY')  # start the rays this far above
    L.new(diag, reach.inputs[0])
    reach.inputs[1].default_value = 2.0
    above_up = vmath('SCALE', up)
    L.new(reach.outputs[0], above_up.inputs["Scale"])
    start = vmath('ADD', position.outputs["Position"], above_up.outputs["Vector"])
    down = vmath('SCALE', up)
    down.inputs["Scale"].default_value = -1.0
    ray = node('GeometryNodeRaycast', data_type='FLOAT_VECTOR')
    L.new(target.outputs["Geometry"], ray.inputs["Target Geometry"])
    L.new(target_normal.outputs["Normal"], ray.inputs["Attribute"])  # smooth normal at the hit
    L.new(start.outputs["Vector"], ray.inputs["Source Position"])
    L.new(down.outputs["Vector"], ray.inputs["Ray Direction"])
    length = node('ShaderNodeMath', operation='MULTIPLY')
    L.new(diag, length.inputs[0])
    length.inputs[1].default_value = 4.0
    L.new(length.outputs[0], ray.inputs["Ray Length"])

    def pick(hit_value, fallback):
        sw = node('GeometryNodeSwitch', input_type='VECTOR')
        L.new(ray.outputs["Is Hit"], sw.inputs["Switch"])
        L.new(fallback, sw.inputs["False"])
        L.new(hit_value, sw.inputs["True"])
        return sw.outputs[0]

    near_point = sample(position.outputs["Position"], position.outputs["Position"])
    near_normal = sample(target_normal.outputs["Normal"], position.outputs["Position"])
    surface_point = pick(ray.outputs["Hit Position"], near_point)
    surface_normal = vmath('NORMALIZE', pick(ray.outputs["Attribute"], near_normal)).outputs["Vector"]

    # Height of each vertex above the decal's lowest point (along up).
    dot = vmath('DOT_PRODUCT', position.outputs["Position"], up)
    stats = node('GeometryNodeAttributeStatistic', data_type='FLOAT', domain='POINT')
    L.new(aligned, stats.inputs["Geometry"])
    L.new(dot.outputs["Value"], stats.inputs["Attribute"])
    above = fmath('SUBTRACT', dot.outputs["Value"], stats.outputs["Min"])
    # Even "flat", a sliver of the relief stays (Layering), so the faces keep their
    # stacking order: what was on top stays on top instead of fighting the faces under it.
    keep = fmath('MAXIMUM', gin.outputs["Height"], gin.outputs["Layering"])
    kept = node('ShaderNodeMath', operation='MULTIPLY_ADD')  # above * keep + Offset
    L.new(above, kept.inputs[0])
    L.new(keep, kept.inputs[1])
    L.new(gin.outputs["Offset"], kept.inputs[2])
    lift = vmath('SCALE', up)  # parallel lift too, so the layers don't fan out
    L.new(kept.outputs[0], lift.inputs["Scale"])
    new_pos = vmath('ADD', surface_point, lift.outputs["Vector"])
    set_pos = node('GeometryNodeSetPosition')
    L.new(aligned, set_pos.inputs["Geometry"])
    L.new(new_pos.outputs["Vector"], set_pos.inputs["Position"])

    # Turn each own normal by the smallest rotation from up to the surface normal under
    # it (no twist, whatever way the decal is turned).
    axis = vmath('NORMALIZE', vmath('CROSS_PRODUCT', up, surface_normal).outputs["Vector"]).outputs["Vector"]
    cos = vmath('DOT_PRODUCT', up, surface_normal).outputs["Value"]
    low = node('ShaderNodeMath', operation='MAXIMUM')
    L.new(cos, low.inputs[0])
    low.inputs[1].default_value = -1.0
    high = node('ShaderNodeMath', operation='MINIMUM')
    L.new(low.outputs[0], high.inputs[0])
    high.inputs[1].default_value = 1.0
    angle = fmath('ARCCOSINE', high.outputs[0])
    rotation = node('FunctionNodeAxisAngleToRotation')
    L.new(axis, rotation.inputs["Axis"])
    L.new(angle, rotation.inputs["Angle"])
    laid = node('FunctionNodeRotateVector')  # first the lay-flat turn, then the surface's
    L.new(own_normal, laid.inputs["Vector"])
    L.new(lay_flat, laid.inputs["Rotation"])
    turned = node('FunctionNodeRotateVector')
    L.new(laid.outputs["Vector"], turned.inputs["Vector"])
    L.new(rotation.outputs["Rotation"], turned.inputs["Rotation"])

    # Border: blend into the surface normal.
    weight = node('GeometryNodeInputNamedAttribute', data_type='FLOAT')
    weight.inputs["Name"].default_value = GROUP
    factor = fmath('MULTIPLY', weight.outputs["Attribute"], gin.outputs["Border Blend"], use_clamp=True)
    mix = node('ShaderNodeMix', data_type='VECTOR')
    L.new(factor, by_id(mix.inputs, "Factor_Float"))
    L.new(turned.outputs["Vector"], by_id(mix.inputs, "A_Vector"))
    L.new(surface_normal, by_id(mix.inputs, "B_Vector"))
    final = vmath('NORMALIZE', by_id(mix.outputs, "Result_Vector"))

    set_normal = node('GeometryNodeSetMeshNormal', mode='FREE', domain='CORNER')
    L.new(set_pos.outputs["Geometry"], set_normal.inputs["Mesh"])
    L.new(final.outputs["Vector"], set_normal.inputs["Custom Normal"])
    L.new(set_normal.outputs["Mesh"], gout.inputs["Geometry"])
    return ng


def get_node_group():
    ng = bpy.data.node_groups.get(NODE_GROUP)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree':
        return ng
    return _build_node_group()


def _input_id(ng, name):
    for item in ng.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == name:
            return item.identifier
    return None


def _set(mod, name, value):
    identifier = _input_id(mod.node_group, name)
    props = getattr(mod, "properties", None)
    socket = getattr(getattr(props, "inputs", None), identifier, None)
    if socket is not None:
        socket.value = value
    else:
        mod[identifier] = value


# ---------------------------------------------------------------------------
# Decal setup
# ---------------------------------------------------------------------------

def _point_segment(p, a, b):
    ab = b - a
    t = 0.0 if ab.length_squared < 1e-18 else max(0.0, min(1.0, (p - a).dot(ab) / ab.length_squared))
    return (p - (a + ab * t)).length


def _up_and_base(decal, target):
    """The decal's "up" (in its own space): the target's surface normal under it, so
    a closed decal (a screw head with top and bottom) gets the right way up too; and
    the height of its lowest point along it."""
    mesh = decal.data
    up = None
    # From the mesh itself: the bounding box would already be flattened by a decal
    # modifier when Quick Decal runs again on the same object.
    coords = [v.co for v in mesh.vertices]
    if not coords:
        return Vector((0.0, 0.0, 1.0)), 0.0
    lo = Vector([min(c[i] for c in coords) for i in range(3)])
    hi = Vector([max(c[i] for c in coords) for i in range(3)])
    center_world = decal.matrix_world @ ((lo + hi) / 2)
    local = target.matrix_world.inverted() @ center_world
    hit, _location, normal, _index = target.closest_point_on_mesh(local)
    if hit:
        world_normal = (target.matrix_world.inverted().transposed().to_3x3() @ normal)
        up = (decal.matrix_world.inverted().transposed().to_3x3() @ world_normal)
        # The normal must point from the surface towards the decal.
        if (center_world - target.matrix_world @ _location).dot(world_normal) < 0:
            up = -up
    if up is None or up.length < 1e-9:  # fall back on the decal's own facing
        up = Vector()
        for poly in mesh.polygons:
            up += poly.normal * poly.area
    up = up.normalized() if up.length > 1e-9 else Vector((0.0, 0.0, 1.0))
    base = min((v.co.dot(up) for v in mesh.vertices), default=0.0)
    return up, base


def _border_weights(obj, up, loops):
    """1 on the decal's outer border, fading inwards over `loops` edge loops (rings of
    vertices, counted along the mesh's edges). The border is the open edge if the
    mesh has one; for a closed mesh, its outline seen along its up direction."""
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    border = {v for e in bm.edges if e.is_boundary for v in e.verts}
    if not border and bm.verts:
        tangent = up.orthogonal().normalized()
        other = up.cross(tangent).normalized()
        flat = [Vector((v.co.dot(tangent), v.co.dot(other), 0.0)) for v in bm.verts]
        hull = [flat[i] for i in convex_hull_2d([(p.x, p.y) for p in flat])]
        size = max((p - q).length for p in hull for q in hull) if len(hull) > 1 else 1.0
        tolerance = max(size * 0.02, 1e-6)
        edges = list(zip(hull, hull[1:] + hull[:1]))
        border = {v for v, p in zip(bm.verts, flat)
                  if edges and min(_point_segment(p, a, b) for a, b in edges) <= tolerance}
    # Rings: how many edges away from the border each vertex is.
    ring = {v: 0 for v in border}
    frontier = list(border)
    while frontier:
        nxt = []
        for v in frontier:
            for e in v.link_edges:
                w = e.other_vert(v)
                if w not in ring:
                    ring[w] = ring[v] + 1
                    nxt.append(w)
        frontier = nxt
    span = max(loops, 0) + 1
    weights = []
    for v in bm.verts:
        r = ring.get(v)
        if r is None or r >= span:
            weights.append(0.0)
        else:
            t = 1.0 - r / span
            weights.append(t * t * (3 - 2 * t))  # smooth falloff
    bm.free()
    return weights


def _decal_up(mesh, toward):
    """The decal's own up (its thinnest direction, like a screw head's axis), in its
    own space, pointing to the same side as `toward` (away from the surface)."""
    from mathutils import Matrix
    pts = [v.co for v in mesh.vertices]
    if len(pts) < 3:
        return toward.copy()
    center = sum(pts, Vector()) / len(pts)
    cov = Matrix(((0.0,) * 3,) * 3)
    for p in pts:
        d = p - center
        for i in range(3):
            for j in range(3):
                cov[i][j] += d[i] * d[j]

    def dominant(m, start):
        v = start.normalized()
        for _ in range(48):
            nxt = m @ v
            if nxt.length < 1e-12:
                break
            v = nxt.normalized()
        return v

    try:
        axis = dominant(cov.inverted(), Vector((0.3, 0.5, 0.8)))  # smallest spread
    except ValueError:  # flat: the axis is the plane's normal
        first = dominant(cov, Vector((0.3, 0.5, 0.8)))
        rest = cov - Matrix([[first[i] * first[j] * (first @ (cov @ first)) for j in range(3)] for i in range(3)])
        second = dominant(rest, first.orthogonal())
        axis = first.cross(second)
    if axis.length < 1e-9:
        return toward.copy()
    axis.normalize()
    return axis if axis.dot(toward) >= 0 else -axis


def make_decal(context, decal, target, offset, height, loops, blend, parent, layering=0.02):
    for poly in decal.data.polygons:
        poly.use_smooth = True
    decal.visible_shadow = False  # a decal is a surface detail: no shadow of its own
    up, _base = _up_and_base(decal, target)  # only to find a closed decal's outline

    group = decal.vertex_groups.get(GROUP) or decal.vertex_groups.new(name=GROUP)
    for i, w in enumerate(_border_weights(decal, up, loops)):
        group.add([i], w, 'REPLACE')

    # Older Quick Decal modifiers (shrinkwrap + data transfer) are replaced.
    for old in ("Decal Project", "Decal Normals"):
        mod = decal.modifiers.get(old)
        if mod is not None:
            decal.modifiers.remove(mod)

    mod = decal.modifiers.get(MODIFIER)
    if mod is None or mod.type != 'NODES':
        mod = decal.modifiers.new(MODIFIER, 'NODES')
    mod.node_group = get_node_group()
    scale = max(decal.matrix_world.to_scale()) or 1.0
    size = max(target.dimensions) or 1.0
    _set(mod, "Target", target)
    _set(mod, "Offset", offset * size / scale)  # in the decal's own units
    _set(mod, "Height", height)
    _set(mod, "Border Blend", blend)
    _set(mod, "Layering", layering)
    _set(mod, "Decal Up", _decal_up(decal.data, up))
    # Always last (a Smooth by Angle after it would throw its normals away).
    for other in decal.modifiers:
        if other is not mod and getattr(other, "use_pin_to_last", False):
            other.use_pin_to_last = False
    decal.modifiers.move(decal.modifiers.find(mod.name), len(decal.modifiers) - 1)
    if hasattr(mod, "use_pin_to_last"):
        mod.use_pin_to_last = True

    if parent and decal.parent is not target:
        world = decal.matrix_world.copy()
        decal.parent = target
        decal.matrix_parent_inverse = target.matrix_world.inverted()
        decal.matrix_world = world


class GTOOLS_OT_quick_decal(bpy.types.Operator):
    """Press the selected object(s) onto the active one like a decal: flattened, but its volume still shows"""
    bl_idname = "object.gtools_quick_decal"
    bl_label = "Quick Decal"
    bl_options = {'REGISTER', 'UNDO'}

    # SKIP_SAVE: every Quick Decal starts from the defaults, not from the last one's tweaks.
    height: FloatProperty(
        name="Height", description="How much of the decal's relief to keep (0 = completely flat, it still "
                                   "shades like the full volume)",
        default=0.0, min=0.0, max=1.0, subtype='FACTOR', options={'SKIP_SAVE'},
    )
    offset: FloatProperty(
        name="Offset", description="Gap above the surface (fraction of the target's size), so it doesn't flicker",
        default=0.0005, min=0.0, soft_max=0.01, precision=4, step=0.01, options={'SKIP_SAVE'},
    )
    border_loops: IntProperty(
        name="Border Loops", description="How many edge loops in from the outer border the normals blend "
                                         "into the surface (fading out inwards)",
        default=3, min=0, soft_max=12, options={'SKIP_SAVE'},
    )
    blend: FloatProperty(name="Border Blend", description="Strength of the border blend",
                         default=1.0, min=0.0, max=1.0, subtype='FACTOR', options={'SKIP_SAVE'})
    layering: FloatProperty(
        name="Layering", description="A sliver of the relief kept even when flat, so the decal's faces stay "
                                     "stacked in order and don't show through each other",
        default=0.02, min=0.0, max=0.2, subtype='FACTOR', options={'SKIP_SAVE'},
    )
    parent: BoolProperty(name="Parent to Target", default=True, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'OBJECT' and obj is not None and obj.type == 'MESH'
                and any(o is not obj and o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        target = context.active_object
        decals = [o for o in context.selected_objects if o is not target and o.type == 'MESH']
        if not decals:
            self.report({'WARNING'}, "Quick Decal: select the decal(s), then the target")
            return {'CANCELLED'}
        for decal in decals:
            make_decal(context, decal, target, self.offset, self.height, self.border_loops, self.blend,
                       self.parent, self.layering)
        self.report({'INFO'}, f"Quick Decal: {len(decals)} decal(s) on {target.name}")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(GTOOLS_OT_quick_decal)


def unregister():
    bpy.utils.unregister_class(GTOOLS_OT_quick_decal)
