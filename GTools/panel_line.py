"""Panel Line (Object Mode, Boolean pie, under Quick Decal): a DECALmachine-style
panel line, made live from a cutter.

Select the cutter, then the target (active) and run Panel Line. A new object
"<target>_PanelLine" gets a Geometry Nodes modifier (node group GT_PanelLine_strip):

- finds where the cutter meets the target's surface (the boolean's intersection),
- builds its own clean strip of faces along that line (Segments across, Resolution
  points per width along) and drops every vertex onto the closest point of the
  surface, lifted a hair along the surface normal: fast, and it follows corners,
- gives the strip custom normals that fake a V-groove: each side leans towards
  the middle line and blends into the surface at its outer edge,
- each face of the strip takes the material of the target right under it (it
  carries the target's material list), so each side matches its surface. A cutter
  with a material also gives the target a "Panel Material" modifier that splits its
  surface along the cutter and gives the part inside the cutter's material.

Right after making it, the wheel sets its width (Ctrl: groove depth, Shift: segments,
Alt: resolution); Enter or click confirms, Esc cancels. The panel line and the cutter
are parented to the target; move the cutter and the panel line follows.
"""

import bpy
from bpy.props import FloatProperty, IntProperty

from .sharp_subtract import _prepare_cutter

NODE_GROUP = "GT_PanelLine_strip_v4"  # its own strip of faces (fast), materials from the surface under it
MODIFIER = "Panel Line"


def _build_node_group(name):
    """The strip version: its own clean strip of faces along the line (fast, no
    booleans on the result), each vertex dropped onto the target's surface."""
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    if hasattr(ng, "is_modifier"):
        ng.is_modifier = True
    face = ng.interface
    face.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    face.new_socket("Target", in_out='INPUT', socket_type='NodeSocketObject')
    face.new_socket("Cutter", in_out='INPUT', socket_type='NodeSocketObject')
    s = face.new_socket("Width", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 0.02, 0.0
    s = face.new_socket("Depth", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value = 0.7, 0.0, 3.0
    s = face.new_socket("Offset", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 0.001, 0.0
    s = face.new_socket("Segments", in_out='INPUT', socket_type='NodeSocketInt')
    s.default_value, s.min_value, s.max_value = 4, 2, 16
    s = face.new_socket("Resolution", in_out='INPUT', socket_type='NodeSocketInt')
    s.default_value, s.min_value, s.max_value = 2, 1, 8
    face.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    N, L = ng.nodes, ng.links
    gin = N.new('NodeGroupInput')
    gout = N.new('NodeGroupOutput')

    def node(kind, **props):
        n = N.new(kind)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def vmath(op, a, b=None):
        n = node('ShaderNodeVectorMath', operation=op)
        L.new(a, n.inputs[0])
        if b is not None:
            L.new(b, n.inputs[1])
        return n

    def fmath(op, a, b=None, value=None):
        n = node('ShaderNodeMath', operation=op)
        L.new(a, n.inputs[0])
        if b is not None:
            L.new(b, n.inputs[1])
        elif value is not None:
            n.inputs[1].default_value = value
        return n.outputs[0]

    def attr(name, kind='FLOAT_VECTOR'):
        n = node('GeometryNodeInputNamedAttribute', data_type=kind)
        n.inputs["Name"].default_value = name
        return n.outputs["Attribute"]

    def store(geometry, name, value, kind='FLOAT_VECTOR', domain='POINT'):
        n = node('GeometryNodeStoreNamedAttribute', data_type=kind, domain=domain)
        L.new(geometry, n.inputs["Geometry"])
        n.inputs["Name"].default_value = name
        L.new(value, n.inputs["Value"])
        return n.outputs["Geometry"]

    target = node('GeometryNodeObjectInfo', transform_space='RELATIVE')
    L.new(gin.outputs["Target"], target.inputs["Object"])
    cutter = node('GeometryNodeObjectInfo', transform_space='RELATIVE')
    L.new(gin.outputs["Cutter"], cutter.inputs["Object"])
    target_geo = target.outputs["Geometry"]
    target_normal = node('GeometryNodeInputNormal')
    position = node('GeometryNodeInputPosition')

    def sample(value, where):
        s = node('GeometryNodeSampleNearestSurface', data_type='FLOAT_VECTOR')
        L.new(target_geo, s.inputs["Mesh"])
        L.new(value, s.inputs["Value"])
        L.new(where, s.inputs["Sample Position"])
        return s.outputs["Value"]

    # 1. The line: where the cutter meets the surface (the boolean's intersection edges).
    boolean = node('GeometryNodeMeshBoolean', operation='DIFFERENCE', solver='EXACT')
    L.new(target_geo, boolean.inputs["Mesh 1"])
    L.new(cutter.outputs["Geometry"], boolean.inputs["Mesh 2"])
    separate = node('GeometryNodeSeparateGeometry', domain='EDGE')
    L.new(boolean.outputs["Mesh"], separate.inputs["Geometry"])
    L.new(boolean.outputs["Intersecting Edges"], separate.inputs["Selection"])
    merge = node('GeometryNodeMergeByDistance')
    L.new(separate.outputs["Selection"], merge.inputs["Geometry"])
    merge.inputs["Distance"].default_value = 1e-5
    to_curve = node('GeometryNodeMeshToCurve')
    L.new(merge.outputs["Geometry"], to_curve.inputs["Mesh"])
    # Close points along it (Resolution per width), so the strip can follow corners.
    resample = node('GeometryNodeResampleCurve')
    L.new(to_curve.outputs["Curve"], resample.inputs["Curve"])
    resample.inputs["Mode"].default_value = 'Length'
    L.new(fmath('DIVIDE', gin.outputs["Width"], gin.outputs["Resolution"]), resample.inputs["Length"])

    # 2. Along the line: its point, the surface normal there, and the side direction.
    tangent = node('GeometryNodeInputTangent')
    up = vmath('NORMALIZE', sample(target_normal.outputs["Normal"], position.outputs["Position"])).outputs["Vector"]
    side = vmath('NORMALIZE', vmath('CROSS_PRODUCT', tangent.outputs["Tangent"], up).outputs["Vector"]).outputs["Vector"]
    line = store(resample.outputs["Curve"], "gt_side", side)
    line = store(line, "gt_center", position.outputs["Position"])

    # 3. Across the line: a profile from -1 to 1 (stored as gt_u).
    profile_line = node('GeometryNodeCurvePrimitiveLine')
    profile_line.inputs["Start"].default_value = (-1.0, 0.0, 0.0)
    profile_line.inputs["End"].default_value = (1.0, 0.0, 0.0)
    profile = node('GeometryNodeResampleCurve')
    L.new(profile_line.outputs["Curve"], profile.inputs["Curve"])
    profile.inputs["Mode"].default_value = 'Count'
    L.new(fmath('ADD', gin.outputs["Segments"], value=1.0), profile.inputs["Count"])
    separate_x = node('ShaderNodeSeparateXYZ')
    L.new(position.outputs["Position"], separate_x.inputs[0])
    profile_geo = store(profile.outputs["Curve"], "gt_u", separate_x.outputs["X"], kind='FLOAT')
    strip = node('GeometryNodeCurveToMesh')
    L.new(line, strip.inputs["Curve"])
    L.new(profile_geo, strip.inputs["Profile Curve"])

    # 4. Lay it out across the line, then drop every vertex onto the closest point of
    # the surface (the closest point keeps the strip on each face around corners,
    # where straight-down rays would leave it hanging off the edge), lifted along the
    # surface normal right there.
    across = fmath('MULTIPLY', attr("gt_u", 'FLOAT'), fmath('MULTIPLY', gin.outputs["Width"], value=0.5))
    out = vmath('SCALE', attr("gt_side"))
    L.new(across, out.inputs["Scale"])
    flat_pos = vmath('ADD', attr("gt_center"), out.outputs["Vector"]).outputs["Vector"]
    on_surface = sample(position.outputs["Position"], flat_pos)
    surface_up = vmath('NORMALIZE', sample(target_normal.outputs["Normal"], flat_pos)).outputs["Vector"]
    lift = vmath('SCALE', surface_up)
    L.new(gin.outputs["Offset"], lift.inputs["Scale"])
    final_pos = vmath('ADD', on_surface, lift.outputs["Vector"])
    set_pos = node('GeometryNodeSetPosition')
    L.new(strip.outputs["Mesh"], set_pos.inputs["Geometry"])
    L.new(final_pos.outputs["Vector"], set_pos.inputs["Position"])

    # Around convex corners a face spanning both sides cuts under the surface (its
    # middle dips below even though its corners sit on top). Measure how far each
    # face's middle is under the wanted offset and lift its corners by that.
    def corner_fix(geometry):
        center_pos = node('GeometryNodeFieldOnDomain', domain='FACE', data_type='FLOAT_VECTOR')
        L.new(position.outputs["Position"], center_pos.inputs["Value"])
        below = sample(position.outputs["Position"], center_pos.outputs["Value"])
        below_up = vmath('NORMALIZE', sample(target_normal.outputs["Normal"], center_pos.outputs["Value"])).outputs["Vector"]
        height = vmath('DOT_PRODUCT', vmath('SUBTRACT', center_pos.outputs["Value"], below).outputs["Vector"], below_up)
        short = fmath('MAXIMUM', fmath('SUBTRACT', gin.outputs["Offset"], height.outputs["Value"]), value=0.0)
        on_face = node('GeometryNodeFieldOnDomain', domain='FACE', data_type='FLOAT')
        L.new(short, on_face.inputs["Value"])
        on_point = node('GeometryNodeFieldOnDomain', domain='POINT', data_type='FLOAT')
        L.new(on_face.outputs["Value"], on_point.inputs["Value"])
        push = vmath('SCALE', vmath('NORMALIZE', sample(target_normal.outputs["Normal"], position.outputs["Position"])).outputs["Vector"])
        L.new(fmath('MULTIPLY', on_point.outputs["Value"], value=2.0), push.inputs["Scale"])
        n = node('GeometryNodeSetPosition')
        L.new(geometry, n.inputs["Geometry"])
        L.new(push.outputs["Vector"], n.inputs["Offset"])
        return n.outputs["Geometry"]

    lifted = corner_fix(corner_fix(set_pos.outputs["Geometry"]))

    # 5. Fake V-groove: each half leans towards the middle, fading out at the outer edge.
    face_u = node('GeometryNodeFieldOnDomain', domain='FACE', data_type='FLOAT')
    L.new(attr("gt_u", 'FLOAT'), face_u.inputs["Value"])
    sign = fmath('SIGN', face_u.outputs["Value"])
    fade = node('ShaderNodeMath', operation='SUBTRACT')  # 1 - |u|
    fade.inputs[0].default_value = 1.0
    L.new(fmath('ABSOLUTE', attr("gt_u", 'FLOAT')), fade.inputs[1])
    tilt = fmath('MULTIPLY', fmath('MULTIPLY', fade.outputs[0], gin.outputs["Depth"]), sign)
    lean = vmath('SCALE', attr("gt_side"))
    L.new(tilt, lean.inputs["Scale"])
    surface = vmath('NORMALIZE', sample(target_normal.outputs["Normal"], position.outputs["Position"])).outputs["Vector"]
    groove = vmath('NORMALIZE', vmath('SUBTRACT', surface, lean.outputs["Vector"]).outputs["Vector"])
    set_normal = node('GeometryNodeSetMeshNormal', mode='FREE', domain='CORNER')
    L.new(lifted, set_normal.inputs["Mesh"])
    L.new(groove.outputs["Vector"], set_normal.inputs["Custom Normal"])

    # Each face takes the material of the target right under it (the panel object's
    # slots mirror the target's materials), so each side of the line matches the
    # surface on that side, whatever it is (and the cutter's material inside, when
    # the target got a Panel Material modifier from it).
    center = node('GeometryNodeFieldOnDomain', domain='FACE', data_type='FLOAT_VECTOR')
    L.new(position.outputs["Position"], center.inputs["Value"])
    target_index = node('GeometryNodeInputNamedAttribute', data_type='INT')
    target_index.inputs["Name"].default_value = "material_index"
    under = node('GeometryNodeSampleNearestSurface', data_type='INT')
    L.new(target_geo, under.inputs["Mesh"])
    L.new(target_index.outputs["Attribute"], under.inputs["Value"])
    L.new(center.outputs["Value"], under.inputs["Sample Position"])
    # Carry the target's own material list over (join an empty copy of it), so the
    # sampled indices always point at the right materials, whatever the target's
    # materials are now or later.
    # (one face of it is kept, squashed to a point so it never shows: a mesh with
    # no faces left would drop its materials)
    index = node('GeometryNodeInputIndex')
    not_first = fmath('GREATER_THAN', index.outputs["Index"], value=0.0)
    carrier = node('GeometryNodeDeleteGeometry', domain='FACE')
    L.new(target_geo, carrier.inputs["Geometry"])
    L.new(not_first, carrier.inputs["Selection"])
    squash = node('GeometryNodeSetPosition')
    L.new(carrier.outputs["Geometry"], squash.inputs["Geometry"])
    squash.inputs["Position"].default_value = (0.0, 0.0, 0.0)
    squash_pos = node('ShaderNodeCombineXYZ')
    L.new(squash_pos.outputs[0], squash.inputs["Position"])
    carry = node('GeometryNodeJoinGeometry')
    # (linked so the target's piece comes first: its materials keep their indices)
    L.new(set_normal.outputs["Mesh"], carry.inputs[0])
    L.new(squash.outputs["Geometry"], carry.inputs[0])
    material = node('GeometryNodeSetMaterialIndex')
    L.new(carry.outputs[0], material.inputs["Geometry"])
    L.new(under.outputs["Value"], material.inputs["Material Index"])
    L.new(material.outputs["Geometry"], gout.inputs["Geometry"])
    return ng


MATERIAL_GROUP = "GT_PanelMaterial_v4"
MATERIAL_MODIFIER = "Panel Material"


def _build_material_group():
    """On the target: its surface split exactly along the cutter, the part inside the
    cutter given the cutter's material (the panel line hides the seam)."""
    ng = bpy.data.node_groups.new(MATERIAL_GROUP, 'GeometryNodeTree')
    if hasattr(ng, "is_modifier"):
        ng.is_modifier = True
    face = ng.interface
    face.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    face.new_socket("Cutter", in_out='INPUT', socket_type='NodeSocketObject')
    face.new_socket("Material", in_out='INPUT', socket_type='NodeSocketMaterial')
    face.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    N, L = ng.nodes, ng.links
    gin = N.new('NodeGroupInput')
    gout = N.new('NodeGroupOutput')
    cutter = N.new('GeometryNodeObjectInfo')
    cutter.transform_space = 'RELATIVE'
    L.new(gin.outputs["Cutter"], cutter.inputs["Object"])

    # Tag the target's own faces: after the booleans, the faces still tagged are the
    # target's surface; the cutter's walls inside it come out untagged. (Telling them
    # apart by distance to the surface drops faces and leaves holes.)
    tag = N.new('GeometryNodeStoreNamedAttribute')
    tag.data_type, tag.domain = 'BOOLEAN', 'FACE'
    L.new(gin.outputs["Geometry"], tag.inputs["Geometry"])
    tag.inputs["Name"].default_value = "gt_target_face"
    tag.inputs["Value"].default_value = True
    tagged = tag.outputs["Geometry"]
    is_target = N.new('GeometryNodeInputNamedAttribute')
    is_target.data_type = 'BOOLEAN'
    is_target.inputs["Name"].default_value = "gt_target_face"

    def surface_only(geometry):
        """Only the faces from the target (not the cutter's walls inside it)."""
        sep = N.new('GeometryNodeSeparateGeometry')
        sep.domain = 'FACE'
        L.new(geometry, sep.inputs["Geometry"])
        L.new(is_target.outputs["Attribute"], sep.inputs["Selection"])
        clean = N.new('GeometryNodeRemoveAttribute')
        L.new(sep.outputs["Selection"], clean.inputs["Geometry"])
        clean.inputs["Name"].default_value = "gt_target_face"
        return clean.outputs["Geometry"]

    inside = N.new('GeometryNodeMeshBoolean')
    inside.operation, inside.solver = 'INTERSECT', 'EXACT'
    inside.inputs["Hole Tolerant"].default_value = True  # open / non-closed targets too
    inside.inputs["Self Intersection"].default_value = True  # and ones that cross themselves
    L.new(tagged, inside.inputs["Mesh 2"])
    L.new(cutter.outputs["Geometry"], inside.inputs["Mesh 2"])
    outside = N.new('GeometryNodeMeshBoolean')
    outside.operation, outside.solver = 'DIFFERENCE', 'EXACT'
    outside.inputs["Hole Tolerant"].default_value = True
    outside.inputs["Self Intersection"].default_value = True
    L.new(tagged, outside.inputs["Mesh 1"])
    L.new(cutter.outputs["Geometry"], outside.inputs["Mesh 2"])
    paint = N.new('GeometryNodeSetMaterial')
    L.new(surface_only(inside.outputs["Mesh"]), paint.inputs["Geometry"])
    L.new(gin.outputs["Material"], paint.inputs["Material"])
    join = N.new('GeometryNodeJoinGeometry')
    L.new(surface_only(outside.outputs["Mesh"]), join.inputs[0])
    L.new(paint.outputs["Geometry"], join.inputs[0])
    merge = N.new('GeometryNodeMergeByDistance')
    L.new(join.outputs[0], merge.inputs["Geometry"])
    merge.inputs["Distance"].default_value = 1e-6
    L.new(merge.outputs["Geometry"], gout.inputs["Geometry"])
    return ng


def get_material_group():
    ng = bpy.data.node_groups.get(MATERIAL_GROUP)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree':
        return ng
    return _build_material_group()


def _first_material(obj):
    for slot in getattr(obj, "material_slots", []):
        if slot.material is not None:
            return slot.material
    return None


def get_node_group():
    ng = bpy.data.node_groups.get(NODE_GROUP)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree':
        return ng
    return _build_node_group(NODE_GROUP)


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


def make_panel_line(context, cutter, target, width, depth, offset, segments=4, resolution=2):
    mesh = bpy.data.meshes.new(f"{target.name}_PanelLine")
    obj = bpy.data.objects.new(mesh.name, mesh)
    for collection in target.users_collection:
        collection.objects.link(obj)
    obj.parent = target
    obj.matrix_parent_inverse.identity()  # same space as the target
    # A cutter with a material paints the target with it inside the panel.
    cutter_material = _first_material(cutter)
    if cutter_material is not None:
        # ...and the target itself takes the cutter's material inside the panel.
        paint = target.modifiers.new(MATERIAL_MODIFIER, 'NODES')
        paint.node_group = get_material_group()
        _set(paint, "Cutter", cutter)
        _set(paint, "Material", cutter_material)
        obj["gt_panel_material"] = paint.name  # so cancelling can take it away again
    mod = obj.modifiers.new(MODIFIER, 'NODES')
    mod.node_group = get_node_group()
    size = max(target.dimensions) or 1.0
    scale = max(target.matrix_world.to_scale()) or 1.0
    _set(mod, "Target", target)
    _set(mod, "Cutter", cutter)
    _set(mod, "Width", width * size / scale)
    _set(mod, "Depth", depth)
    _set(mod, "Offset", offset * size / scale)
    _set(mod, "Resolution", resolution)
    _set(mod, "Segments", segments)
    # The cutter is just a handle now: a wire you can grab, not rendered. It is
    # parented to the target (keeping its place), so moving the target carries the
    # cutter and the panel line along.
    if cutter.parent is not target:
        world = cutter.matrix_world.copy()
        cutter.parent = target
        cutter.matrix_parent_inverse = target.matrix_world.inverted()
        cutter.matrix_world = world
    # Into the Cutters collection like every other cutter (wire, not rendered, named
    # <target>_Cutter_N), so Toggle Cutters and Modifier Scroll treat it the same.
    _prepare_cutter(context, target, cutter)
    # The panel line is a fake detail on the surface: it shouldn't cast shadows.
    obj.visible_shadow = False
    return obj


class GTOOLS_OT_panel_line(bpy.types.Operator):
    """Make a live panel line where the selected cutter meets the active object (move the cutter to move it)"""
    bl_idname = "object.gtools_panel_line"
    bl_label = "Panel Line"
    bl_options = {'REGISTER', 'UNDO'}

    width: FloatProperty(name="Width", description="Panel line width (fraction of the target's size)",
                         default=0.012, min=0.0005, soft_max=0.1, precision=4, step=0.05, options={'SKIP_SAVE'})
    depth: FloatProperty(name="Depth", description="How deep the groove looks (how much the normals lean in)",
                         default=0.7, min=0.0, soft_max=3.0, options={'SKIP_SAVE'})
    segments: IntProperty(name="Segments", description="Faces across the panel line",
                          default=4, min=2, max=16, options={'SKIP_SAVE'})
    resolution: IntProperty(name="Resolution", description="Points along the line per width (follows "
                                                           "curves and corners more closely)",
                            default=2, min=1, max=8, options={'SKIP_SAVE'})
    offset: FloatProperty(name="Offset", description="Gap above the surface (fraction of the target's size)",
                          default=0.0005, min=0.0, soft_max=0.01, precision=4, step=0.01, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'OBJECT' and obj is not None and obj.type == 'MESH'
                and any(o is not obj and o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        target = context.active_object
        cutters = [o for o in context.selected_objects if o is not target and o.type == 'MESH']
        if not cutters:
            self.report({'WARNING'}, "Panel Line: select the cutter(s), then the target")
            return {'CANCELLED'}
        self._cutter_state = {c: (c.display_type, c.hide_render, c.parent, c.matrix_world.copy(),
                                  c.name, list(c.users_collection))
                              for c in cutters}
        self._target = target
        made = [make_panel_line(context, c, target, self.width, self.depth, self.offset, self.segments,
                                self.resolution)
                for c in cutters]
        self._made = made
        for o in context.selected_objects:
            o.select_set(False)
        for c in cutters:  # ready to move the cutter around
            c.select_set(True)
        context.view_layer.objects.active = cutters[-1]
        self.report({'INFO'}, f"Panel Line: {len(made)} made on {target.name}; move the cutter to move it")
        return {'FINISHED'}


def _draw_hud(op, context):
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    from .sharp_subtract import hint_panel_origin
    gpu.state.blend_set('ALPHA')
    title = (f"Panel Line  ·  width {op.width:.4f}  ·  depth {op.depth:.2f}  ·  segments {op.segments}"
             f"  ·  resolution {op.resolution}")
    hints = [
        ("Wheel  (or + / -)", "width"),
        ("Ctrl Wheel", "depth (how deep the groove looks)"),
        ("Shift Wheel", "segments across"),
        ("Alt Wheel", "resolution along the line"),
        ("Enter / Click", "confirm"),
        ("Esc / Right click", "cancel"),
    ]
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


def _panel_invoke(self, context, event):
    """Make the panel line, then let the wheel set its width before confirming."""
    result = self.execute(context)
    if result != {'FINISHED'} or context.area is None or context.area.type != 'VIEW_3D':
        return result
    self._handle = bpy.types.SpaceView3D.draw_handler_add(_draw_hud, (self, context), 'WINDOW', 'POST_PIXEL')
    context.window_manager.modal_handler_add(self)
    context.workspace.status_text_set("Panel Line: wheel: width  |  Ctrl wheel: depth  |  Shift wheel: segments  |  Alt wheel: resolution  |  "
                                      "Enter / click: confirm  |  Esc: cancel")
    return {'RUNNING_MODAL'}


def _panel_apply(self):
    target = self._target
    size = max(target.dimensions) or 1.0
    scale = max(target.matrix_world.to_scale()) or 1.0
    for obj in self._made:
        mod = obj.modifiers.get(MODIFIER)
        if mod is None:
            continue
        _set(mod, "Width", self.width * size / scale)
        _set(mod, "Depth", self.depth)
        _set(mod, "Segments", self.segments)
        _set(mod, "Resolution", self.resolution)
        obj.update_tag()  # make sure the viewport re-evaluates it right away


def _panel_end(self, context):
    bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
    context.workspace.status_text_set(None)
    context.area.tag_redraw()


def _panel_modal(self, context, event):
    context.area.tag_redraw()
    up = event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS', 'EQUAL'} and event.value == 'PRESS'
    down = event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS', 'MINUS'} and event.value == 'PRESS'
    if up or down:
        if event.alt:
            self.resolution = max(1, min(8, self.resolution + (1 if up else -1)))
        elif event.shift:
            self.segments = max(2, min(16, self.segments + (1 if up else -1)))
        elif event.ctrl:
            self.depth = max(0.0, self.depth + (0.1 if up else -0.1))
        else:
            self.width = max(0.0005, self.width * (1.15 if up else 1 / 1.15))
        _panel_apply(self)
    elif (event.type in {'RET', 'NUMPAD_ENTER'} or event.type == 'LEFTMOUSE') and event.value == 'PRESS':
        _panel_end(self, context)
        return {'FINISHED'}
    elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
        _panel_end(self, context)
        for obj in self._made:
            paint = self._target.modifiers.get(obj.get("gt_panel_material", ""))
            if paint is not None:
                self._target.modifiers.remove(paint)
            mesh = obj.data
            bpy.data.objects.remove(obj)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for cutter, (display, hide, parent, world, name, collections) in self._cutter_state.items():
            cutter.display_type, cutter.hide_render = display, hide
            cutter.name = name
            for c in list(cutter.users_collection):
                if c not in collections:
                    c.objects.unlink(cutter)
            for c in collections:
                if cutter.name not in c.objects:
                    c.objects.link(cutter)
            if cutter.parent is not parent:
                cutter.parent = parent
                if parent is not None:
                    cutter.matrix_parent_inverse = parent.matrix_world.inverted()
                cutter.matrix_world = world
        for o in context.selected_objects:
            o.select_set(False)
        for cutter in self._cutter_state:
            cutter.select_set(True)
        self._target.select_set(True)
        context.view_layer.objects.active = self._target
        return {'CANCELLED'}
    elif event.type in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
        return {'PASS_THROUGH'}
    return {'RUNNING_MODAL'}


GTOOLS_OT_panel_line.invoke = _panel_invoke
GTOOLS_OT_panel_line.modal = _panel_modal


def register():
    bpy.utils.register_class(GTOOLS_OT_panel_line)


def unregister():
    bpy.utils.unregister_class(GTOOLS_OT_panel_line)
