"""Curve Align (Edit Mode): shape a row of vertices with a Bezier curve, or a grid
of vertices / faces with a Bezier patch.

Start it from the Select pie (Ctrl Space in Edit Mode), the G-Tools sidebar panel,
Vertex menu > Curve Align, or the toolbar tool (select, then double click / Enter
/ Space). A row of vertices gets a curve; whole faces selected (a grid, a single
strip of quads, any faces) always get a patch. The control points start fitted to the vertices, which then follow.

Control points edit like vertices:
- Click / Shift click selects (drag moves right away), A selects all / none.
- G (or Space) moves, R rotates, S scales, around your Pivot Point setting.
- While transforming: X / Y / Z lock a global axis (a line shows it), type a
  number for an exact value (S X 0 flattens on X), Ctrl toggles snapping (move
  snaps to mesh vertices, rotate to 5 degrees, scale to 0.1; Blender's snap
  toggle sets the default), Shift is finer, click / Enter confirms, Esc cancels.
- Ctrl Wheel (or numpad + / -): more / fewer control points (3 to 8). The plain
  wheel zooms the view as usual.  L: straighten the curve.
  F: flatten (onto the best-fit plane).  E: even spacing (curve).
- Drag on empty space: box select points (Shift adds, Ctrl removes).
- Ctrl Z / Ctrl Shift Z: undo / redo inside the tool.
- Move follows the add-on's Move setting: Space style (Space moves, Space again
  locks Y) or G style (G moves, Y locks Y).
- Enter, or opening a pie (Ctrl Space, Z, V), finishes and keeps the result.
  Esc cancels everything.
"""

import math

import bmesh
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from .sharp_subtract import hint_panel_origin

PICK_PX = 14   # how close (pixels) the mouse must be to grab a control point
SNAP_PX = 20   # how close to a mesh vertex a moved point snaps to it
MIN_POINTS, MAX_POINTS = 3, 8


# ---------------------------------------------------------------------------
# Bezier math
# ---------------------------------------------------------------------------

def _binomial(n, k):
    return math.comb(n, k)


def bezier(points, t):
    n = len(points) - 1
    p = Vector((0.0, 0.0, 0.0))
    for i, c in enumerate(points):
        p += c * (_binomial(n, i) * (1 - t) ** (n - i) * t ** i)
    return p


def fit_bezier(samples, params, count):
    """Control points (count of them, ends fixed on the first / last sample) that best
    fit the samples at the given curve parameters (least squares)."""
    a, b = samples[0], samples[-1]
    if count <= 2 or len(samples) < 3:
        return [a.lerp(b, i / (count - 1)) for i in range(count)]
    n = count - 1
    inner = list(range(1, n))
    # Normal equations M x = r, one solve per coordinate (M is small).
    m = [[0.0] * len(inner) for _ in inner]
    r = [[0.0, 0.0, 0.0] for _ in inner]
    for s, t in zip(samples, params):
        basis = [_binomial(n, i) * (1 - t) ** (n - i) * t ** i for i in range(count)]
        target = s - a * basis[0] - b * basis[-1]
        for row, i in enumerate(inner):
            for col, j in enumerate(inner):
                m[row][col] += basis[i] * basis[j]
            for k in range(3):
                r[row][k] += basis[i] * target[k]
    solution = _solve(m, r)
    if solution is None:
        return [a.lerp(b, i / n) for i in range(count)]
    return [a] + [Vector(x) for x in solution] + [b]


def _solve(m, r):
    """Gaussian elimination with partial pivoting; r has 3 columns."""
    size = len(m)
    m = [row[:] + rhs[:] for row, rhs in zip(m, r)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda i: abs(m[i][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for row in range(size):
            if row != col:
                f = m[row][col] / m[col][col]
                for k in range(col, size + 3):
                    m[row][k] -= f * m[col][k]
    return [[m[i][size + k] / m[i][i] for k in range(3)] for i in range(size)]


def _arc_params(points):
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + (b - a).length)
    total = lengths[-1] or 1.0
    return [x / total for x in lengths]


def _curve_lookup(control, samples=256):
    """Points along the curve with their arc length, to place vertices by length."""
    pts = [bezier(control, i / samples) for i in range(samples + 1)]
    return pts, _arc_params(pts)


def _at_length(pts, lengths, u):
    """Point on the sampled curve at fraction u (0..1) of its length."""
    if u <= 0:
        return pts[0].copy()
    if u >= 1:
        return pts[-1].copy()
    lo, hi = 0, len(lengths) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if lengths[mid] < u:
            lo = mid
        else:
            hi = mid
    span = lengths[hi] - lengths[lo] or 1.0
    return pts[lo].lerp(pts[hi], (u - lengths[lo]) / span)


def _basis(n, t):
    return [math.comb(n, i) * (1 - t) ** (n - i) * t ** i for i in range(n + 1)]


def patch_point(control, cu, cv, u, v):
    bu, bv = _basis(cu - 1, u), _basis(cv - 1, v)
    p = Vector((0.0, 0.0, 0.0))
    for j in range(cv):
        for i in range(cu):
            p += control[j * cu + i] * (bu[i] * bv[j])
    return p


def fit_patch(samples, uvs, cu, cv):
    """Least-squares control grid (cu x cv) for samples at (u, v)."""
    size = cu * cv
    m = [[0.0] * size for _ in range(size)]
    r = [[0.0, 0.0, 0.0] for _ in range(size)]
    for s, (u, v) in zip(samples, uvs):
        bu, bv = _basis(cu - 1, u), _basis(cv - 1, v)
        w = [bu[k % cu] * bv[k // cu] for k in range(size)]
        for a in range(size):
            if w[a] == 0.0:
                continue
            for b in range(size):
                m[a][b] += w[a] * w[b]
            for k in range(3):
                r[a][k] += w[a] * s[k]
    for a in range(size):  # a touch of damping keeps sparse fits stable
        m[a][a] += 1e-9
    solution = _solve(m, r)
    if solution is None:
        lo = Vector([min(p[k] for p in samples) for k in range(3)])
        hi = Vector([max(p[k] for p in samples) for k in range(3)])
        return [lo.lerp(hi, 0.5) for _ in range(size)]
    return [Vector(x) for x in solution]


def _main_direction(points, center, normal):
    """Direction of largest spread inside the plane (power iteration)."""
    cov = Matrix(((0.0,) * 3,) * 3)
    for p in points:
        d = p - center
        for i in range(3):
            for j in range(3):
                cov[i][j] += d[i] * d[j]
    u = normal.orthogonal().normalized()
    for _ in range(32):
        nxt = cov @ u
        nxt -= normal * nxt.dot(normal)
        if nxt.length < 1e-12:
            break
        u = nxt.normalized()
    return u


def planar_params(points):
    """(u, v) in 0..1 for each point, from its position on the best-fit plane.
    Used for face selections that are not a clean grid of quads."""
    center, normal = best_plane(points)
    u_axis = _main_direction(points, center, normal)
    v_axis = normal.cross(u_axis).normalized()
    us = [(p - center).dot(u_axis) for p in points]
    vs = [(p - center).dot(v_axis) for p in points]
    du, dv = (max(us) - min(us)) or 1.0, (max(vs) - min(vs)) or 1.0
    return [((a - min(us)) / du, (b - min(vs)) / dv) for a, b in zip(us, vs)]


def best_plane(points):
    """(center, normal) of the plane closest to the points."""
    center = sum(points, Vector()) / len(points)
    cov = Matrix(((0.0,) * 3,) * 3)
    for p in points:
        d = p - center
        for i in range(3):
            for j in range(3):
                cov[i][j] += d[i] * d[j]
    # The normal is the direction of least spread: power iteration on the inverse.
    normal = Vector((0.3, 0.5, 0.8))
    try:
        inv = cov.inverted()
        for _ in range(32):
            normal = (inv @ normal).normalized()
    except ValueError:  # flat already: the null direction is the normal
        for axis in (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))):
            if (cov @ axis).length < 1e-9:
                return center, axis
    return center, normal


# ---------------------------------------------------------------------------
# Selection: the vertices in order (row) or as a grid
# ---------------------------------------------------------------------------

def ordered_selection(bm):
    """Selected vertices in order along the row: following selected edges when they
    make one chain, otherwise along the line between the two farthest vertices."""
    verts = [v for v in bm.verts if v.select]
    if len(verts) < 2:
        return verts
    chosen = set(verts)
    links = {v: [e.other_vert(v) for e in v.link_edges if e.select and e.other_vert(v) in chosen] for v in verts}
    ends = [v for v in verts if len(links[v]) == 1]
    if len(ends) == 2 and all(len(links[v]) <= 2 for v in verts):
        order, prev, cur = [ends[0]], None, ends[0]
        while True:
            nxt = [v for v in links[cur] if v is not prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            order.append(cur)
        if len(order) == len(verts):
            return order
    # Not one clean chain: sort along the longest direction.
    a = max(verts, key=lambda v: (v.co - verts[0].co).length)
    b = max(verts, key=lambda v: (v.co - a.co).length)
    axis = (b.co - a.co).normalized()
    return sorted(verts, key=lambda v: (v.co - a.co).dot(axis))


def grid_selection(bm):
    """Rows of vertices if the selection is a grid of quads (all verts selected),
    else None."""
    faces = [f for f in bm.faces if all(v.select for v in f.verts)]
    if not faces or any(len(f.verts) != 4 for f in faces):
        return None
    face_set = set(faces)
    verts = {v for f in faces for v in f.verts}
    links = {v: set() for v in verts}
    for f in faces:
        for e in f.edges:
            a, b = e.verts
            links[a].add(b)
            links[b].add(a)
    count = {v: sum(1 for f in v.link_faces if f in face_set) for v in verts}
    corners = [v for v in verts if count[v] == 1]
    if len(corners) != 4:
        return None

    def boundary(e):
        return sum(1 for f in e.link_faces if f in face_set) == 1

    def walk(start, first):
        path, prev, cur = [start], start, first
        while True:
            path.append(cur)
            if cur in corners and cur is not start:
                return path
            nxt = [e.other_vert(cur) for e in cur.link_edges
                   if boundary(e) and e.other_vert(cur) is not prev and e.other_vert(cur) in verts]
            if len(nxt) != 1:
                return None
            prev, cur = cur, nxt[0]

    c0 = corners[0]
    first = [e.other_vert(c0) for e in c0.link_edges if boundary(e) and e.other_vert(c0) in verts]
    if len(first) != 2:
        return None
    row0, col0 = walk(c0, first[0]), walk(c0, first[1])
    if row0 is None or col0 is None:
        return None
    rows = [row0]
    used = set(row0)
    for k in range(1, len(col0)):
        prev_row = rows[-1]
        row = [col0[k]]
        used.add(col0[k])
        for j in range(1, len(prev_row)):
            options = [v for v in links[prev_row[j]] if v not in used and v in links[row[-1]]]
            if len(options) != 1:
                return None
            row.append(options[0])
            used.add(options[0])
        rows.append(row)
    if len(used) != len(verts):
        return None
    return rows


def _grid_params(rows_world):
    """u per column, v per row: average normalised distance along rows / columns."""
    nr, nc = len(rows_world), len(rows_world[0])
    us = [0.0] * nc
    for row in rows_world:
        for j, t in enumerate(_arc_params(row)):
            us[j] += t / nr
    vs = [0.0] * nr
    for j in range(nc):
        for i, t in enumerate(_arc_params([rows_world[i][j] for i in range(nr)])):
            vs[i] += t / nc
    return us, vs


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

AXES = {'X': Vector((1, 0, 0)), 'Y': Vector((0, 1, 0)), 'Z': Vector((0, 0, 1))}
AXIS_COLORS = {'X': (1.0, 0.25, 0.3, 0.9), 'Y': (0.5, 0.9, 0.2, 0.9), 'Z': (0.25, 0.55, 1.0, 0.9)}


def _lines(context, pairs, color, width):
    seg = []
    for a, b in pairs:
        seg += [tuple(a), tuple(b)]
    if not seg:
        return
    line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    line.uniform_float("viewportSize", (context.region.width, context.region.height))
    line.uniform_float("lineWidth", width)
    line.uniform_float("color", color)
    batch_for_shader(line, 'LINES', {"pos": seg}).draw(line)


def _draw_view(op, context):
    gpu.state.blend_set('ALPHA')
    if op.mode == 'CURVE':
        curve = [bezier(op.control, i / 64) for i in range(65)]
        _lines(context, zip(curve, curve[1:]), (0.3, 0.85, 1.0, 0.95), 2.0)
        _lines(context, zip(op.control, op.control[1:]), (1.0, 0.8, 0.2, 0.5), 1.0)
    else:
        cu, cv = op.cu, op.cv
        # Only the outline of the patch (its four border curves), no inner lines.
        border = []
        for t in (0.0, 1.0):
            row = [patch_point(op.control, cu, cv, s / 32, t) for s in range(33)]
            col = [patch_point(op.control, cu, cv, t, s / 32) for s in range(33)]
            border += list(zip(row, row[1:])) + list(zip(col, col[1:]))
        _lines(context, border, (0.3, 0.85, 1.0, 0.95), 2.0)
        net = []
        for j in range(cv):
            for i in range(cu):
                p = op.control[j * cu + i]
                if i + 1 < cu:
                    net.append((p, op.control[j * cu + i + 1]))
                if j + 1 < cv:
                    net.append((p, op.control[(j + 1) * cu + i]))
        _lines(context, net, (1.0, 0.8, 0.2, 0.5), 1.0)
    if op.transform and op.axis:  # like Blender's own transform: show the locked axis
        axis = AXES[op.axis]
        far = op.size * 50
        _lines(context, [(op.pivot - axis * far, op.pivot + axis * far)], AXIS_COLORS[op.axis], 1.5)
    gpu.state.blend_set('NONE')


def _draw_pixel(op, context):
    import blf
    region, rv3d = context.region, context.region_data
    gpu.state.blend_set('ALPHA')
    shader = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    for i, c in enumerate(op.control):
        p = view3d_utils.location_3d_to_region_2d(region, rv3d, c)
        if p is None:
            continue
        if i in op.selected:
            color, size = (1.0, 0.55, 0.1, 1.0), 13.0
        elif i == op.hover:
            color, size = (1.0, 0.8, 0.2, 1.0), 13.0
        else:
            color, size = (1.0, 1.0, 1.0, 0.95), 10.0
        gpu.state.point_size_set(size)
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'POINTS', {"pos": [tuple(p)]}).draw(shader)
    if op.snap_target is not None:
        p = view3d_utils.location_3d_to_region_2d(region, rv3d, op.snap_target)
        if p is not None:
            gpu.state.point_size_set(16.0)
            shader.uniform_float("color", (1.0, 0.3, 1.0, 1.0))
            batch_for_shader(shader, 'POINTS', {"pos": [tuple(p)]}).draw(shader)

    if op.box is not None:
        (x0, y0), (x1, y1) = op.box
        rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        flat = gpu.shader.from_builtin('UNIFORM_COLOR')
        fill = batch_for_shader(flat, 'TRIS', {"pos": rect}, indices=((0, 1, 2), (0, 2, 3)))
        flat.uniform_float("color", (1.0, 1.0, 1.0, 0.06))
        fill.draw(flat)
        line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        line.uniform_float("viewportSize", (region.width, region.height))
        line.uniform_float("lineWidth", 1.0)
        line.uniform_float("color", (1.0, 1.0, 1.0, 0.7))
        seg = [rect[0], rect[1], rect[1], rect[2], rect[2], rect[3], rect[3], rect[0]]
        batch_for_shader(line, 'LINES', {"pos": seg}).draw(line)

    name = "Curve Align" if op.mode == 'CURVE' else "Patch Align"
    if op.transform:
        verb = {'MOVE': "Move", 'ROTATE': "Rotate", 'SCALE': "Scale"}[op.transform]
        title = f"{name}  ·  {verb}"
        if op.axis:
            title += f"  ·  axis {op.axis}"
        if op.numeric:
            title += f"  ·  {op.numeric}"
        if op.snapping:
            title += "  ·  snap"
        hints = [
            ("Mouse", verb.lower()),
            ("X / Y / Z" + (" (Space: Y)" if op.space_style and op.transform == 'MOVE' else ""),
             "lock to an axis (again: free)"),
            ("Numbers", "exact value (S X 0 = flatten on X)"),
            ("Ctrl", "snapping on / off"),
            ("Shift", "finer"),
            ("Click / Enter", "confirm"),
            ("Esc / Right click", "cancel this transform"),
        ]
    else:
        if op.mode == 'CURVE':
            title = f"{name}  ·  {len(op.verts)} vertices  ·  {len(op.control)} points"
        else:
            title = f"{name}  ·  {len(op.verts)} vertices  ·  {op.cu} x {op.cv} points"
        hints = [
            ("Click / Shift click / A", "select points (drag a point: move it)"),
            ("Drag on empty space", "box select (Shift add, Ctrl remove)"),
            (("Space / G" if op.space_style else "G") + ", R, S", "move, rotate, scale points"),
            ("Ctrl Z / Ctrl Shift Z", "undo / redo"),
            ("Ctrl Wheel  (or + / -)", "more / fewer points"),
        ]
        if op.mode == 'CURVE':
            hints += [("L", "straighten"), ("F", "flatten"),
                      ("E", "spacing: " + ("even" if op.even else "as it was"))]
        else:
            hints += [("F", "flatten the patch")]
        hints += [("Enter, or open a pie (Ctrl Space...)", "done"), ("Esc", "cancel")]
    font, size = 0, 12
    blf.size(font, size)
    line_h = size * 1.55
    key_w = max(blf.dimensions(font, k)[0] for k, _ in hints) + 14
    width = max(blf.dimensions(font, title)[0], key_w + max(blf.dimensions(font, a)[0] for _, a in hints)) + 20
    height = line_h * (len(hints) + 1) + 14
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
    for key, action in hints:
        ty -= line_h
        blf.color(font, 1, 1, 1, 0.95)
        blf.position(font, x + 10, ty, 0)
        blf.draw(font, key)
        blf.color(font, 1, 1, 1, 0.6)
        blf.position(font, x + 10 + key_w, ty, 0)
        blf.draw(font, action)
    gpu.state.blend_set('NONE')


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

PIE_KEYS = {('SPACE', True), ('Z', False), ('V', False)}  # (key, ctrl): opening these pies finishes
NUMERIC = {'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4', 'FIVE': '5', 'SIX': '6',
           'SEVEN': '7', 'EIGHT': '8', 'NINE': '9', 'PERIOD': '.', 'MINUS': '-',
           'NUMPAD_0': '0', 'NUMPAD_1': '1', 'NUMPAD_2': '2', 'NUMPAD_3': '3', 'NUMPAD_4': '4',
           'NUMPAD_5': '5', 'NUMPAD_6': '6', 'NUMPAD_7': '7', 'NUMPAD_8': '8', 'NUMPAD_9': '9',
           'NUMPAD_PERIOD': '.', 'NUMPAD_MINUS': '-'}


class GTOOLS_OT_curve_align(bpy.types.Operator):
    """Shape the selected vertices with a Bezier curve (a row) or a Bezier patch (a grid)"""
    bl_idname = "mesh.gtools_curve_align"
    bl_label = "Curve Align"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.area is not None and context.area.type == 'VIEW_3D'
                and obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT')

    def invoke(self, context, event):
        obj = context.active_object
        self.obj = obj
        self.bm = bmesh.from_edit_mesh(obj.data)
        self.matrix = obj.matrix_world.copy()
        self.inverse = self.matrix.inverted()
        faces = [f for f in self.bm.faces if all(v.select for v in f.verts)]
        rows = grid_selection(self.bm) if faces else None
        if faces:
            # Whole faces selected (even a single strip): always a patch.
            self.mode = 'PATCH'
            if rows is not None:  # a clean grid of quads: parameters follow its rows / columns
                self.verts = [v for row in rows for v in row]
                world_rows = [[self.matrix @ v.co for v in row] for row in rows]
                us, vs = _grid_params(world_rows)
                self.uvs = [(us[j], vs[i]) for i in range(len(rows)) for j in range(len(rows[0]))]
                # No more control points across than there are vertex rows / columns.
                self.max_u, self.max_v = len(rows[0]), len(rows)
            else:  # any other faces: parameters from the best-fit plane
                self.verts = list({v for f in faces for v in f.verts})
                self.uvs = planar_params([self.matrix @ v.co for v in self.verts])
                self.max_u = self.max_v = MAX_POINTS - 2
            if self.max_v > self.max_u:  # keep u along the long side
                self.uvs = [(b, a) for a, b in self.uvs]
                self.max_u, self.max_v = self.max_v, self.max_u
            self.cu = min(3, self.max_u)
            self.cv = min(3, self.max_v)
            self.control = fit_patch([self.matrix @ v.co for v in self.verts], self.uvs, self.cu, self.cv)
        else:
            self.mode = 'CURVE'
            self.verts = ordered_selection(self.bm)
            if len(self.verts) < 3:
                self.report({'WARNING'}, "Curve Align: select a row of at least 3 vertices, or a grid of quads")
                return {'CANCELLED'}
            world = [self.matrix @ v.co for v in self.verts]
            self.params = _arc_params(world)  # where each vertex sits along the row
            self.count = 3
            self.control = fit_bezier(world, self.params, self.count)
        self.original = [v.co.copy() for v in self.verts]
        world_all = [self.matrix @ co for co in self.original]
        lo = Vector([min(p[k] for p in world_all) for k in range(3)])
        hi = Vector([max(p[k] for p in world_all) for k in range(3)])
        self.size = max((hi - lo).length, 1e-3)
        others = set(self.verts)
        self.snap_points = [self.matrix @ v.co for v in self.bm.verts if v not in others]
        self.even = False
        self.selected = set()
        self.hover = None
        self.dragging = None
        self.transform = None  # 'MOVE' / 'ROTATE' / 'SCALE'
        self.axis = None
        self.numeric = ""
        self.snapping = False
        self.snap_target = None
        self.pivot = Vector()
        self._last_picked = None
        self.undo_stack, self.redo_stack = [], []
        self.box = None  # (start, end) while drag-selecting points
        # Move / Y-axis keys follow the add-on's hotkey setting (Space style or G style).
        prefs = context.preferences.addons.get(__package__)
        self.space_style = prefs is None or getattr(prefs.preferences, "move_key", 'SPACE') == 'SPACE'
        self._handles = (
            bpy.types.SpaceView3D.draw_handler_add(_draw_view, (self, context), 'WINDOW', 'POST_VIEW'),
            bpy.types.SpaceView3D.draw_handler_add(_draw_pixel, (self, context), 'WINDOW', 'POST_PIXEL'),
        )
        context.window_manager.modal_handler_add(self)
        self._status(context)
        self._apply()
        return {'RUNNING_MODAL'}

    # -- helpers -------------------------------------------------------------

    def _status(self, context):
        if self.transform:
            text = ("Move the mouse  |  X / Y / Z: axis  |  numbers: exact value  |  Ctrl: snap  |  "
                    "Shift: finer  |  Click / Enter: confirm  |  Esc: cancel")
        else:
            text = ("Click / drag points, drag a box  |  " + ("Space / G" if self.space_style else "G")
                    + ": move  |  R: rotate  |  S: scale  |  Ctrl Z: undo  |  Ctrl Wheel: points  |  "
                    + ("L: straight  |  F: flat  |  E: spacing  |  " if self.mode == 'CURVE' else "F: flatten  |  ")
                    + "Enter / pie: done  |  Esc: cancel")
        context.workspace.status_text_set(text)

    def _snapshot(self):
        return ([c.copy() for c in self.control], getattr(self, "count", 0), getattr(self, "cu", 0),
                getattr(self, "cv", 0), self.even)

    def _push_undo(self):
        """Remember the shape before a change (Ctrl Z inside the tool)."""
        self.undo_stack.append(self._snapshot())
        del self.undo_stack[:-64]
        self.redo_stack.clear()

    def _restore(self, state):
        control, count, cu, cv, even = state
        self.control = [c.copy() for c in control]
        if self.mode == 'CURVE':
            self.count = count
        else:
            self.cu, self.cv = cu, cv
        self.even = even
        self.selected = {i for i in self.selected if i < len(self.control)}
        self.hover = None
        self._apply()

    def _undo(self, redo=False):
        source, target = (self.redo_stack, self.undo_stack) if redo else (self.undo_stack, self.redo_stack)
        if source:
            target.append(self._snapshot())
            self._restore(source.pop())

    def _apply(self):
        if self.mode == 'CURVE':
            pts, lengths = _curve_lookup(self.control)
            n = len(self.verts)
            for i, v in enumerate(self.verts):
                u = i / (n - 1) if self.even else self.params[i]
                v.co = self.inverse @ _at_length(pts, lengths, u)
        else:
            for v, (u, w) in zip(self.verts, self.uvs):
                v.co = self.inverse @ patch_point(self.control, self.cu, self.cv, u, w)
        bmesh.update_edit_mesh(self.obj.data, loop_triangles=False, destructive=False)

    def _resolution(self, step):
        """More / fewer control points, refitted to the current shape."""
        if self.mode == 'CURVE':
            count = max(MIN_POINTS, min(MAX_POINTS, self.count + step))
            if count == self.count:
                return
            self._push_undo()
            pts, lengths = _curve_lookup(self.control)
            samples = [_at_length(pts, lengths, i / 32) for i in range(33)]
            self.count = count
            self.control = fit_bezier(samples, [i / 32 for i in range(33)], count)
        else:
            top = MAX_POINTS - 2
            cu = max(min(MIN_POINTS, self.max_u), min(top, self.max_u, self.cu + step))
            cv = max(min(MIN_POINTS, self.max_v), min(top, self.max_v, self.cv + step))
            if (cu, cv) == (self.cu, self.cv):
                return
            self._push_undo()
            uvs = [(i / 10, j / 10) for j in range(11) for i in range(11)]
            samples = [patch_point(self.control, self.cu, self.cv, u, v) for u, v in uvs]
            self.cu, self.cv = cu, cv
            self.control = fit_patch(samples, uvs, cu, cv)
        self.selected.clear()
        self.hover = None
        self._apply()

    def _straighten(self):
        if self.mode != 'CURVE':
            return
        self._push_undo()
        a, b = self.control[0], self.control[-1]
        n = len(self.control) - 1
        self.control = [a.lerp(b, i / n) for i in range(n + 1)]
        self._apply()

    def _flatten(self):
        self._push_undo()
        targets = sorted(self.selected) if len(self.selected) >= 3 else range(len(self.control))
        pts = [self.control[i] for i in targets]
        center, normal = best_plane(pts)
        for i in targets:
            p = self.control[i]
            self.control[i] = p - normal * (p - center).dot(normal)
        self._apply()

    def _point_under(self, context, xy):
        region, rv3d = context.region, context.region_data
        best = None
        for i, c in enumerate(self.control):
            p = view3d_utils.location_3d_to_region_2d(region, rv3d, c)
            if p is None:
                continue
            d = (p - Vector(xy)).length
            if d <= PICK_PX and (best is None or d < best[0]):
                best = (d, i)
        return best[1] if best else None

    def _finish(self, context):
        for h in self._handles:
            bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        context.workspace.status_text_set(None)
        context.area.tag_redraw()

    def _cancel_all(self, context):
        for v, co in zip(self.verts, self.original):
            v.co = co
        bmesh.update_edit_mesh(self.obj.data, loop_triangles=False, destructive=False)
        self._finish(context)
        return {'CANCELLED'}

    # -- G / Space, R, S -----------------------------------------------------

    def _start_transform(self, context, kind, xy):
        if self.selected:
            targets = sorted(self.selected)
        elif self.hover is not None and kind == 'MOVE':
            targets = [self.hover]
        else:
            targets = list(range(len(self.control)))
        self._push_undo()
        self.transform = kind
        self.axis = None
        self.numeric = ""
        self.targets = targets
        self.start_points = [c.copy() for c in self.control]
        self.start_mouse = Vector(xy)
        tool_settings = context.scene.tool_settings
        pivot_mode = tool_settings.transform_pivot_point
        if pivot_mode == 'CURSOR':
            self.pivot = context.scene.cursor.location.copy()
        elif pivot_mode == 'ACTIVE_ELEMENT' and self._last_picked is not None:
            self.pivot = self.start_points[self._last_picked].copy()
        elif pivot_mode == 'BOUNDING_BOX_CENTER':
            pts = [self.start_points[i] for i in targets]
            lo = Vector([min(p[k] for p in pts) for k in range(3)])
            hi = Vector([max(p[k] for p in pts) for k in range(3)])
            self.pivot = (lo + hi) / 2
        else:  # median (and individual origins, the same for single points)
            self.pivot = sum((self.start_points[i] for i in targets), Vector()) / len(targets)
        self._snap_default = tool_settings.use_snap
        self._status(context)

    def _number(self):
        try:
            return float(self.numeric) if self.numeric not in {"", "-", ".", "-."} else None
        except ValueError:
            return None

    def _update_transform(self, context, event):
        region, rv3d = context.region, context.region_data
        xy = Vector((event.mouse_region_x, event.mouse_region_y))
        if event.shift:
            xy = self.start_mouse.lerp(xy, 0.1)
        self.snapping = self._snap_default != bool(event.ctrl)
        self.snap_target = None
        value = self._number()
        axis = AXES[self.axis] if self.axis else None

        if self.transform == 'MOVE':
            if value is not None:
                delta = (axis or AXES['X']) * value
            else:
                a = view3d_utils.region_2d_to_location_3d(region, rv3d, self.start_mouse, self.pivot)
                b = view3d_utils.region_2d_to_location_3d(region, rv3d, xy, self.pivot)
                delta = b - a
                if self.snapping and self.snap_points:
                    lead = self.start_points[self.targets[0]]
                    best = None
                    for p in self.snap_points:
                        sp = view3d_utils.location_3d_to_region_2d(region, rv3d, p)
                        if sp is not None:
                            d = (sp - xy).length
                            if d <= SNAP_PX and (best is None or d < best[0]):
                                best = (d, p)
                    if best is not None:
                        self.snap_target = best[1]
                        delta = best[1] - lead
                if axis is not None:
                    delta = axis * delta.dot(axis)
            for i in self.targets:
                self.control[i] = self.start_points[i] + delta

        elif self.transform == 'SCALE':
            if value is not None:
                factor = value
            else:
                center = view3d_utils.location_3d_to_region_2d(region, rv3d, self.pivot) or self.start_mouse
                factor = (xy - center).length / max((self.start_mouse - center).length, 1.0)
                if self.snapping:
                    factor = round(factor * 10) / 10
            for i in self.targets:
                offset = self.start_points[i] - self.pivot
                if axis is not None:
                    along = axis * offset.dot(axis)
                    offset = offset - along + along * factor
                else:
                    offset = offset * factor
                self.control[i] = self.pivot + offset

        else:  # ROTATE: around the locked axis, or the view direction
            if axis is None:
                axis = -view3d_utils.region_2d_to_vector_3d(region, rv3d, self.start_mouse)
            if value is not None:
                angle = math.radians(value)
            else:
                center = view3d_utils.location_3d_to_region_2d(region, rv3d, self.pivot) or self.start_mouse
                a0 = math.atan2(*(self.start_mouse - center).yx)
                a1 = math.atan2(*(xy - center).yx)
                angle = a1 - a0
                # Seen from the viewer, the locked axis may point away: flip so the
                # points follow the mouse.
                view = -view3d_utils.region_2d_to_vector_3d(region, rv3d, center)
                if axis.dot(view) < 0:
                    angle = -angle
                if self.snapping:
                    step = math.radians(5)
                    angle = round(angle / step) * step
            rot = Matrix.Rotation(angle, 3, axis)
            for i in self.targets:
                self.control[i] = self.pivot + rot @ (self.start_points[i] - self.pivot)
        self._apply()

    def _end_transform(self, context, keep):
        if not keep:
            self.control = [c.copy() for c in self.start_points]
            self._apply()
            if self.undo_stack:
                self.undo_stack.pop()  # nothing changed: drop the undo step
        self.transform = None
        self.axis = None
        self.numeric = ""
        self.snap_target = None
        self._status(context)

    # -- modal ---------------------------------------------------------------

    def _move_keys(self):
        return {'SPACE', 'G'} if self.space_style else {'G'}

    def _axis_key(self, event):
        """Axis lock key, or None. In the Space style, Space during a move locks Y
        (like the add-on's own move); the plain Y key always works."""
        if event.type in AXES:
            return event.type
        if self.space_style and event.type == 'SPACE' and self.transform == 'MOVE':
            return 'Y'
        return None

    def _box_select(self, event):
        (x0, y0), (x1, y1) = self.box
        lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        inside = set()
        for i, c in enumerate(self.control):
            p = view3d_utils.location_3d_to_region_2d(self._region, self._rv3d, c)
            if p is not None and lo_x <= p.x <= hi_x and lo_y <= p.y <= hi_y:
                inside.add(i)
        if event.ctrl:
            self.selected -= inside
        elif event.shift:
            self.selected |= inside
        else:
            self.selected = inside

    def modal(self, context, event):
        context.area.tag_redraw()
        self._region, self._rv3d = context.region, context.region_data
        xy = (event.mouse_region_x, event.mouse_region_y)
        nav = {'MIDDLEMOUSE', 'WHEELINMOUSE', 'WHEELOUTMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}

        # Opening a pie (Select pie, shading, view) finishes and lets the pie open.
        if event.value == 'PRESS' and (event.type, bool(event.ctrl)) in PIE_KEYS and not self.transform:
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}

        if self.transform:
            axis_key = self._axis_key(event) if event.value in {'PRESS', 'DOUBLE_CLICK'} else None
            if event.type in {'MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT', 'LEFT_CTRL', 'RIGHT_CTRL'}:
                self._update_transform(context, event)
            elif axis_key is not None and not event.is_repeat:
                self.axis = None if self.axis == axis_key else axis_key
                self._update_transform(context, event)
            elif event.type in NUMERIC and event.value == 'PRESS':
                self.numeric += NUMERIC[event.type]
                self._update_transform(context, event)
            elif event.type == 'BACK_SPACE' and event.value == 'PRESS':
                self.numeric = self.numeric[:-1]
                self._update_transform(context, event)
            elif (event.type == 'LEFTMOUSE' or event.type in {'RET', 'NUMPAD_ENTER'}) and event.value == 'PRESS':
                self._end_transform(context, keep=True)
            elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
                self._end_transform(context, keep=False)
            elif event.type in nav or event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
                return {'PASS_THROUGH'}
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            if self.dragging is not None:
                point = self.control[self.dragging]
                target = view3d_utils.region_2d_to_location_3d(context.region, context.region_data, xy, point)
                if event.shift:  # finer
                    target = point.lerp(target, 0.1)
                self.control[self.dragging] = target
                self._apply()
            elif self.box is not None:
                self.box = (self.box[0], xy)
            else:
                self.hover = self._point_under(context, xy)
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            index = self._point_under(context, xy)
            if index is None:
                self.box = (xy, xy)  # drag a box to select points (a plain click deselects)
            else:
                if event.shift:
                    self.selected.symmetric_difference_update({index})
                elif index not in self.selected:
                    self.selected = {index}
                self._last_picked = index
                self._push_undo()
                self.dragging = index
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self.dragging is not None and self.undo_stack and \
                    all((a - b).length < 1e-9 for a, b in zip(self.undo_stack[-1][0], self.control)):
                self.undo_stack.pop()  # a click without moving: no undo step
            self.dragging = None
            if self.box is not None:
                self._box_select(event)
                self.box = None
        elif event.type == 'Z' and event.value == 'PRESS' and (event.ctrl or event.oskey):
            self._undo(redo=event.shift)
        elif event.type in self._move_keys() and event.value == 'PRESS' and not event.is_repeat:
            self._start_transform(context, 'MOVE', xy)
        elif event.type == 'R' and event.value == 'PRESS':
            self._start_transform(context, 'ROTATE', xy)
        elif event.type == 'S' and event.value == 'PRESS':
            self._start_transform(context, 'SCALE', xy)
        elif event.type == 'A' and event.value == 'PRESS':
            self.selected = set() if len(self.selected) == len(self.control) else set(range(len(self.control)))
        elif event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and not event.ctrl:
            return {'PASS_THROUGH'}  # plain wheel zooms the view as usual
        elif event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS', 'PAGE_UP'} and event.value == 'PRESS':
            self._resolution(+1)  # Ctrl wheel
        elif event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS', 'PAGE_DOWN'} and event.value == 'PRESS':
            self._resolution(-1)
        elif event.type == 'L' and event.value == 'PRESS':
            self._straighten()
        elif event.type == 'F' and event.value == 'PRESS':
            self._flatten()
        elif event.type == 'E' and event.value == 'PRESS' and self.mode == 'CURVE':
            self._push_undo()
            self.even = not self.even
            self._apply()
        elif event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._finish(context)
            return {'FINISHED'}
        elif event.type == 'ESC' and event.value == 'PRESS':
            return self._cancel_all(context)
        elif event.type in nav:
            return {'PASS_THROUGH'}  # navigate the view while shaping
        return {'RUNNING_MODAL'}


# ---------------------------------------------------------------------------
# Toolbar tool + menu
# ---------------------------------------------------------------------------

class GTOOLS_TL_curve_align(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'EDIT_MESH'
    bl_idname = "gtools.curve_align"
    bl_label = "Curve Align"
    bl_description = (
        "Select a row of vertices (curve) or a grid of quads (patch), then double click, Enter or Space.\n"
        "Edit the control points like vertices: G / Space, R, S, X / Y / Z, numbers, Ctrl to snap"
    )
    bl_icon = "ops.curve.draw"
    bl_keymap = (
        ("view3d.select", {"type": 'LEFTMOUSE', "value": 'CLICK'},
         {"properties": [("deselect_all", True)]}),
        ("view3d.select", {"type": 'LEFTMOUSE', "value": 'CLICK', "shift": True},
         {"properties": [("toggle", True)]}),
        ("view3d.select_box", {"type": 'LEFTMOUSE', "value": 'CLICK_DRAG'}, None),
        ("view3d.select_box", {"type": 'LEFTMOUSE', "value": 'CLICK_DRAG', "shift": True},
         {"properties": [("mode", 'ADD')]}),
        ("view3d.select_box", {"type": 'LEFTMOUSE', "value": 'CLICK_DRAG', "ctrl": True},
         {"properties": [("mode", 'SUB')]}),
        ("mesh.gtools_curve_align", {"type": 'LEFTMOUSE', "value": 'DOUBLE_CLICK'}, None),
        ("mesh.gtools_curve_align", {"type": 'RET', "value": 'PRESS'}, None),
        ("mesh.gtools_curve_align", {"type": 'SPACE', "value": 'PRESS'}, None),
    )


def _menu(self, context):
    self.layout.operator(GTOOLS_OT_curve_align.bl_idname, icon='CURVE_BEZCURVE')


def register():
    bpy.utils.register_class(GTOOLS_OT_curve_align)
    bpy.utils.register_tool(GTOOLS_TL_curve_align, after={"gtools.smart_cut_edit"}, separator=False, group=False)
    bpy.types.VIEW3D_MT_edit_mesh_vertices.append(_menu)


def unregister():
    bpy.types.VIEW3D_MT_edit_mesh_vertices.remove(_menu)
    bpy.utils.unregister_tool(GTOOLS_TL_curve_align)
    bpy.utils.unregister_class(GTOOLS_OT_curve_align)
