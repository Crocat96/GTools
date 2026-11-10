"""X-ray selection (simple version).

Turns the viewport X-ray on, runs Blender's box / lasso / circle select and
turns X-ray off again when the selection finishes.
"""

import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty

_SELECT_IDNAMES = {"view3d.select_box", "view3d.select_lasso", "view3d.select_circle"}
_pending = []  # (viewport pointer, property name) to restore


def _restore(pending):
    """Find viewports by pointer: any that were closed in the meantime are skipped."""
    by_ptr = {}
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    by_ptr[space.as_pointer()] = space
    for ptr, attr in pending:
        space = by_ptr.get(ptr)
        if space is not None:
            setattr(space.shading, attr, False)


def _selection_running():
    for window in bpy.context.window_manager.windows:
        for op in window.modal_operators:
            # bl_idname may come as "VIEW3D_OT_select_box" or "view3d.select_box"
            if op.bl_idname.lower().replace("_ot_", ".") in _SELECT_IDNAMES:
                return True
    return False


def _watch():
    if _selection_running():
        return 0.05
    _restore(_pending)
    _pending.clear()
    return None


class GTOOLS_OT_select_xray(bpy.types.Operator):
    """Box / lasso / circle select that selects through the object"""
    bl_idname = "view3d.gtools_select_xray"
    bl_label = "X-Ray Select"
    bl_options = {'INTERNAL'}

    tool: EnumProperty(items=(('BOX', "Box", ""), ('LASSO', "Lasso", ""), ('CIRCLE', "Circle", "")))
    mode: EnumProperty(
        items=(('SET', "Set", ""), ('ADD', "Add", ""), ('SUB', "Subtract", ""),
               ('XOR', "Difference", ""), ('AND', "Intersect", "")),
        default='SET',
    )
    wait_for_input: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        space = context.space_data
        shading = space.shading
        attr = "show_xray_wireframe" if shading.type == 'WIREFRAME' else "show_xray"
        if not getattr(shading, attr):
            setattr(shading, attr, True)
            _pending.append((space.as_pointer(), attr))

        mode = self.mode
        if self.tool == 'CIRCLE' and mode not in {'SET', 'ADD', 'SUB'}:
            mode = 'SET'

        # Same as Heavypoly's original select_through_border: our handler first,
        # then Blender's selection, which sits on top and receives the drag.
        # When the selection ends, the next event reaches us and we turn X-ray off.
        context.window_manager.modal_handler_add(self)
        try:
            if self.tool == 'BOX':
                result = bpy.ops.view3d.select_box('INVOKE_DEFAULT', mode=mode, wait_for_input=self.wait_for_input)
            elif self.tool == 'LASSO':
                result = bpy.ops.view3d.select_lasso('INVOKE_DEFAULT', mode=mode)
            else:
                result = bpy.ops.view3d.select_circle('INVOKE_DEFAULT', mode=mode, wait_for_input=self.wait_for_input)
        except Exception:
            print("G-Tools X-ray: failed to start the selection")
            traceback.print_exc()
            result = {'CANCELLED'}

        if 'RUNNING_MODAL' not in result:
            print(f"G-Tools X-ray: selection returned {result} ({self.tool}, {mode})")

        if _pending and not bpy.app.timers.is_registered(_watch):
            bpy.app.timers.register(_watch, first_interval=0.05)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if _selection_running():
            return {'PASS_THROUGH'}
        _restore(_pending)
        _pending.clear()
        return {'FINISHED', 'PASS_THROUGH'}


def register():
    bpy.utils.register_class(GTOOLS_OT_select_xray)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    _restore(_pending)
    _pending.clear()
    bpy.utils.unregister_class(GTOOLS_OT_select_xray)
