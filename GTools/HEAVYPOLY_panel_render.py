import bpy
import random
from bpy.props import *
from bpy_extras.node_utils import find_node_input

class HP_PT_render(bpy.types.Panel):
    bl_label = "G TOOLS RENDER"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        col = row.column()
        col2 = row.column()
        layout.use_property_split = True
#FIRST COLUMN##############################################################

        scene = context.scene
        rd = scene.render
        props = scene.eevee
        image_settings = rd.image_settings
        col.label(text='RENDER CAMERA')
        col.prop(scene, "camera", text = '')
        row = col.row()
        row.scale_x=.5
        row.prop(rd, "resolution_x", text="X")
        row.prop(rd, "resolution_y", text="Y")
        row.prop(rd, "resolution_percentage", text="%")
        row = col.row()
        row.prop(scene, "frame_start", text="F Start")
        row.prop(rd, "fps", text = 'F Rate')
        row = col.row()
        row.prop(scene, "frame_end", text="F End")
        row.prop(scene, "frame_step", text="F Step")
        col.separator()
        row = col.row()

        col.label(text='WORLD')
        world = bpy.context.scene.world
        col.prop(scene.eevee, "use_soft_shadows")

        col.label(text="Mist Pass")
        #col.prop(scene.view_layers, "use_pass_mist")
        #row.prop(view_layer, "mist_settings.start", text="Start")
        #row.prop(view_layer, "mist_settings.depth", text="Depth")
        #row = col.row()
        #col.prop(view_layer, "mist_settings.falloff", text="Falloff")


        




       
        scene = bpy.context.scene
        props = scene.eevee

#SECOND COLUMN##############################################################

                
        

      
        col2.label(text='RENDER SETTINGS')
        col2.prop(scene.view_settings, 'view_transform', text='')
        col2.prop(scene.view_settings, 'look', text='')
        col2.template_curve_mapping(scene.view_settings, "curve_mapping", type='COLOR', levels=True)
        col2.prop(rd, "filepath", text="")
        row = col.row()
        row.prop(props, "taa_samples")
        row.prop(props, "taa_render_samples")


classes = (
    HP_PT_render,

)
register, unregister = bpy.utils.register_classes_factory(classes)


if __name__ == "__main__":
    register()
