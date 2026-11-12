"""G-Tools hotkeys, configurable from the add-on preferences.

- New hotkeys go into the "addon" keyconfig (they take priority over Blender's).
- Conflicting Blender hotkeys are disabled in the "user" keyconfig and restored
  when the settings change or the add-on is disabled.
- Modal map changes (Space = Y axis while moving) also go into the "user"
  keyconfig: Blender does not allow modal maps in the addon keyconfig.
"""

import bpy
import rna_keymap_ui
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .keymap_data import KM_ARGS, REST, XRAY_DEFAULTS

ADDON_ID = __package__

SELECT_OPS = {
    "view3d.select_box": 'BOX',
    "view3d.select_lasso": 'LASSO',
    "view3d.select_circle": 'CIRCLE',
}
XRAY_OP = "view3d.gtools_select_xray"


# ---------------------------------------------------------------------------
# Configurable keys.
# Each option: "add" and "disable" lists of (keymap, operator, event, props)
# ---------------------------------------------------------------------------

def _ev(type, value='PRESS', **mods):
    return dict(type=type, value=value, **mods)


FEATURES = {
    "move_key": {
        'SPACE': {
            "add": [
                ("Window", "transform.translate", _ev('SPACE'), None),
                ("Markers", "marker.move", _ev('SPACE'), None),
                ("Transform Modal Map", "AXIS_Y", _ev('SPACE'), None),
                # A quick second Space press arrives as a double click.
                ("Transform Modal Map", "AXIS_Y", _ev('SPACE', 'DOUBLE_CLICK'), None),
            ],
            "disable": [
                ("Frames", "screen.animation_play", _ev('SPACE'), None),
                ("Frames", "screen.animation_play", _ev('SPACE', shift=True, ctrl=True), {"reverse": True}),
                ("Markers", "marker.move", _ev('G'), None),
                ("Transform Modal Map", "CONFIRM", _ev('SPACE', any=True), None),
            ],
        },
        'G': {"add": [], "disable": []},
    },
    "extrude_key": {
        'SHIFT_SPACE': {
            "add": [
                ("Mesh", "mesh.hp_extrude", _ev('SPACE', shift=True), None),
                ("Curve", "mesh.hp_extrude", _ev('SPACE', shift=True), None),
            ],
            "disable": [],
        },
        'E': {
            "add": [
                ("Mesh", "mesh.hp_extrude", _ev('E'), None),
                ("Curve", "mesh.hp_extrude", _ev('E'), None),
            ],
            "disable": [
                ("Mesh", "view3d.edit_mesh_extrude_move_normal", _ev('E'), None),
                ("Curve", "curve.extrude_move", _ev('E'), None),
            ],
        },
        'OFF': {"add": [], "disable": []},
    },
    "bevel_key": {
        'B': {
            "add": [("Mesh", "view3d.smart_bevel", _ev('B'), None)],
            "disable": [],
        },
        'CTRL_B': {
            "add": [("Mesh", "view3d.smart_bevel", _ev('B', ctrl=True), None)],
            "disable": [("Mesh", "mesh.bevel", _ev('B', ctrl=True), {"affect": 'EDGES'})],
        },
        'OFF': {"add": [], "disable": []},
    },
    "delete_key": {
        'X': {
            "add": [("3D View", "view3d.smart_delete", _ev('X'), None)],
            "disable": [
                ("Mesh", "wm.call_menu", _ev('X'), {"name": 'VIEW3D_MT_edit_mesh_delete'}),
                ("Curve", "wm.call_menu", _ev('X'), {"name": 'VIEW3D_MT_edit_curve_delete'}),
                ("Object Mode", "object.delete", _ev('X'), {"use_global": False}),
                ("Metaball", "mball.delete_metaelems", _ev('X'), None),
            ],
        },
        'OFF': {"add": [], "disable": []},
    },
    "subdiv_key": {
        'TAB': {
            "add": [
                ("3D View", "view3d.subdivision_toggle", _ev('TAB'), None),
                ("Mesh", "view3d.subdivision_toggle", _ev('TAB'), None),
            ],
            "disable": [
                ("Object Non-modal", "object.mode_set", _ev('TAB'), {"mode": 'EDIT', "toggle": True}),
            ],
        },
        'SELECT_PIE': {
            "add": [
                ("3D View", "wm.call_menu_pie", _ev('TAB'), {"name": 'Pie_Select'}),
                ("Mesh", "wm.call_menu_pie", _ev('TAB'), {"name": 'Pie_Select'}),
            ],
            "disable": [
                ("Object Non-modal", "object.mode_set", _ev('TAB'), {"mode": 'EDIT', "toggle": True}),
            ],
        },
        'OFF': {"add": [], "disable": []},
    },
    "use_pies": {
        True: {
            "add": [
                ("3D View", "wm.call_menu_pie", _ev('SPACE', ctrl=True), {"name": 'Pie_Select'}),
                ("3D View", "wm.call_menu_pie", _ev('V'), {"name": 'View_Pie'}),
                ("3D View", "wm.call_menu_pie", _ev('Z'), {"name": 'Shading_Pie'}),
                ("3D View", "wm.call_menu_pie", _ev('B', ctrl=True), {"name": 'Pie_Boolean'}),
                ("3D View", "wm.call_menu_pie", _ev('S', ctrl=True), {"name": 'Save_Pie'}),
                ("Window", "wm.call_menu_pie", _ev('S', ctrl=True), {"name": 'Save_Pie'}),
                ("Mesh", "wm.call_menu_pie", _ev('A', shift=True), {"name": 'Add_PrimitivesPie'}),
            ],
            "disable": [
                ("Screen", "screen.screen_full_area", _ev('SPACE', ctrl=True), None),
            ],
        },
        False: {"add": [], "disable": []},
    },
}


# Extra hotkeys for the full G-Tools style.
# In mesh and curve Edit Mode, dragging does a box select. Replaces Heavypoly's old
# view3d.select_through_border, which no longer exists; without this,
# Blender's "drag = move" in the Mesh keymap grabs the click.
GTOOLS_EXTRA = [
    ("Mesh", "view3d.select_box", _ev('LEFTMOUSE', 'CLICK_DRAG'), {"mode": 'SET'}),
    ("Mesh", "view3d.select_box", _ev('LEFTMOUSE', 'CLICK_DRAG', shift=True), {"mode": 'ADD'}),
    ("Mesh", "view3d.select_box", _ev('LEFTMOUSE', 'CLICK_DRAG', ctrl=True), {"mode": 'SUB'}),
    ("Curve", "view3d.select_box", _ev('LEFTMOUSE', 'CLICK_DRAG'), {"mode": 'SET'}),
    ("Curve", "view3d.select_box", _ev('LEFTMOUSE', 'CLICK_DRAG', shift=True), {"mode": 'ADD'}),
    ("Curve", "view3d.select_box", _ev('LEFTMOUSE', 'CLICK_DRAG', ctrl=True), {"mode": 'SUB'}),
]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_addon_items = []    # (km, kmi) created in the addon keyconfig
_user_applied = []   # descriptions of what was changed in the user keyconfig:
                     # ("disable" | "modal_add", km_name, ident, event, props)


class _Defaults:
    style = 'GTOOLS'
    move_key = 'SPACE'
    extrude_key = 'SHIFT_SPACE'
    bevel_key = 'B'
    delete_key = 'X'
    subdiv_key = 'TAB'
    use_pies = True
    use_xray_select = True


def _prefs():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def _set_props(target, props):
    for name, value in (props or {}).items():
        try:
            if isinstance(value, dict):
                _set_props(getattr(target, name), value)
            else:
                setattr(target, name, value)
        except (AttributeError, TypeError, ValueError) as ex:
            print(f"G-Tools: property '{name}' ignored ({ex})")


def _props_match(target, props):
    if not props:
        # No properties: only match an item that has none set either.
        if target is None:
            return True
        return not any(
            target.is_property_set(p.identifier)
            for p in target.bl_rna.properties if p.identifier != "rna_type"
        )
    for name, value in props.items():
        if target is None or not hasattr(target, name):
            return False
        current = getattr(target, name)
        if isinstance(value, dict):
            if not _props_match(current, value):
                return False
        elif isinstance(value, float):
            if abs(current - value) > 1e-4:
                return False
        elif isinstance(value, (set, frozenset)):
            if set(current) != set(value):
                return False
        elif current != value:
            return False
    return True


def _event_match(kmi, event):
    if kmi.type != event["type"] or kmi.value != event["value"]:
        return False
    if event.get("any"):
        return kmi.any
    if kmi.any:
        return False
    for mod in ("shift", "ctrl", "alt", "oskey"):
        if bool(getattr(kmi, mod)) != bool(event.get(mod, False)):
            return False
    return kmi.key_modifier == event.get("key_modifier", "NONE")


def _find_item(km, ident, event, props, modal):
    for kmi in km.keymap_items:
        if not _event_match(kmi, event):
            continue
        name = kmi.propvalue if modal else kmi.idname
        if name != ident:
            continue
        if modal or _props_match(kmi.properties, props):
            return kmi
    return None


def _event_key(event):
    e = {k: v for k, v in event.items() if k != "repeat" and v not in (False, None)}
    return tuple(sorted(e.items()))


def _new_item(km, ident, event, modal):
    kwargs = {k: v for k, v in event.items() if k not in {"type", "value"}}
    if modal:
        return km.keymap_items.new_modal(ident, event["type"], event["value"], **kwargs)
    return km.keymap_items.new(ident, event["type"], event["value"], **kwargs)


# ---------------------------------------------------------------------------
# Build the configuration from the preferences
# ---------------------------------------------------------------------------

def _xray_props(tool, props, event):
    props = props or {}
    out = {"tool": tool}
    if "mode" in props:
        out["mode"] = props["mode"]
    if "wait_for_input" in props:
        out["wait_for_input"] = props["wait_for_input"]
    elif event["type"] not in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'}:
        out["wait_for_input"] = True  # key (B, C...): wait for the click, like Blender
    return out


def build_config(s):
    """Return (adds, disables) as lists of (km_name, ident, event, props)."""
    adds, disables = [], []
    for attr, options in FEATURES.items():
        option = options.get(getattr(s, attr))
        if option:
            adds += option["add"]
            disables += option["disable"]

    taken = {(km, _event_key(ev)) for km, _, ev, _ in adds}
    if s.style == 'GTOOLS':
        for km_name, ident, ev, props in GTOOLS_EXTRA:
            if (km_name, _event_key(ev)) not in taken:
                adds.append((km_name, ident, ev, props))
                taken.add((km_name, _event_key(ev)))
        for km_name, data in REST:
            for ident, ev, props in data["add"]:
                if (km_name, _event_key(ev)) not in taken:  # the chosen option wins
                    adds.append((km_name, ident, ev, props))
                    taken.add((km_name, _event_key(ev)))
            for ident, ev, props in data["disable"]:
                disables.append((km_name, ident, ev, props))

    if s.use_xray_select:
        disabled_keys = {(km, ident, _event_key(ev)) for km, ident, ev, _ in disables}
        converted = []
        for km_name, ident, ev, props in adds:
            if ident in SELECT_OPS:
                converted.append((km_name, XRAY_OP, ev, _xray_props(SELECT_OPS[ident], props, ev)))
            else:
                converted.append((km_name, ident, ev, props))
        adds = converted
        for km_name, ident, ev, props in XRAY_DEFAULTS:
            if (km_name, ident, _event_key(ev)) in disabled_keys:
                continue
            if (km_name, _event_key(ev)) in taken:
                continue
            adds.append((km_name, XRAY_OP, ev, _xray_props(SELECT_OPS[ident], props, ev)))
            taken.add((km_name, _event_key(ev)))
    return adds, disables


# ---------------------------------------------------------------------------
# Apply / remove
# ---------------------------------------------------------------------------

def _is_modal(km_name):
    return KM_ARGS.get(km_name, {}).get("modal", False)


def _remove_addon_items():
    for km, kmi in _addon_items:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    _addon_items.clear()


def _restore_user_side():
    kc = bpy.context.window_manager.keyconfigs.user
    for kind, km_name, ident, ev, props in reversed(_user_applied):
        km = kc.keymaps.get(km_name) if kc else None
        if km is None:
            continue
        kmi = _find_item(km, ident, ev, props, _is_modal(km_name))
        if kmi is None:
            continue
        if kind == "disable":
            kmi.active = True
        else:
            km.keymap_items.remove(kmi)
    _user_applied.clear()


def clear_all():
    _restore_user_side()
    # Blender stores user keyconfig changes as "diffs" against default + addon.
    # They must be consolidated BEFORE touching the addon items, otherwise the
    # old hotkeys would be saved as if the user had added them.
    wm = bpy.context.window_manager
    if wm.keyconfigs.user is not None:
        wm.keyconfigs.update()
    _remove_addon_items()


def rebuild():
    """Remove everything the add-on set up and apply it again from the preferences."""
    wm = bpy.context.window_manager
    clear_all()
    kc_addon = wm.keyconfigs.addon
    kc_user = wm.keyconfigs.user
    if kc_addon is None or kc_user is None:
        return
    adds, disables = build_config(_prefs() or _Defaults())

    for km_name, ident, ev, props in adds:
        if _is_modal(km_name):
            continue
        km = kc_addon.keymaps.new(name=km_name, **KM_ARGS[km_name])
        try:
            kmi = _new_item(km, ident, ev, modal=False)
        except Exception as ex:
            print(f"G-Tools: could not create {ident} in '{km_name}' ({ex})")
            continue
        _set_props(kmi.properties, props)
        _addon_items.append((km, kmi))

    wm.keyconfigs.update()

    done = set()
    for km_name, ident, ev, props in disables:
        key = (km_name, ident, _event_key(ev))
        if key in done:
            continue
        km = kc_user.keymaps.get(km_name)
        kmi = km and _find_item(km, ident, ev, props, _is_modal(km_name))
        if kmi is not None:
            kmi.active = False
            done.add(key)
            _user_applied.append(("disable", km_name, ident, ev, props))

    for km_name, ident, ev, props in adds:
        if not _is_modal(km_name):
            continue
        km = kc_user.keymaps.get(km_name)
        if km is None:
            continue
        kmi = _find_item(km, ident, ev, props, modal=True)
        if kmi is None:  # it may already exist from saved preferences
            try:
                kmi = _new_item(km, ident, ev, modal=True)
            except Exception as ex:
                print(f"G-Tools: could not create {ident} in '{km_name}' ({ex})")
                continue
        kmi.active = True
        _user_applied.append(("modal_add", km_name, ident, ev, props))


def _deferred_rebuild():
    """At Blender startup the user keyconfig is not ready yet when the add-on registers."""
    kc = bpy.context.window_manager.keyconfigs.user
    km = kc.keymaps.get("3D View") if kc else None
    if km is None or len(km.keymap_items) == 0 or _prefs() is None:
        return 0.5
    rebuild()
    return None


def _update(self, context):
    rebuild()


# ---------------------------------------------------------------------------
# UI helpers (preferences and cheatsheet)
# ---------------------------------------------------------------------------

def find_active_item(km_name, idname, props=None):
    """Return the active item from the user keyconfig (the one actually in use)."""
    kc = bpy.context.window_manager.keyconfigs.user
    km = kc.keymaps.get(km_name) if kc else None
    if km is None:
        return None
    for kmi in km.keymap_items:
        if kmi.active and kmi.idname == idname:
            if not props or all(getattr(kmi.properties, k, None) == v for k, v in props.items()):
                return kmi
    return None


def _props_match_items(a, b):
    if a is None or b is None:
        return a is None and b is None
    for p in b.bl_rna.properties:
        name = p.identifier
        if name == "rna_type":
            continue
        if a.is_property_set(name) != b.is_property_set(name):
            return False
        if b.is_property_set(name) and p.type not in {"POINTER", "COLLECTION"}:
            if getattr(a, name) != getattr(b, name):
                return False
    return True


def _user_item_for(kc_user, km, kmi, claimed):
    """The user keyconfig item matching an add-on hotkey.
    Drawing that one makes edits persist with 'Save Preferences'."""
    km_user = kc_user.keymaps.get(km.name)
    if km_user is None:
        return None, None
    fallback = None
    for item in km_user.keymap_items:
        if item.idname != kmi.idname or (km_user.name, item.id) in claimed:
            continue
        if not _props_match_items(item.properties, kmi.properties):
            continue
        if (item.type, item.value, item.any, item.shift, item.ctrl, item.alt, item.oskey) == \
           (kmi.type, kmi.value, kmi.any, kmi.shift, kmi.ctrl, kmi.alt, kmi.oskey):
            return km_user, item
        if fallback is None and item.is_user_modified:
            fallback = item
    return (km_user, fallback) if fallback else (None, None)


class GTOOLS_OT_restore_keymaps(bpy.types.Operator):
    """Reset the G-Tools hotkeys to the add-on defaults"""
    bl_idname = "gtools.restore_keymaps"
    bl_label = "Restore G-Tools Hotkeys"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        kc_user = context.window_manager.keyconfigs.user
        claimed = set()
        for km, kmi in _addon_items:
            km_user, item = _user_item_for(kc_user, km, kmi, claimed)
            if item is not None:
                claimed.add((km_user.name, item.id))
                if item.is_user_modified:
                    km_user.restore_item_to_default(item)
        rebuild()
        self.report({'INFO'}, "G-Tools hotkeys restored")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

class GTOOLS_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    style: EnumProperty(
        name="Hotkey Style",
        items=(
            ('GTOOLS', "Full G-Tools", "All Heavypoly-style hotkeys"),
            ('VANILLA', "Blender + Smart Tools", "Default Blender hotkeys, plus only the keys below"),
        ),
        default='GTOOLS', update=_update,
    )
    move_key: EnumProperty(
        name="Move",
        items=(
            ('SPACE', "Space", "Space moves, and while moving, Space locks the Y axis"),
            ('G', "G (Blender)", "G moves and Y locks the Y axis, like Blender"),
        ),
        default='SPACE', update=_update,
    )
    extrude_key: EnumProperty(
        name="Smart Extrude",
        items=(('SHIFT_SPACE', "Shift Space", ""), ('E', "E", "Replaces Blender's extrude"), ('OFF', "No Key", "")),
        default='SHIFT_SPACE', update=_update,
    )
    bevel_key: EnumProperty(
        name="Smart Bevel",
        items=(('B', "B", ""), ('CTRL_B', "Ctrl B", "Replaces Blender's bevel"), ('OFF', "No Key", "")),
        default='B', update=_update,
    )
    delete_key: EnumProperty(
        name="Smart Delete",
        items=(('X', "X", "Deletes without a menu"), ('OFF', "No Key", "X opens Blender's menu")),
        default='X', update=_update,
    )
    subdiv_key: EnumProperty(
        name="Tab",
        items=(
            ('TAB', "Subdivision Toggle", "Tab toggles subdivision (Edit Mode from the select pie)"),
            ('SELECT_PIE', "Selection Pie", "Tab opens the selection pie (like Ctrl Space)"),
            ('OFF', "Nothing (Vanilla)", "Tab toggles Edit Mode, as in Blender"),
        ),
        default='TAB', update=_update,
    )
    use_pies: BoolProperty(name="Pie menus", default=True, update=_update)
    use_xray_select: BoolProperty(
        name="X-Ray Selection",
        description="Box, lasso and circle select through the object (turns X-ray on while selecting)",
        default=True, update=_update,
    )
    show_cheatsheet: BoolProperty(
        name="Viewport Cheatsheet",
        description="'G-Tools' panel in the sidebar (N) with the main hotkeys",
        default=True,
    )
    photoshop_path: StringProperty(
        name="Photoshop",
        description="Photoshop.exe used by the Photoshop Bridge. Empty = find the newest installed version",
        default="", subtype='FILE_PATH',
    )

    tab: EnumProperty(
        items=(
            ('GTOOLS', "G-Tools Hotkeys", ""),
            ('BLENDER', "Disabled Blender Hotkeys", ""),
            ('TOOLS', "Tools Guide", "What every tool does and how to use it"),
        ),
        default='GTOOLS',
    )
    search: StringProperty(name="Search", description="Filter by name or key", options={'TEXTEDIT_UPDATE'})

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        col = box.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(self, "style")
        col.separator()
        col.prop(self, "move_key", expand=True)
        col.prop(self, "extrude_key", expand=True)
        col.prop(self, "bevel_key", expand=True)
        col.prop(self, "delete_key", expand=True)
        col.prop(self, "subdiv_key", expand=True)
        col.separator()
        col.prop(self, "use_pies")
        col.prop(self, "use_xray_select")
        col.prop(self, "show_cheatsheet")
        col.prop(self, "photoshop_path")

        layout.separator()
        row = layout.row()
        row.prop(self, "tab", expand=True)
        row = layout.row()
        row.prop(self, "search", text="", icon='VIEWZOOM')
        if self.tab == 'GTOOLS':
            row.operator(GTOOLS_OT_restore_keymaps.bl_idname, text="", icon='LOOP_BACK')
            self._draw_gtools(context, layout.column())
        elif self.tab == 'TOOLS':
            from .tools_guide import draw_guide
            draw_guide(layout.column(), context, self.search)
        else:
            self._draw_disabled(context, layout.column())

    def _matches_search(self, kmi):
        text = self.search.strip().lower()
        if not text:
            return True
        return text in kmi.name.lower() or text in kmi.idname.lower() or text in kmi.to_string().lower()

    def _draw_gtools(self, context, col):
        kc_user = context.window_manager.keyconfigs.user
        claimed = set()
        current_km = None
        for km, kmi in _addon_items:
            km_user, item = _user_item_for(kc_user, km, kmi, claimed)
            if item is None:
                continue
            claimed.add((km_user.name, item.id))
            if not self._matches_search(item):
                continue
            if km.name != current_km:
                current_km = km.name
                col.separator()
                col.label(text=km.name, icon='KEYINGSET')
            col.context_pointer_set("keymap", km_user)
            rna_keymap_ui.draw_kmi([], kc_user, km_user, item, col, 0)

    def _draw_disabled(self, context, col):
        kc_user = context.window_manager.keyconfigs.user
        rows = []
        for kind, km_name, ident, ev, props in _user_applied:
            if kind != "disable":
                continue
            km = kc_user.keymaps.get(km_name)
            kmi = km and _find_item(km, ident, ev, props, _is_modal(km_name))
            if kmi is not None and self._matches_search(kmi):
                rows.append((km, kmi))
        if not rows:
            col.label(text="No Blender hotkeys are disabled.")
            return
        col.label(text="You can re-enable any of them with its checkbox.", icon='INFO')
        current_km = None
        for km, kmi in rows:
            if km.name != current_km:
                current_km = km.name
                col.separator()
                col.label(text=km.name, icon='KEYINGSET')
            col.context_pointer_set("keymap", km)
            rna_keymap_ui.draw_kmi([], kc_user, km, kmi, col, 0)


# ---------------------------------------------------------------------------

classes = (
    GTOOLS_OT_restore_keymaps,
    GTOOLS_AddonPreferences,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.app.timers.register(_deferred_rebuild, first_interval=0.1)


def unregister():
    if bpy.app.timers.is_registered(_deferred_rebuild):
        bpy.app.timers.unregister(_deferred_rebuild)
    clear_all()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
