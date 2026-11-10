"""Tools Guide: every tool the add-on adds or changes, how to start it and what it
does, shown as text in the add-on preferences (third tab). Hotkeys are the default
Full G-Tools ones; the configurable keys are set at the top of the preferences."""

import textwrap

# (section, [(tool, how to use it, what it does), ...])
GUIDE = (
    ("Hotkeys this add-on changes", (
        ("Move", "Space (or G, set in Move Key)",
         "Space moves the selection like G. While moving, Space locks the Y axis."),
        ("Smart Extrude", "Shift Space (or E, set in Smart Extrude)",
         "Extrudes whatever is selected (vertices, edges or faces, or curve points) and moves it right away."),
        ("Smart Bevel", "B (or Ctrl B, set in Smart Bevel)",
         "Vertex mode bevels the vertices. Face mode bevels the border of the selected faces. Edge mode "
         "bevels the edges with clamping and arc miters. Doubles are merged afterwards."),
        ("Smart Delete", "X",
         "Deletes without a menu. Object Mode deletes the objects; deleting a cutter also removes its "
         "boolean modifiers. Edit Mode deletes faces in Face mode, vertices otherwise (curve points, "
         "Grease Pencil points and metaball elements too)."),
        ("Subdivision Toggle", "Tab (Tab Key option)",
         "Adds a Subdivision Surface modifier (level 3, named Subsurf_Base) to the selected objects, or "
         "turns it on and off. The other Tab option opens the Select pie instead."),
        ("Smart Slide / Push", "Shift G in Edit Mode",
         "Vertices and edges: slide along their edges as infinite lines, past the next vertex and past "
         "mesh borders (the opposite edge, extended). Faces: push them along their edges, so they ride "
         "up or down edges that converge or diverge and stay flat. The path is drawn while you drag. "
         "L locks the slide axis: drag back past the start and it keeps extending the same edges "
         "instead of jumping to the other side (L again unlocks). Wheel / Tab steps to the next edge "
         "direction and locks it, without aiming the mouse. E: Even. Shift: precision. Ctrl: steps "
         "of 0.1. The exact value is in the redo panel."),
        ("X-Ray Selection", "Box, lasso and circle select (X-Ray Selection option)",
         "Selects through the object: X-ray is turned on while you select and off again afterwards."),
        ("Full G-Tools extras", "Hotkey Style: Full G-Tools",
         "Ctrl D draw primitives, P separate and select, Ctrl R loop cut, drag to box select in Edit Mode, "
         "and the rest of the Heavypoly-style keys. The G-Tools Hotkeys tab lists them all and lets you "
         "change them; the Disabled Blender Hotkeys tab shows what they replace."),
    )),
    ("Pie menus", (
        ("Select / Modes pie", "Ctrl Space (or Tab, set in Tab Key)",
         "Vert / Edge / Face (also enters Edit Mode), Object, and Sculpt / paint modes (Grease Pencil "
         "modes for Grease Pencil objects). Similar, Similar Name, Select Collection, Select Flat, Border, "
         "Split to New Object, Join, Set / Remove Parent. Also opens Modifier Scroll (Object Mode), Curve "
         "Align and Loop Resample (Edit Mode), and Create Patch from Curves (with 2 to 4 curves selected)."),
        ("View pie", "V",
         "Front / Back / Left / Right / Top / Bottom, Align, Perspective / Ortho, Zoom to Selected and "
         "Walk. Camera: new camera at the view, view the camera, lock the camera to the view. Also "
         "render presets for animations."),
        ("Shading pie", "Z",
         "Wireframe, solid, material and rendered shading, background wire, Smart Shade Smooth (auto "
         "smooth on the selected objects), isolate (local view) and toggle background hide. Also render "
         "image or animation, render and world settings, and bake / free lighting."),
        ("Boolean pie", "Ctrl B",
         "Live booleans: select the cutter(s), then the base (active). Subtract, Add, Intersect, Slice, "
         "Cutline, Laser, Wrap, Subtract Inset, plus Sharp Subtract, Quick Decal and Panel Line. Toggle "
         "Cutters shows or hides them; Apply Booleans (optionally on a copy) and Apply All Modifiers. "
         "Cutters are named <base>_Cutter_N and kept in the Cutters collection."),
        ("Add pie", "Shift A in Edit Mode",
         "Cubes (big / small), cylinders (6 to 128 sides), spheres, a curve, a light or a Grease Pencil. In "
         "Face mode the new mesh is aligned to the selected faces; otherwise it goes on the selection, or "
         "at the 3D cursor if nothing is selected."),
        ("Save pie", "Ctrl S", "Save, Save As, Open, New file and Link."),
    )),
    ("Modeling tools", (
        ("Smart Cut", "Toolbar tool (Object and Edit Mode)",
         "Draw a line (or click a polyline) to cut. The shaded side is removed; F flips it. Edit Mode cuts "
         "the mesh, fills the hole and marks the new edges sharp. Object Mode adds a movable box cutter as "
         "a live Sharp Subtract. Tool header: Align to Face (cut into the face under the mouse), Before "
         "Mirror, and Add (extrude the drawn shape instead of cutting)."),
        ("Sharp Subtract", "Boolean pie: select cutter(s), then the base",
         "A live boolean done with Geometry Nodes. The edges of the cut are marked sharp, and each cutter "
         "gets its own modifier."),
        ("Edge Bevel", "Toolbar tool (Object Mode)",
         "Hover a sharp edge and click to bevel it non-destructively. The bevel is a boolean, so it cuts "
         "through n-gons. Alt takes the whole loop. X / Delete over a bevel removes it."),
        ("Loop Resample", "Select pie in Edit Mode, or the Edge / Face menus",
         "Loops (a cylinder side): the wheel changes the number of sides and keeps the shape. Bevels (a "
         "strip, or bevels meeting at corners): the wheel changes the segments, R switches the wheel to "
         "the radius, F flips the profile and 0 removes the bevel. Enter or click applies. Everything is "
         "tried on a copy first: geometry it can't rebuild cleanly is left untouched, with a message."),
        ("Curve Align", "Select pie or Vertex menu in Edit Mode, or its toolbar tool",
         "Shapes a row of vertices with a Bezier curve, or a grid of faces with a Bezier patch. Control "
         "points edit like vertices (G / R / S, click to select)."),
        ("Smart Slide / Push", "Shift G in Edit Mode", "See Hotkeys above."),
        ("Draw Primitives", "Ctrl D (Full G-Tools)",
         "Drag on a surface to draw a box or another primitive. S changes the shape, X makes it a live "
         "boolean, B sets the cut type, Shift extrudes, Alt rotates, Space finishes."),
        ("Separate and Select", "P in Edit Mode (Full G-Tools), or the Select pie",
         "Splits the selection into a new object and selects that object."),
        ("Create Patch from Curves", "Select pie in Object Mode, with 2 to 4 curves selected",
         "Makes a live surface between the curves, like a CAD boundary patch. 4 curves give a Coons "
         "patch, 3 a triangular patch, 2 a loft. Moving or editing the curves updates it. The modifier "
         "sets which curve is which side, reversing, resolution, flipped normals and smooth shading."),
        ("Ornament", "Shift A > Ornament (Object Mode)",
         "A curve with a moulding profile swept along it (Crown, Bead, Baroque). If a mesh was selected, "
         "the ornament sticks to its surface."),
    )),
    ("Hard-surface details", (
        ("Modifier Scroll", "Select pie in Object Mode",
         "The wheel goes through the booleans of the active object, showing only the current cutter. "
         "Click selects that cutter to edit it; Esc goes back."),
        ("Quick Decal", "Boolean pie: select decal(s), then the target",
         "Presses objects onto a surface like decals, keeping the shading of their volume (a flattened "
         "screw still looks round). Height, offset and how the border blends in can be adjusted."),
        ("Panel Line", "Boolean pie: select a cutter, then the target",
         "A live panel line along where the cutter meets the surface, with a fake V-groove in the normals."),
    )),
    ("Viewport and render", (
        ("Hotkey cheatsheet", "Sidebar (N) > G-Tools (Viewport Cheatsheet option)",
         "Shows the main hotkeys as they are actually set."),
        ("G TOOLS RENDER panel", "Properties > Render",
         "A panel with quick access to common render settings."),
    )),
)


def _wrap(text, width):
    return textwrap.wrap(text, max(30, width)) or [""]


def draw_guide(layout, context, search=""):
    """Draw the guide, filtered by `search` (any word of the tool's name or text)."""
    text = search.strip().lower()
    region = getattr(context, "region", None)
    chars = int((region.width if region else 900) / 7.5) - 10
    shown = 0
    for section, tools in GUIDE:
        rows = [t for t in tools if not text or any(text in part.lower() for part in t)]
        if not rows:
            continue
        box = layout.box()
        box.label(text=section, icon='TOOL_SETTINGS')
        for name, how, what in rows:
            shown += 1
            col = box.column(align=True)
            col.separator()
            row = col.row()
            row.label(text=name, icon='DOT')
            sub = col.column(align=True)
            sub.scale_y = 0.85
            for line in _wrap(f"How: {how}", chars):
                sub.label(text=line)
            for line in _wrap(what, chars):
                sub.label(text=line)
    if not shown:
        layout.label(text=f"No tool matches '{search}'", icon='INFO')
