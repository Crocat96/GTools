import bpy
import rna_keymap_ui

# Import custom operators and pie menus
from HEAVYPOLY_OPERATORS import (
    HP_OT_extrude,
    HP_OT_SmartBevel,
    HP_OT_PushAndSlide,
    HP_OT_smart_snap_origin,
    HP_OT_loopcut,
    HP_OT_SeparateAndSelect,
    HP_OT_Smart_Delete,
    HP_OT_Subdivision_Toggle,
)

from HEAVYPOLY_pie_boolean import HP_MT_pie_boolean
from HEAVYPOLY_pie_selection import HP_MT_pie_select
from HEAVYPOLY_pie_view import HP_MT_pie_view
from HEAVYPOLY_pie_shading import HP_MT_pie_shading
from HEAVYPOLY_pie_save import HP_MT_pie_save
from HEAVYPOLY_pie_add import HP_MT_pie_add

# List to store added keymaps
addon_keymaps = []

# List to store modified keymap items in default keyconfig
modified_keymap_items = []

# List of actions to toggle
actions_to_toggle = [
    {"idname": "screen.screen_full_area", "keymap": "Screen", "label": "Ctrl+Space Toggle Maximize Area"},
    {"idname": "view3d.render_border", "keymap": "3D View", "label": "Ctrl+B Set Render Region"},
    {"idname": "wm.call_menu", "keymap": "Mesh", "properties": {"name": "VIEW3D_MT_mesh_add"}, "label": "Ctrl+A Add Primitive in Edit Mode"},
    {"idname": "mesh.rip_move", "keymap": "Mesh", "label": "V Rip"},
    {"idname": "wm.call_menu", "keymap": "Mesh", "properties": {"name": "VIEW3D_MT_edit_mesh_delete"}, "label": "X Delete in Edit Mode"},
    {"idname": "object.delete", "keymap": "Object Mode", "label": "X Delete in Object Mode"},
    {"idname": "mesh.loopcut_slide", "keymap": "Mesh", "label": "Ctrl+R Loop Cut Slide"},
    {"idname": "mesh.separate", "keymap": "Mesh", "label": "Mesh Separate"},
    # Add more actions as needed
]

def get_action_prop_name(action, index):
    prop_name = f"disable_action_{index}_{action['idname']}_{action['keymap'].replace(' ', '_')}"
    # Replace any non-alphanumeric characters with underscores
    prop_name = ''.join(c if c.isalnum() else '_' for c in prop_name)
    # Limit the property name length to 63 characters
    prop_name = prop_name[:63]
    return prop_name

def clear_keymaps():
    """Remove added keymap items."""
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

def add_keymap_items():
    """Add custom keymap items."""
    clear_keymaps()

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    # -------------------------------------------------------------------------
    # Regular Keymaps
    # -------------------------------------------------------------------------

    # Mesh keymap (Edit Mode)
    km_mesh = kc.keymaps.new(name='Mesh', space_type='EMPTY')

    # LOOP CUT
    kmi = km_mesh.keymap_items.new('mesh.hp_simple_loopcut', type='R', value='PRESS', ctrl=True)
    addon_keymaps.append((km_mesh, kmi))

    # SELECT LINKED
    kmi = km_mesh.keymap_items.new('mesh.select_linked_pick', type='LEFTMOUSE', value='DOUBLE_CLICK')
    addon_keymaps.append((km_mesh, kmi))

    # SELECT LINKED ADD
    kmi = km_mesh.keymap_items.new('mesh.select_linked_pick', type='LEFTMOUSE', value='DOUBLE_CLICK', shift=True)
    addon_keymaps.append((km_mesh, kmi))

    # SMART PUSH AND SLIDE
    kmi = km_mesh.keymap_items.new(HP_OT_PushAndSlide.bl_idname, type='G', value='PRESS', shift=True)
    addon_keymaps.append((km_mesh, kmi))

    # SELECT RING
    kmi = km_mesh.keymap_items.new('mesh.edgering_select', type='LEFTMOUSE', value='DOUBLE_CLICK', alt=True)
    addon_keymaps.append((km_mesh, kmi))

    # SEPARATE AND SELECT
    kmi = km_mesh.keymap_items.new(HP_OT_SeparateAndSelect.bl_idname, type='P', value='PRESS')
    addon_keymaps.append((km_mesh, kmi))

    # 3D View keymap (Object Mode)
    km_view3d = kc.keymaps.new(name='3D View', space_type='VIEW_3D')

    # SMART DELETE
    kmi = km_view3d.keymap_items.new(HP_OT_Smart_Delete.bl_idname, type='X', value='PRESS')
    addon_keymaps.append((km_view3d, kmi))

    # SMART EXTRUDE
    kmi = km_view3d.keymap_items.new(HP_OT_extrude.bl_idname, type='SPACE', value='PRESS', shift=True)
    addon_keymaps.append((km_view3d, kmi))

    # INSET
    kmi = km_view3d.keymap_items.new('mesh.inset', type='SPACE', value='PRESS', alt=True)
    addon_keymaps.append((km_view3d, kmi))

    # SMART BEVEL
    kmi = km_view3d.keymap_items.new(HP_OT_SmartBevel.bl_idname, type='B', value='PRESS')
    addon_keymaps.append((km_view3d, kmi))

    # FRAME SELECTED
    kmi = km_view3d.keymap_items.new('view3d.view_selected', type='MIDDLEMOUSE', value='PRESS', ctrl=True, shift=True)
    addon_keymaps.append((km_view3d, kmi))

    # JOIN VERTEX
    kmi = km_view3d.keymap_items.new('mesh.merge', type='J', value='PRESS', ctrl=True)
    kmi.properties.type = 'LAST'
    addon_keymaps.append((km_view3d, kmi))

    # BRIDGE
    kmi = km_view3d.keymap_items.new('mesh.bridge_edge_loops', type='B', value='PRESS', shift=True)
    addon_keymaps.append((km_view3d, kmi))

    # TOGGLE SUBDIVISION
    kmi = km_view3d.keymap_items.new('view3d.subdivision_toggle', type='TAB', value='PRESS')
    addon_keymaps.append((km_view3d, kmi))

    # SNAP ORIGIN TO SELECTION
    kmi = km_view3d.keymap_items.new(HP_OT_smart_snap_origin.bl_idname, type='RIGHTMOUSE', value='PRESS', ctrl=True, shift=True)
    addon_keymaps.append((km_view3d, kmi))

    # -------------------------------------------------------------------------
    # Pie Menus
    # -------------------------------------------------------------------------

    # BOOLEAN PIE MENU
    kmi = km_view3d.keymap_items.new('wm.call_menu_pie', type='B', value='PRESS', ctrl=True)
    kmi.properties.name = HP_MT_pie_boolean.bl_idname
    addon_keymaps.append((km_view3d, kmi))

    # SELECTION PIE MENU
    kmi = km_view3d.keymap_items.new('wm.call_menu_pie', type='SPACE', value='PRESS', ctrl=True)
    kmi.properties.name = HP_MT_pie_select.bl_idname
    addon_keymaps.append((km_view3d, kmi))

    # VIEW PIE MENU
    kmi = km_view3d.keymap_items.new('wm.call_menu_pie', type='V', value='PRESS')
    kmi.properties.name = HP_MT_pie_view.bl_idname
    addon_keymaps.append((km_view3d, kmi))

    # SHADING PIE MENU
    kmi = km_view3d.keymap_items.new('wm.call_menu_pie', type='Z', value='PRESS')
    kmi.properties.name = HP_MT_pie_shading.bl_idname
    addon_keymaps.append((km_view3d, kmi))

    # SAVE PIE MENU
    kmi = km_view3d.keymap_items.new('wm.call_menu_pie', type='S', value='PRESS', ctrl=True)
    kmi.properties.name = HP_MT_pie_save.bl_idname
    addon_keymaps.append((km_view3d, kmi))

    # ADD PRIMITIVES PIE MENU IN EDIT MODE
    kmi = km_mesh.keymap_items.new('wm.call_menu_pie', type='A', value='PRESS', shift=True)
    kmi.properties.name = HP_MT_pie_add.bl_idname
    addon_keymaps.append((km_mesh, kmi))

def toggle_action(enable, idname, keymap_name, properties=None):
    """Enable or disable a keymap item."""
    kc = bpy.context.window_manager.keyconfigs.default
    km = kc.keymaps.get(keymap_name)
    if not km:
        print(f"Keymap '{keymap_name}' not found")
        return

    found = False
    for kmi in km.keymap_items:
        if kmi.idname == idname:
            if not properties or all(getattr(kmi.properties, prop, None) == val for prop, val in properties.items()):
                # Before changing 'active', store the original state if not already stored
                if kmi not in [item[0] for item in modified_keymap_items]:
                    modified_keymap_items.append((kmi, kmi.active))
                kmi.active = enable
                found = True
                action_text = 'Enabled' if enable else 'Disabled'
                print(f"{action_text} {idname} in {keymap_name}")
                break
    if not found:
        print(f"{idname} not found in {keymap_name} or properties do not match.")

def update_actions(self, context):
    """Update the enabled state of actions."""
    wm = context.window_manager
    for index, action in enumerate(actions_to_toggle):
        prop_name = get_action_prop_name(action, index)
        prop = getattr(wm, prop_name, False)
        toggle_action(not prop, action["idname"], action["keymap"], action.get("properties"))

class KEYMAP_OT_reset(bpy.types.Operator):
    """Reset keymaps to default settings."""
    bl_idname = "keymap.reset"
    bl_label = "Reset Keymaps"

    def execute(self, context):
        add_keymap_items()
        update_actions(self, context)
        self.report({'INFO'}, "Keymaps reset to default")
        return {'FINISHED'}

class KEYMAP_UL_list(bpy.types.Panel):
    """Panel to display and manage custom keymaps."""
    bl_label = "Keymap List"
    bl_idname = "OBJECT_PT_keymap_list"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Tool'

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        # Collapsible box for action toggles
        box = layout.box()
        row = box.row()
        row.prop(wm, "expand_actions", text="", icon="TRIA_DOWN" if wm.expand_actions else "TRIA_RIGHT", emboss=False)
        row.label(text="Toggle Actions")

        if wm.expand_actions:
            for index, action in enumerate(actions_to_toggle):
                prop_name = get_action_prop_name(action, index)
                box.prop(wm, prop_name, text=action["label"])

        # Keymap list
        box = layout.box()
        col = box.column()
        col.label(text="Keymap List:", icon="KEYINGSET")

        kc = bpy.context.window_manager.keyconfigs.user

        # Add heading for Regular Keymaps and Pie Menus
        regular_keymaps_done = False
        for km, kmi in addon_keymaps:
            if kmi.idname == 'wm.call_menu_pie' and not regular_keymaps_done:
                regular_keymaps_done = True
                # Add heading for Pie Menus
                col.label(text="Pie Menus:", icon="DOT")
            col.context_pointer_set("keymap", km)
            rna_keymap_ui.draw_kmi([], kc, km, kmi, col, 0)
            col.separator()

        # Reset button
        col.operator(KEYMAP_OT_reset.bl_idname, text="Reset Keymaps", icon="LOOP_BACK")

def register():
    bpy.utils.register_class(KEYMAP_UL_list)
    bpy.utils.register_class(KEYMAP_OT_reset)

    # Register pie menu classes
    bpy.utils.register_class(HP_MT_pie_boolean)
    bpy.utils.register_class(HP_MT_pie_select)
    bpy.utils.register_class(HP_MT_pie_view)
    bpy.utils.register_class(HP_MT_pie_shading)
    bpy.utils.register_class(HP_MT_pie_save)
    bpy.utils.register_class(HP_MT_pie_add)

    # Add properties to WindowManager
    wm = bpy.types.WindowManager
    wm.expand_actions = bpy.props.BoolProperty(
        name="Expand Actions",
        description="Expand or collapse the action toggles",
        default=False
    )
    for index, action in enumerate(actions_to_toggle):
        prop_name = get_action_prop_name(action, index)
        setattr(
            wm,
            prop_name,
            bpy.props.BoolProperty(
                name=action["label"],
                default=False,
                update=update_actions
            )
        )

    add_keymap_items()

def unregister():
    clear_keymaps()

    # Restore original keymap item states
    for kmi, original_active in modified_keymap_items:
        kmi.active = original_active
    modified_keymap_items.clear()

    # Remove properties from WindowManager
    wm = bpy.types.WindowManager
    del wm.expand_actions
    for index, action in enumerate(actions_to_toggle):
        prop_name = get_action_prop_name(action, index)
        delattr(wm, prop_name)

    # Unregister classes
    bpy.utils.unregister_class(KEYMAP_UL_list)
    bpy.utils.unregister_class(KEYMAP_OT_reset)

    # Unregister pie menu classes
    bpy.utils.unregister_class(HP_MT_pie_add)
    bpy.utils.unregister_class(HP_MT_pie_save)
    bpy.utils.unregister_class(HP_MT_pie_shading)
    bpy.utils.unregister_class(HP_MT_pie_view)
    bpy.utils.unregister_class(HP_MT_pie_select)
    bpy.utils.unregister_class(HP_MT_pie_boolean)

if __name__ == "__main__":
    register()
