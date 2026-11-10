bl_info = {
    "name": "G TOOLS",
    "author": "Gabriel Jimenez",
    "version": (0, 4, 0),
    "blender": (5, 0, 0),
    "location": "Edit > Preferences > Add-ons > G Tools (hotkeys) | View3D > Sidebar > G-Tools",
    "description": "Quicker and smarter Blender tools",
    "warning": "",
    "wiki_url": "none",
    "category": "Mesh",
}
import sys
import importlib
import bpy



modules_names = [
    'HEAVYPOLY_draw_primitives', 'HEAVYPOLY_OPERATORS',
    'HEAVYPOLY_panel_render', 'HEAVYPOLY_pie_add', 'HEAVYPOLY_pie_boolean',
    'HEAVYPOLY_pie_save', 'HEAVYPOLY_pie_selection', 'HEAVYPOLY_pie_shading',
    'HEAVYPOLY_pie_view', 'sharp_subtract', 'smart_cut', 'curve_align', 'loop_resample', 'modifier_scroll', 'quick_decal', 'panel_line', 'ornament', 'edge_bevel', 'xray_select', 'curve_patch', 'smart_slide', 'keymaps', 'cheatsheet'
]  # A list of imported module names #'HEAVYPOLY_select_through_border' 'HEAVYPOLY_menu_master',



modules_full_names = {}
for module_name in modules_names:
    # Construct the full module name
    if 'DEBUG_MODE' in sys.argv:
        modules_full_names[module_name] = '{}'.format(module_name)
    else:
        modules_full_names[module_name] = '{}.{}'.format(__name__, module_name)

# Import or reload modules
for full_name in modules_full_names.values():
    if full_name in sys.modules:
        importlib.reload(sys.modules[full_name])
    else:
        globals()[full_name] = importlib.import_module(full_name)
        setattr(globals()[full_name], 'modules_names', modules_full_names)

# Register function to register all modules
def register():
    for full_name in modules_full_names.values():
        module = sys.modules.get(full_name)
        if module and hasattr(module, 'register'):
            module.register()

# Unregister function to unregister all modules
def unregister():
    for full_name in reversed(list(modules_full_names.values())):
        module = sys.modules.get(full_name)
        if module and hasattr(module, 'unregister'):
            module.unregister()

# Register the add-on when run as a script
if __name__ == "__main__":
    register()