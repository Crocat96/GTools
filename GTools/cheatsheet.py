"""'G-Tools' panel in the viewport sidebar with the main hotkeys.

Reads keys from the user keyconfig, so it shows what is actually assigned
(including any changes the user makes in the preferences).
"""

import bpy

from .keymaps import ADDON_ID, find_active_item

# (label, keymap, operator, properties)
SECTIONS = (
    ("Pie menus", (
        ("Select / modes", "3D View", "wm.call_menu_pie", {"name": 'Pie_Select'}),
        ("Views", "3D View", "wm.call_menu_pie", {"name": 'View_Pie'}),
        ("Shading", "3D View", "wm.call_menu_pie", {"name": 'Shading_Pie'}),
        ("Booleans", "3D View", "wm.call_menu_pie", {"name": 'Pie_Boolean'}),
        ("Add (Edit Mode)", "Mesh", "wm.call_menu_pie", {"name": 'Add_PrimitivesPie'}),
        ("Save / open", "Window", "wm.call_menu_pie", {"name": 'Save_Pie'}),
    )),
    ("Smart tools", (
        ("Smart Extrude", "Mesh", "mesh.hp_extrude", None),
        ("Smart Bevel", "Mesh", "view3d.smart_bevel", None),
        ("Smart Delete", "3D View", "view3d.smart_delete", None),
        ("Smart Slide / Push", "Mesh", "mesh.push_and_slide", None),
        ("Separate and select", "Mesh", "object.separate_and_select", None),
        ("Loop cut", "Mesh", "mesh.hp_simple_loopcut", None),
        ("Subdivision on/off", "3D View", "view3d.subdivision_toggle", None),
        ("Origin to selection", "3D View", "view3d.smart_snap_origin", None),
        ("Draw primitives", "3D View", "view3d.hp_draw", None),
    )),
)


def _row(col, label, key):
    row = col.row(align=True)
    split = row.split(factor=0.58)
    split.label(text=label)
    sub = split.row()
    sub.alignment = 'RIGHT'
    sub.label(text=key)


class VIEW3D_PT_gtools_cheatsheet(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "G-Tools"
    bl_label = "G-Tools Hotkeys"

    @classmethod
    def poll(cls, context):
        addon = context.preferences.addons.get(ADDON_ID)
        return addon is not None and addon.preferences.show_cheatsheet

    def draw(self, context):
        prefs = context.preferences.addons[ADDON_ID].preferences
        layout = self.layout

        box = layout.box()
        box.label(text="Transform", icon='ORIENTATION_GLOBAL')
        col = box.column(align=True)
        if prefs.move_key == 'SPACE':
            _row(col, "Move", "Space")
            _row(col, "  lock Y axis", "Space")
        else:
            _row(col, "Move", "G")
            _row(col, "  lock Y axis", "Y")
        _row(col, "  X / Z axis", "X / Z")
        if prefs.use_xray_select:
            _row(col, "X-ray selection", "on")

        for title, entries in SECTIONS:
            rows = []
            for label, km_name, idname, props in entries:
                kmi = find_active_item(km_name, idname, props)
                if kmi is not None:
                    rows.append((label, kmi.to_string()))
            if not rows:
                continue
            box = layout.box()
            box.label(text=title, icon='KEYINGSET')
            col = box.column(align=True)
            for label, key in rows:
                _row(col, label, key)

        layout.operator("preferences.addon_show", text="Configure Hotkeys", icon='PREFERENCES').module = ADDON_ID


def register():
    bpy.utils.register_class(VIEW3D_PT_gtools_cheatsheet)


def unregister():
    bpy.utils.unregister_class(VIEW3D_PT_gtools_cheatsheet)
