"""Sharp Subtract: live boolean done with Geometry Nodes.

Every operation reuses the same node group (GT_SharpSubtract):
    Group Input -> Mesh Boolean (Difference, Exact) -> Set Shade Smooth (Edge, off) -> Group Output
The intersecting edges of the cut are marked sharp. The cutter is an exposed
"Cutter" object input, so each cutter gets its own modifier on the base object.

Modifiers and cutters use the same naming as the other live booleans
(<base>_Cutter_N), so Toggle Cutters, Smart Delete and Apply Booleans keep working.
"""

import bpy

NODE_GROUP_NAME = "GT_SharpSubtract"
CUTTER_INPUT = "Cutter"


# ---------------------------------------------------------------------------
# Node group
# ---------------------------------------------------------------------------

def _socket(sockets, *names):
    for name in names:
        if name in sockets:
            return sockets[name]
    raise KeyError(f"None of {names} found")


def _object_input(node_group):
    """Identifier of the node group's Object input, or None."""
    for item in node_group.interface.items_tree:
        if (item.item_type == 'SOCKET' and item.in_out == 'INPUT'
                and item.socket_type == 'NodeSocketObject'):
            return item.identifier
    return None


def _build_node_group(name, sharp=True, union=False):
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket(CUTTER_INPUT, in_out='INPUT', socket_type='NodeSocketObject')
    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    if hasattr(ng, "is_modifier"):
        ng.is_modifier = True

    nodes, links = ng.nodes, ng.links

    group_in = nodes.new('NodeGroupInput')
    group_in.location = (-600, 0)

    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.location = (-400, -150)
    obj_info.transform_space = 'RELATIVE'

    boolean = nodes.new('GeometryNodeMeshBoolean')
    boolean.location = (-150, 0)
    boolean.operation = 'UNION' if union else 'DIFFERENCE'
    boolean.solver = 'EXACT'

    group_out = nodes.new('NodeGroupOutput')
    group_out.location = (320, 0)

    # Union takes every mesh in Mesh 2; difference subtracts Mesh 2 from Mesh 1.
    links.new(group_in.outputs[0], _socket(boolean.inputs, "Mesh 2" if union else "Mesh 1"))
    links.new(group_in.outputs[1], _socket(obj_info.inputs, "Object"))
    if union:
        # Fills (concave Edge Bevel) are first clipped to the object's convex hull, so a
        # fill can never stick out past the object's outline (e.g. a cut across a
        # cylinder, where the side curves in at the ends of the edge).
        hull = nodes.new('GeometryNodeConvexHull')
        hull.location = (-400, 150)
        links.new(group_in.outputs[0], hull.inputs[0])
        clip = nodes.new('GeometryNodeMeshBoolean')
        clip.location = (-250, -150)
        clip.operation = 'INTERSECT'
        clip.solver = 'EXACT'
        links.new(_socket(hull.outputs, "Convex Hull"), _socket(clip.inputs, "Mesh 2"))
        links.new(_socket(obj_info.outputs, "Geometry"), _socket(clip.inputs, "Mesh 2"))
        links.new(_socket(clip.outputs, "Mesh"), _socket(boolean.inputs, "Mesh 2"))
    else:
        links.new(_socket(obj_info.outputs, "Geometry"), _socket(boolean.inputs, "Mesh 2"))
    if not sharp:  # plain boolean (used by Edge Bevel, whose rounded cuts must stay smooth)
        links.new(_socket(boolean.outputs, "Mesh"), group_out.inputs[0])
        return ng

    shade = nodes.new('GeometryNodeSetShadeSmooth')
    shade.location = (100, 0)
    shade.domain = 'EDGE'
    _socket(shade.inputs, "Shade Smooth").default_value = False  # off = sharp
    links.new(_socket(boolean.outputs, "Mesh"), _socket(shade.inputs, "Mesh", "Geometry"))
    links.new(_socket(boolean.outputs, "Intersecting Edges"), _socket(shade.inputs, "Selection"))
    links.new(_socket(shade.outputs, "Mesh", "Geometry"), group_out.inputs[0])
    return ng


PLAIN_NODE_GROUP_NAME = "GT_Subtract"
# (new names, so files with the older union groups get the clipped version)
UNION_NODE_GROUP_NAME = "GT_Fill"
SHARP_UNION_NODE_GROUP_NAME = "GT_SharpFill"


def get_node_group(sharp=True, union=False):
    """Reuse the node group if it exists (even if you edited it), otherwise build it:
    GT_SharpSubtract / GT_Subtract (difference, with / without sharp marks on the
    intersection edges), GT_SharpFill / GT_Fill (union clipped to the object's
    convex hull, same)."""
    if union:
        name = SHARP_UNION_NODE_GROUP_NAME if sharp else UNION_NODE_GROUP_NAME
    else:
        name = NODE_GROUP_NAME if sharp else PLAIN_NODE_GROUP_NAME
    ng = bpy.data.node_groups.get(name)
    if ng is not None and ng.bl_idname == 'GeometryNodeTree' and _object_input(ng):
        return ng
    # Missing, or no longer has an Object input: build a fresh one.
    return _build_node_group(name, sharp, union)


# ---------------------------------------------------------------------------
# Cutters
# ---------------------------------------------------------------------------

def _cutters_collection(context):
    col = bpy.data.collections.get("Cutters")
    if col is None:
        col = bpy.data.collections.new("Cutters")
    if col.name not in context.scene.collection.children:
        context.scene.collection.children.link(col)
    return col


def _prepare_cutter(context, base, cutter):
    # (the view layer can still list just-removed objects as None until it updates)
    count = len([o for o in context.view_layer.objects
                 if o is not None and "_Cutter" in o.name and o != cutter]) + 1
    cutter.name = f"{base.name}_Cutter_{count}"
    cutter.display_type = 'WIRE'
    cutter.hide_render = True
    col = _cutters_collection(context)
    for c in list(cutter.users_collection):
        if c != col:
            c.objects.unlink(cutter)
    if cutter.name not in col.objects:
        col.objects.link(cutter)
    cutter.select_set(False)


def _insert_index(base):
    """New cuts go before Smooth by Angle / Weighted Normal so they still affect shading."""
    for i, mod in enumerate(base.modifiers):
        if mod.type == 'WEIGHTED_NORMAL':
            return i
        if mod.type == 'NODES' and mod.node_group and mod.node_group.name.startswith("Smooth by Angle"):
            return i
    return len(base.modifiers)


def _set_input(mod, identifier, value):
    """Blender 5.1+ exposes modifier inputs as mod.properties.inputs.<id>.value;
    older versions use mod["<id>"]."""
    props = getattr(mod, "properties", None)
    socket = getattr(getattr(props, "inputs", None), identifier, None)
    if socket is not None:
        socket.value = value
    else:
        mod[identifier] = value


def add_sharp_subtract(base, cutter, node_group, before_mirror=False):
    """before_mirror: put the cut before the first Mirror modifier, so it gets mirrored too."""
    index = _insert_index(base)
    if before_mirror:
        for i, mod in enumerate(base.modifiers):
            if mod.type == 'MIRROR':
                index = min(index, i)
                break
    mod = base.modifiers.new(cutter.name, 'NODES')
    mod.node_group = node_group
    _set_input(mod, _object_input(node_group), cutter)
    # Not always the last one: modifiers pinned to last (like Smooth by Angle)
    # stay at the end, and new modifiers are inserted before them.
    from_index = base.modifiers.find(mod.name)
    if index < from_index:
        base.modifiers.move(from_index, index)
    return mod


def purge_orphan_cutters():
    """A cutter used by a Geometry Nodes input can stay in bpy.data after being
    deleted (unlinked from every collection, but still with one user). Remove those."""
    for obj in list(bpy.data.objects):
        if "_Cutter" in obj.name and not obj.users_collection:
            bpy.data.objects.remove(obj)


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class GTOOLS_OT_sharp_subtract(bpy.types.Operator):
    """Subtract the selected objects from the active one with Geometry Nodes, marking the cut edges sharp"""
    bl_idname = "view3d.gtools_sharp_subtract"
    bl_label = "Sharp Subtract"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        base = context.active_object

        if base.mode == 'EDIT':
            # Same as Live Subtract: the selected part becomes the cutter.
            bpy.ops.mesh.select_linked()
            bpy.ops.mesh.normals_make_consistent(inside=False)
            bpy.ops.mesh.separate(type='SELECTED')
            bpy.ops.object.mode_set(mode='OBJECT')

        cutters = [o for o in context.selected_objects if o != base and o.type == 'MESH']
        if not cutters:
            self.report({'WARNING'}, "Select the cutter(s) and then the object to cut (active)")
            return {'CANCELLED'}

        node_group = get_node_group()
        for cutter in cutters:
            _prepare_cutter(context, base, cutter)
            add_sharp_subtract(base, cutter, node_group)

        base.select_set(True)
        context.view_layer.objects.active = base
        return {'FINISHED'}


def register():
    bpy.utils.register_class(GTOOLS_OT_sharp_subtract)


def unregister():
    bpy.utils.unregister_class(GTOOLS_OT_sharp_subtract)


def hint_panel_origin(context, width):
    """Bottom-right corner of the viewport for the shortcut panel, kept clear of
    the sidebar (N) when it is drawn over the viewport."""
    x = context.region.width - width - 14
    if getattr(context.preferences.system, "use_region_overlap", False):
        for region in context.area.regions:
            if region.type == 'UI' and region.width > 1:
                x -= region.width
    return max(x, 14), 14
