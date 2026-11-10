"""Modifier Scroll (Object Mode): go through the booleans of the active object one
by one and pick the one to edit, like HardOps' modifier scroll.

Works with every modifier that cuts or adds with another object: Blender Boolean
modifiers and the add-on's Geometry Nodes booleans (Sharp Subtract, Smart Cut,
Edge Bevel...), plus the panel lines made on the object (their cutters). Start it from the Select pie in Object Mode.

- Wheel (or up / down arrows): next / previous boolean. Only its cutter is shown,
  outlined, and the list at the bottom left marks it.
- Click / Enter: select that cutter (and only it) to edit it.
- Esc / Right click: back to how it was.
"""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .sharp_subtract import hint_panel_origin


def _cutter_of(mod):
    """The object a boolean-like modifier uses, or None."""
    if mod.type == 'BOOLEAN':
        return mod.object if getattr(mod, "operand_type", 'OBJECT') == 'OBJECT' else None
    if mod.type != 'NODES' or mod.node_group is None:
        return None
    items = [item for item in mod.node_group.interface.items_tree
             if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.socket_type == 'NodeSocketObject']
    # Groups with several objects (a panel line: Target + Cutter): the cutter is the one.
    named = [item for item in items if item.name == "Cutter"]
    if named:
        items = named
    elif any(item.name == "Target" for item in items):
        items = [item for item in items if item.name != "Target"]
    for item in items:
            props = getattr(mod, "properties", None)
            socket = getattr(getattr(props, "inputs", None), item.identifier, None)
            value = socket.value if socket is not None else mod.get(item.identifier)
            if isinstance(value, bpy.types.Object):
                return value
    return None


def _target_of(mod):
    for item in mod.node_group.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == "Target":
            props = getattr(mod, "properties", None)
            socket = getattr(getattr(props, "inputs", None), item.identifier, None)
            return socket.value if socket is not None else mod.get(item.identifier)
    return None


def boolean_entries(obj):
    """(modifier, cutter) for every boolean-like modifier of the object, in stack order,
    then the panel lines made on it (they live on their own objects)."""
    out = []
    for mod in obj.modifiers:
        cutter = _cutter_of(mod)
        if cutter is not None:
            out.append((mod, cutter))
    for other in bpy.data.objects:
        if other is obj:
            continue
        for mod in other.modifiers:
            if mod.type == 'NODES' and mod.node_group is not None \
                    and mod.node_group.name.startswith("GT_PanelLine") and _target_of(mod) is obj:
                cutter = _cutter_of(mod)
                if cutter is not None:
                    out.append((mod, cutter))
    return out


def _kind(mod):
    if mod.type == 'BOOLEAN':
        return mod.operation.title()
    name = mod.node_group.name
    if name.startswith("GT_PanelLine"):
        return "Panel Line"
    if name.startswith("GT_PanelMaterial"):
        return "Panel Material"
    if name.startswith(("GT_Fill", "GT_SharpFill", "GT_Union", "GT_SharpUnion")):
        return "Fill"
    return "Cut"


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _draw_view(op, context):
    if op.outline is None:
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    line.uniform_float("viewportSize", (context.region.width, context.region.height))
    line.uniform_float("lineWidth", 2.0)
    line.uniform_float("color", (1.0, 0.6, 0.2, 0.9))
    batch_for_shader(line, 'LINES', {"pos": op.outline}).draw(line)
    gpu.state.blend_set('NONE')


def _panel(context, x, y, title, rows, width=None, align_right=False):
    """Simple text panel; rows are (text, highlighted)."""
    import blf
    font, size = 0, 12
    blf.size(font, size)
    line_h = size * 1.55
    if width is None:
        width = max([blf.dimensions(font, title)[0]] + [blf.dimensions(font, t)[0] for t, _ in rows]) + 24
    height = line_h * (len(rows) + 1) + 14
    if align_right:
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
    for text, highlighted in rows:
        ty -= line_h
        if highlighted:
            blf.color(font, 1.0, 0.6, 0.2, 1.0)
        else:
            blf.color(font, 1.0, 1.0, 1.0, 0.75)
        blf.position(font, x + 10, ty, 0)
        blf.draw(font, text)


def _draw_pixel(op, context):
    gpu.state.blend_set('ALPHA')
    # The list of booleans, bottom left (a window around the current one if it's long).
    total = len(op.entries)
    first = max(0, min(op.index - 7, total - 15))
    rows = []
    for i in range(first, min(total, first + 15)):
        mod, cutter = op.entries[i]
        marker = "▶ " if i == op.index else "   "
        state = "" if mod.show_viewport else "  (off)"
        if cutter not in op.hidden:
            state += "  (excluded collection: included when you pick it)"
        rows.append((f"{marker}{i + 1}. {mod.name}  ·  {_kind(mod)}  ·  {cutter.name}{state}", i == op.index))
    x = 14
    if getattr(context.preferences.system, "use_region_overlap", False):
        for region in context.area.regions:
            if region.type == 'TOOLS' and region.width > 1:
                x += region.width
    _panel(context, x, 14, f"Booleans of {op.obj.name}  ·  {op.index + 1} / {total}", rows)
    # Shortcuts, bottom right like the other tools.
    _panel(context, 0, 0, "Modifier Scroll", [
        ("Wheel / Up / Down     next / previous", False),
        ("Click / Enter     select it to edit", False),
        ("H     modifier on / off", False),
        ("Esc / Right click     cancel", False),
    ], align_right=True)
    gpu.state.blend_set('NONE')


def _find_layer_collection(layer_collection, obj):
    """The layer collection (searching down) whose collection holds obj, or None."""
    if obj.name in layer_collection.collection.objects:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, obj)
        if found is not None:
            return found
    return None


def _parents(root, target, path=()):
    if root is target:
        return path
    for child in root.children:
        found = _parents(child, target, path + (root,))
        if found is not None:
            return found
    return None


def _bring_into_view_layer(context, cutter):
    """Make a cutter that sits in an excluded collection (e.g. Cutters unchecked in
    the Outliner) selectable: include its collection again, but keep the other
    objects in it hidden so the view stays clean. If it isn't in this scene at all,
    link it into the Cutters collection."""
    root = context.view_layer.layer_collection
    lc = _find_layer_collection(root, cutter)
    if lc is not None:
        before = set(context.view_layer.objects)
        for parent in (_parents(root, lc) or ()):
            parent.exclude = False
        lc.exclude = False
        context.view_layer.update()
        for o in lc.collection.objects:
            if o is not cutter and o not in before and o.name in context.view_layer.objects:
                o.hide_set(True)
    else:
        col = bpy.data.collections.get("Cutters") or bpy.data.collections.new("Cutters")
        if col.name not in context.scene.collection.children:
            context.scene.collection.children.link(col)
        if cutter.name not in col.objects:
            col.objects.link(cutter)
        context.view_layer.update()
    return cutter.name in context.view_layer.objects


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class GTOOLS_OT_modifier_scroll(bpy.types.Operator):
    """Scroll through the booleans of the active object and pick one to edit"""
    bl_idname = "object.gtools_modifier_scroll"
    bl_label = "Modifier Scroll"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return context.mode == 'OBJECT' and obj is not None and obj.type == 'MESH'

    def invoke(self, context, event):
        self.obj = context.active_object
        self.entries = boolean_entries(self.obj)
        if not self.entries:
            self.report({'INFO'}, "Modifier Scroll: this object has no booleans")
            return {'CANCELLED'}
        # Only cutters in this view layer can be shown / hidden / selected (others may sit
        # in an excluded collection or another scene: they're listed, just not toggled).
        layer_objects = set(context.view_layer.objects)
        cutters = {c for _, c in self.entries if c in layer_objects}
        self.hidden = {c: c.hide_get() for c in cutters}
        self.selection = {o: o.select_get() for o in context.view_layer.objects}
        self.active = context.view_layer.objects.active
        self.viewport = {m: m.show_viewport for m, _ in self.entries}
        self.index = len(self.entries) - 1  # start at the newest
        self.outline = None
        self._show(context)
        self._handles = (
            bpy.types.SpaceView3D.draw_handler_add(_draw_view, (self, context), 'WINDOW', 'POST_VIEW'),
            bpy.types.SpaceView3D.draw_handler_add(_draw_pixel, (self, context), 'WINDOW', 'POST_PIXEL'),
        )
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Modifier Scroll: wheel / arrows: next / previous  |  Click / Enter: select it to edit  |  "
            "H: modifier on / off  |  Esc: cancel")
        return {'RUNNING_MODAL'}

    def _show(self, context):
        """Only the current cutter visible, with its outline."""
        _mod, current = self.entries[self.index]
        for cutter in self.hidden:
            try:
                cutter.hide_set(cutter is not current)
            except RuntimeError:
                pass
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = current.evaluated_get(depsgraph)
        try:
            mesh = evaluated.to_mesh()
        except RuntimeError:
            self.outline = None
            return
        m = current.matrix_world
        pts = []
        for e in mesh.edges:
            a, b = e.vertices
            pts += [tuple(m @ mesh.vertices[a].co), tuple(m @ mesh.vertices[b].co)]
        evaluated.to_mesh_clear()
        self.outline = pts or None

    def _end(self, context):
        for h in self._handles:
            bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        context.workspace.status_text_set(None)
        context.area.tag_redraw()

    def _restore(self, keep=None):
        for cutter, hidden in self.hidden.items():
            if cutter is not keep:
                try:
                    cutter.hide_set(hidden)
                except (RuntimeError, ReferenceError):
                    pass

    def modal(self, context, event):
        context.area.tag_redraw()
        step = 0
        if event.type in {'WHEELUPMOUSE', 'UP_ARROW'} and event.value == 'PRESS':
            step = 1
        elif event.type in {'WHEELDOWNMOUSE', 'DOWN_ARROW'} and event.value == 'PRESS':
            step = -1
        if step:
            self.index = (self.index + step) % len(self.entries)
            self._show(context)
        elif event.type == 'H' and event.value == 'PRESS':
            mod = self.entries[self.index][0]
            mod.show_viewport = not mod.show_viewport
        elif (event.type in {'RET', 'NUMPAD_ENTER'} or event.type == 'LEFTMOUSE') and event.value == 'PRESS':
            mod, cutter = self.entries[self.index]
            if cutter not in self.hidden:  # not in this view layer: bring it in to edit it
                if not _bring_into_view_layer(context, cutter):
                    self.report({'WARNING'}, f"{cutter.name} can't be brought into this View Layer")
                    return {'RUNNING_MODAL'}
                self.hidden[cutter] = False
            self._end(context)
            self._restore(keep=cutter)
            cutter.hide_set(False)
            for o in context.view_layer.objects:
                o.select_set(False)
            cutter.select_set(True)
            context.view_layer.objects.active = cutter
            self.report({'INFO'}, f"Editing {cutter.name} ({mod.name})")
            return {'FINISHED'}
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._end(context)
            self._restore()
            for mod, on in self.viewport.items():
                mod.show_viewport = on
            for o, sel in self.selection.items():
                try:
                    o.select_set(sel)
                except ReferenceError:
                    pass
            context.view_layer.objects.active = self.active
            return {'CANCELLED'}
        elif event.type in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}


def register():
    bpy.utils.register_class(GTOOLS_OT_modifier_scroll)


def unregister():
    bpy.utils.unregister_class(GTOOLS_OT_modifier_scroll)
