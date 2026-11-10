bl_info = {
    "name": "Pie Selection",
    "description": "Select Modes",
    "author": "Vaughan Ling",
    "version": (0, 1, 0),
    "blender": (2, 80, 0),
    "location": "",
    "warning": "",
    "wiki_url": "",
    "category": "Pie Menu"
    }

import bpy
from bpy.types import Menu

# Select Pie
class HP_MT_pie_select(Menu):
    bl_label = "Select"
    bl_idname = "Pie_Select"

    def draw(self, context):
        layout = self.layout
        obj = context.object
        obj_type = obj.type if obj else None  # no active object: only the generic buttons

        pie = layout.menu_pie()

        # left
        # G-Tools buttons sit in their own column to the left of the usual ones.
        if bpy.context.mode == 'OBJECT':
            row = pie.row()
            tools = row.column()
            tools.scale_y = 1.5
            tools.operator("object.select_grouped", text="Select Collection").type = 'COLLECTION'
            col = row.column()
            col.scale_y=1.5
            col.operator("object.select_grouped", text="Similar")
            col.operator("view3d.selectsmartsimilar", text="Similar Name")
        else:
            row = pie.row()
            if bpy.context.mode == 'EDIT_MESH':
                tools = row.column()
                tools.scale_y = 1.5
                tools.operator("mesh.gtools_curve_align", text="Curve Align")
                tools.operator("mesh.gtools_loop_resample", text="Loop Resample")
            col = row.column()
            col.scale_y=1.5
            col.operator("mesh.select_similar", text="Similar")
            col.operator("mesh.hp_select_border", text="Border")

        # Right
        match bpy.context.mode:
            case "EDIT_MESH":
                pie.operator("mesh.faces_select_linked_flat", text="Select Flat").sharpness=0.2
            case "SCULPT":
                pie.operator("object.voxel_remesh", text="Remesh", icon='NONE')
            case _:
                # A pie button of its own (dark background), where Select Collection was.
                pie.operator("object.gtools_modifier_scroll", text="Modifier Scroll")

        # bottom
        pie.operator("object.mode_set", text="Object", icon='MESH_CUBE').mode='OBJECT'
        # top

        match obj_type:
            case "GREASEPENCIL":
                pie.operator('object.mode_set', text = 'GP Edit', icon='EDITMODE_HLT').mode='EDIT'
            case "META":
                pie.operator('object.mode_set', text = 'Edit', icon='META_DATA').mode='EDIT'
            case "ARMATURE":
                pie.operator('object.mode_set', text = 'Edit', icon='NONE').mode='EDIT'
            case "LATTICE":
                pie.operator('object.mode_set', text = 'Edit', icon='NONE').mode='EDIT'
            case _:
                pie.operator("object.selectmodesmart", text="Edge", icon='NONE').selectmode='EDGE'

        # topleft
        match obj_type:
            case "GREASEPENCIL":
                pie.operator('object.mode_set', text = 'GP Draw', icon='GREASEPENCIL').mode='PAINT_GREASE_PENCIL'
            case "ARMATURE":
                pie.operator('object.mode_set', text = 'Pose', icon='NONE').mode='POSE'
            case "META":
                split = pie.split()
                col = split.column()
                col.scale_x=1.1
                col.label(text="")
            case "LATTICE":
                split = pie.split()
            case _:
                pie.operator("object.selectmodesmart", text="Vert", icon='NONE').selectmode='VERT'

        # topright
        match obj_type:
            case "GREASEPENCIL":
                pie.operator('object.mode_set', text = "GP Sculpt", icon="SCULPTMODE_HLT").mode="SCULPT_GREASE_PENCIL"
            case "META":
                split = pie.split()
                col = split.column()
                col.scale_x=1.1
                col.label(text="")
            case "LATTICE":
                split = pie.split()
            case _:
                pie.operator("object.selectmodesmart", text="Face", icon='NONE').selectmode='FACE'

        # bottomleft
        split = pie.split()
        col = split.column()
        col.scale_y=1.5
        col.separator()
        col.separator()
        col.separator()
        col.separator()
        col.separator()
        col.operator('object.separate_and_select', text = 'Split To New Object')

        col.operator('object.join', text = 'Join Objects')
        prop = col.operator('object.parent_set', text = 'Set Parent')
        prop.type = 'OBJECT'
        prop.keep_transform=True
        col.operator('object.parent_clear', text = 'Remove Parent').type='CLEAR_KEEP_TRANSFORM'

        #bottomright
        split = pie.split()
        col = split.column()
        col.scale_y=1.5
        col.separator()
        col.separator()
        col.separator()
        col.separator()
        col.separator()
        col.separator()

        match obj_type:
            case "MESH":
                col.operator('object.mode_set', text = 'Sculpt', icon='SCULPTMODE_HLT').mode='SCULPT'
                col.operator('object.mode_set', text = 'Vertex Paint', icon='VPAINT_HLT').mode='VERTEX_PAINT'
                col.operator('object.mode_set', text = 'Weight Paint', icon='WPAINT_HLT').mode='WEIGHT_PAINT'
                col.operator('object.mode_set', text = 'Texture Paint', icon='BRUSH_DATA').mode='TEXTURE_PAINT'
                col.operator('object.sculpt_mode_with_dynotopo', text = 'Sculpt With Dynotopo', icon='SCULPTMODE_HLT')
            case "GREASEPENCIL":
                col.operator('object.mode_set', text = 'Vertex Paint', icon='VPAINT_HLT').mode='VERTEX_GREASE_PENCIL'
                col.operator('object.mode_set', text = 'Weight Paint', icon='WPAINT_HLT').mode='WEIGHT_GREASE_PENCIL'
            case "LATTICE":
                col.operator('object.mode_set', text = 'Weight Paint', icon='WPAINT_HLT').mode='WEIGHT_PAINT'
            case "ARMATURE":
                pass
            case "META":
                pass

            # New datatypes can easily be added above pass
            case _:
                pass

        # Particles Will be removed in later versions, keeping it on the bottom for organisation
        if obj_type == "MESH" and obj.particle_systems:
            col.operator('object.mode_set', text = 'Particle Edit', icon='PARTICLEMODE').mode='PARTICLE_EDIT'

class HP_OT_gp_canvas(bpy.types.Operator):
    bl_idname = "view3d.gp_canvas"
    bl_label = ""
    bl_options = {'REGISTER', 'UNDO'}
    type: bpy.props.StringProperty(name="Front")
    def execute(self, context):
        axis = {'Front': 'AXIS_Y', 'Top': 'AXIS_Z', 'Side': 'AXIS_X'}.get(self.type)
        if axis:
            context.tool_settings.gpencil_sculpt.lock_axis = axis
        return {'FINISHED'}

class HP_OT_sculpt_mode_with_dynotopo(bpy.types.Operator):
    bl_idname = "object.sculpt_mode_with_dynotopo"      # unique identifier for buttons and menu items to reference.
    bl_label = ""      # display name in the interface.
    bl_options = {'REGISTER', 'UNDO'}  # enable undo for the operator.
    def invoke(self, context, event):
        bpy.ops.object.mode_set(mode='SCULPT')
        if not bpy.context.sculpt_object.use_dynamic_topology_sculpting:
            bpy.ops.sculpt.dynamic_topology_toggle()
        return {'FINISHED'}


class HP_OT_SelectModeSmart(bpy.types.Operator):
    """SelectModeSmart"""      # blender will use this as a tooltip for menu items and buttons.
    bl_idname = "object.selectmodesmart"        # unique identifier for buttons and menu items to reference.
    bl_label = "Select Mode Smart"         # display name in the interface.
    bl_options = {'REGISTER', 'UNDO'}  # enable undo for the operator.
    selectmode : bpy.props.StringProperty(name="SelectMode")

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def invoke(self, context, event):
        def select(selectmode):
            bpy.ops.mesh.select_mode(type=selectmode)

        match bpy.context.mode:
            case "OBJECT":
                match bpy.context.object.type:
                    case "MESH":
                        bpy.ops.object.mode_set(mode='EDIT')
                        select(self.selectmode)
                    case "GREASEPENCIL":
                        bpy.ops.object.mode_set(mode='PAINT_GREASE_PENCIL')
                    case "CURVE" | "FONT":
                        bpy.ops.object.mode_set(mode='EDIT')
            case "EDIT_MESH":
                select(self.selectmode)
            case "PAINT_GREASE_PENCIL":
                bpy.ops.object.mode_set(mode='OBJECT')
            case _:
                bpy.ops.object.mode_set(mode="EDIT")
        return {'FINISHED'}

class HP_OT_SelectSmartSimilar(bpy.types.Operator):
    """SelectSmartVert"""      # blender will use this as a tooltip for menu items and buttons.
    bl_idname = "view3d.selectsmartsimilar"        # unique identifier for buttons and menu items to reference.
    bl_label = "Select Smart Similar"         # display name in the interface.
    bl_options = {'REGISTER', 'UNDO'}  # enable undo for the operator.

    def invoke(self, context, event):
        if bpy.context.mode=='OBJECT':
            name = bpy.context.active_object.name
            name = str(name.split('.')[0]) + "*"
            bpy.ops.object.select_pattern(pattern=name)

        else:
            bpy.ops.mesh.select_similar()
        return {'FINISHED'}


class HP_OT_SelectSmartLinkedAndLoop(bpy.types.Operator):
    """SelectSmartVert"""      # blender will use this as a tooltip for menu items and buttons.
    bl_idname = "mesh.selectsmartlinkedandloop"        # unique identifier for buttons and menu items to reference.
    bl_label = "Select Smart Linked And Loop"         # display name in the interface.
    bl_options = {'REGISTER', 'UNDO'}  # enable undo for the operator.

    def invoke(self, context, event):
        if tuple(bpy.context.scene.tool_settings.mesh_select_mode) == (False, True, False):
            bpy.ops.mesh.loop_multi_select()
        else:
            bpy.ops.mesh.select_linked(delimit={'SEAM'})
        return {'FINISHED'}
class HP_OT_select_border(bpy.types.Operator):
    """Select Border"""    # blender will use this as a tooltip for menu items and buttons.
    bl_idname = "mesh.hp_select_border"        # unique identifier for buttons and menu items to reference.
    bl_label = "Select Border"        # display name in the interface.
    bl_options = {'REGISTER', 'UNDO'}  # enable undo for the operator.

    def invoke(self, context, event):
        bpy.ops.mesh.select_mode(type='EDGE')
        bpy.ops.mesh.region_to_loop()
        return {'FINISHED'}

classes = (
    HP_MT_pie_select,
    HP_OT_SelectModeSmart,
    HP_OT_SelectSmartLinkedAndLoop,
    HP_OT_SelectSmartSimilar,
    HP_OT_sculpt_mode_with_dynotopo,
    HP_OT_gp_canvas,
    HP_OT_select_border,
)
register, unregister = bpy.utils.register_classes_factory(classes)

if __name__ == "__main__":
    register()
