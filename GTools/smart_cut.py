"""Smart Cut: draw a line in the viewport and cut the object along it.

The cut plane goes through the drawn line and the view direction (like Blender's
Line Trim). The shaded side of the line is the side that gets removed; drag the
other way (or press F while dragging) to remove the other side.

- Edit Mode: destructive. Bisects the whole mesh, fills the hole and marks the
  new edges sharp.
- Object Mode: non-destructive. Creates a box cutter on the removed side and adds
  it as a Sharp Subtract modifier (same node group, Cutters collection, etc.),
  so you can move or rotate the cutter afterwards. Works on every selected mesh.
  With "Before Mirror" (tool header) the cut goes before the Mirror modifier,
  so it is mirrored too.

Ctrl click switches object while the tool is active (in Line mode a plain
click also works): Object Mode selects the object under the mouse (Shift
toggles it), Edit Mode moves Edit Mode to the object under the mouse.

Shapes (tool header): Line (drag) or Polyline (click points). A closed polyline
(click the first point again) removes the inside; an open one (Enter, Space,
right click or double click) removes the shaded side, like the line.
In Edit Mode the polyline cut is a Sharp Subtract applied right away.
While drawing a polyline, B rounds its corners: move the mouse left / right for
the radius and use the wheel for the segments (B again goes back to drawing).
Hold Space (line or polyline) to move what you are drawing.

Add (tool header): instead of cutting, the polyline becomes new geometry. In
Object Mode it is a new object with a Solidify modifier (depth editable later in
its Thickness); in Edit Mode the extruded shape is added to the mesh. With Align
to Face it grows out of the face under the mouse, otherwise from a plane facing
the view through the objects' center.

Align to Face (tool header): you draw on the face under the mouse and the cut
goes into that face. After drawing you set the depth (mouse left / right, click
to cut); holding Space then moves the cut, constrained to the face.

Available as the "Smart Cut" tool in the toolbar (T) in Object and Edit Mode.
"""

import math

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
from mathutils.geometry import intersect_line_line_2d, intersect_line_plane, tessellate_polygon

from .sharp_subtract import _prepare_cutter, add_sharp_subtract, get_node_group, hint_panel_origin

SNAP_STEP = math.radians(15)


# ---------------------------------------------------------------------------
# Cut plane
# ---------------------------------------------------------------------------

def cut_plane(region, rv3d, start, end, depth_point):
    """World-space plane through the screen line (start -> end) and the view direction.
    Returns (point, normal, line_direction). The normal points to the removed side."""
    p1 = view3d_utils.region_2d_to_location_3d(region, rv3d, start, depth_point)
    p2 = view3d_utils.region_2d_to_location_3d(region, rv3d, end, depth_point)
    mid = (Vector(start) + Vector(end)) / 2
    view = view3d_utils.region_2d_to_vector_3d(region, rv3d, mid)
    direction = (p2 - p1).normalized()
    normal = direction.cross(view).normalized()
    return (p1 + p2) / 2, normal, direction


def _bounds_world(objects):
    points = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return lo, hi


# ---------------------------------------------------------------------------
# Edit Mode: destructive
# ---------------------------------------------------------------------------

def cut_edit(context, point, normal):
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.bisect(plane_co=point, plane_no=normal, use_fill=True, clear_outer=True)
    # After the bisect, the new cut edges are the selection.
    bpy.ops.mesh.mark_sharp()


# ---------------------------------------------------------------------------
# Object Mode: non-destructive
# ---------------------------------------------------------------------------

def _make_box_cutter(context, point, normal, direction, objects):
    """Box with one face on the cut plane, covering the removed side of every object.
    Its origin sits on the cut face, so rotating the cutter pivots around the cut."""
    lo, hi = _bounds_world(objects)
    size = max((hi - lo).length, 0.001) * 2.0
    center = (lo + hi) / 2

    x_axis = (direction - normal * direction.dot(normal)).normalized()
    y_axis = normal.cross(x_axis).normalized()
    origin = center - normal * (center - point).dot(normal)  # object center projected on the plane

    rot = Matrix((x_axis, y_axis, normal)).transposed().to_4x4()
    matrix = Matrix.Translation(origin) @ rot

    h = size / 2
    verts = [(x, y, z) for x in (-h, h) for y in (-h, h) for z in (0.0, size)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh = bpy.data.meshes.new("SmartCut")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    # from_pydata winding is not guaranteed: make the normals point outwards.
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()

    cutter = bpy.data.objects.new("SmartCut", mesh)
    context.scene.collection.objects.link(cutter)
    cutter.matrix_world = matrix
    return cutter


def cut_object(context, objects, point, normal, direction, before_mirror=False):
    base = context.active_object if context.active_object in objects else objects[0]
    cutter = _make_box_cutter(context, point, normal, direction, objects)
    _prepare_cutter(context, base, cutter)
    node_group = get_node_group()
    for obj in objects:
        add_sharp_subtract(obj, cutter, node_group, before_mirror)
    return cutter


# ---------------------------------------------------------------------------
# Polyline: screen polygon -> prism cutter
# ---------------------------------------------------------------------------

def clean_points(points, min_dist=2.0):
    """Drop points that are (almost) on top of the previous one."""
    out = []
    for p in points:
        p = Vector(p)
        if not out or (p - out[-1]).length >= min_dist:
            out.append(p)
    return out


def region_bounds(region, margin=1.0):
    """Viewport rectangle grown by margin (fraction of its size)."""
    m = margin * max(region.width, region.height)
    return (-m, -m, region.width + m, region.height + m)


def cut_bounds(region, rv3d, objects, points):
    """Screen rectangle that contains the viewport, the drawn points and the
    objects (projected along the cut direction onto the depth plane), with margin."""
    xs = [0, region.width] + [p[0] for p in points]
    ys = [0, region.height] + [p[1] for p in points]
    center, direction, _ = cut_frame(region, rv3d, points, objects)
    view = view3d_utils.region_2d_to_vector_3d(region, rv3d, (region.width / 2, region.height / 2))
    behind = False
    for obj in objects:
        for corner in obj.bound_box:
            c = obj.matrix_world @ Vector(corner)
            # Where the cut direction through this corner meets the depth plane.
            denom = direction.dot(view)
            q = c + direction * ((center - c).dot(view) / denom) if abs(denom) > 1e-6 else c
            for target in (c, q):
                p = view3d_utils.location_3d_to_region_2d(region, rv3d, target)
                if p is None:
                    behind = True
                else:
                    xs.append(p.x)
                    ys.append(p.y)
    size = max(max(xs) - min(xs), max(ys) - min(ys))
    m = size * (1.0 if behind else 0.15) + 50
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def bounds_with(bounds, points, margin):
    """Grow the rectangle so it contains the points (with margin): the polyline
    has to be inside it for open_region."""
    if not points:
        return bounds
    xmin = min([bounds[0]] + [p[0] - margin for p in points])
    ymin = min([bounds[1]] + [p[1] - margin for p in points])
    xmax = max([bounds[2]] + [p[0] + margin for p in points])
    ymax = max([bounds[3]] + [p[1] + margin for p in points])
    return (xmin, ymin, xmax, ymax)


def _exit_point(p, d, bounds):
    """Where the ray p + t*d (p inside bounds) leaves the rectangle."""
    xmin, ymin, xmax, ymax = bounds
    ts = []
    if d.x > 1e-9:
        ts.append((xmax - p.x) / d.x)
    elif d.x < -1e-9:
        ts.append((xmin - p.x) / d.x)
    if d.y > 1e-9:
        ts.append((ymax - p.y) / d.y)
    elif d.y < -1e-9:
        ts.append((ymin - p.y) / d.y)
    return p + d * min(ts)


def _perimeter_pos(p, bounds):
    """Position along the rectangle border, counter-clockwise from the bottom-left corner."""
    xmin, ymin, xmax, ymax = bounds
    w, h = xmax - xmin, ymax - ymin
    dist = {
        'bottom': abs(p.y - ymin), 'right': abs(p.x - xmax),
        'top': abs(p.y - ymax), 'left': abs(p.x - xmin),
    }
    edge = min(dist, key=dist.get)
    if edge == 'bottom':
        return p.x - xmin
    if edge == 'right':
        return w + (p.y - ymin)
    if edge == 'top':
        return w + h + (xmax - p.x)
    return 2 * w + h + (ymax - p.y)


def _corners_ccw(s_from, s_to, bounds):
    """Rectangle corners met walking counter-clockwise from s_from to s_to."""
    xmin, ymin, xmax, ymax = bounds
    w, h = xmax - xmin, ymax - ymin
    perimeter = 2 * (w + h)
    corners = [(0.0, Vector((xmin, ymin))), (w, Vector((xmax, ymin))),
               (w + h, Vector((xmax, ymax))), (2 * w + h, Vector((xmin, ymax)))]
    span = (s_to - s_from) % perimeter
    found = []
    for s, c in corners:
        f = (s - s_from) % perimeter
        if 1e-6 < f < span - 1e-6:
            found.append((f, c))
    return [c for _, c in sorted(found, key=lambda x: x[0])]


def _crosses(a, b, segments):
    return any(intersect_line_line_2d(a, b, c, d) for c, d in segments)


def _extend(tip, neighbour, others, extra, bounds):
    """Extend the polyline end out to the border of the bounds. Straight on if
    possible; if that would cross the polyline itself, go to the border along
    the axis direction closest to it that does not cross."""
    straight = (tip - neighbour).normalized()
    candidates = [straight] + sorted(
        (Vector(v) for v in ((1, 0), (-1, 0), (0, 1), (0, -1))),
        key=lambda v: -v.dot(straight))
    for d in candidates:
        exit_point = _exit_point(tip, d, bounds)
        # Nudge the start a bit so touching the tip itself does not count.
        if not _crosses(tip + d * 0.01, exit_point, others + extra):
            return exit_point
    return _exit_point(tip, straight, bounds)


def open_region(points, flip, bounds, min_dist=2.0):
    """Screen polygon for an open polyline: the ends are extended to the border of
    the bounds rectangle, and the rectangle is split in two along the polyline.
    Returns the part on the removed side (left of the drawing direction, or right
    when flipped). Built this way it does not fold over itself, unlike closing the
    shape with far-away points (which left the cutter non-manifold)."""
    pts = clean_points(points, min_dist)
    if len(pts) < 2:
        return pts
    segments = list(zip(pts, pts[1:]))
    start = _extend(pts[0], pts[1], segments[1:], [], bounds)
    end = _extend(pts[-1], pts[-2], segments[:-1], [(pts[0], start)], bounds)
    path = [start] + pts + [end]
    if flip:
        path.reverse()
    s_end = _perimeter_pos(path[-1], bounds)
    s_start = _perimeter_pos(path[0], bounds)
    return clean_points(path + _corners_ccw(s_end, s_start, bounds), min_dist * 0.005)


def self_intersects(polygon):
    """True if any two non-adjacent edges of the closed polygon cross."""
    n = len(polygon)
    edges = [(Vector(polygon[i]), Vector(polygon[(i + 1) % n])) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # neighbours share a point
            if intersect_line_line_2d(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                return True
    return False


def cut_frame(region, rv3d, drawn, objects):
    """Depth point (center of the objects), extrusion direction and size for a cut.
    The direction is the view ray through the middle of what you drew, and every
    point is extruded along that same direction. In perspective this keeps the
    walls of the cut parallel instead of opening up with the field of view."""
    lo, hi = _bounds_world(objects)
    center = (lo + hi) / 2
    mid = sum((Vector(p) for p in drawn), Vector((0, 0))) / len(drawn)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, mid).normalized()
    return center, direction, max((hi - lo).length, 0.001)


def prism_from_world(context, near, far):
    """Closed prism between two matching rings of world points (one cap each)."""
    import bmesh
    origin = sum(near + far, Vector()) / (2 * len(near))
    bm = bmesh.new()
    vn = [bm.verts.new(v - origin) for v in near]
    vf = [bm.verts.new(v - origin) for v in far]
    bm.faces.new(vn)
    bm.faces.new(list(reversed(vf)))
    n = len(near)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((vn[i], vn[j], vf[j], vf[i]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new("SmartCut")
    bm.to_mesh(mesh)
    bm.free()
    cutter = bpy.data.objects.new("SmartCut", mesh)
    context.scene.collection.objects.link(cutter)
    cutter.location = origin
    return cutter


def make_prism_cutter(context, region, rv3d, polygon, objects, drawn):
    """Extrude the screen polygon so it goes all the way through the objects."""
    center, direction, size = cut_frame(region, rv3d, drawn, objects)
    length = size * 1.5
    base = [view3d_utils.region_2d_to_location_3d(region, rv3d, p, center) for p in polygon]
    return prism_from_world(context, [p - direction * length for p in base],
                            [p + direction * length for p in base])


def apply_cutter_object_mode(context, objects, cutter, before_mirror=False):
    base = context.active_object if context.active_object in objects else objects[0]
    _prepare_cutter(context, base, cutter)
    node_group = get_node_group()
    for obj in objects:
        add_sharp_subtract(obj, cutter, node_group, before_mirror)
    return cutter


def apply_cutter_edit_mode(context, objects, cutter):
    """Destructive: Sharp Subtract with a temporary cutter, applied at once."""
    active = context.active_object
    bpy.ops.object.mode_set(mode='OBJECT')
    node_group = get_node_group()
    try:
        for obj in objects:
            mod = add_sharp_subtract(obj, cutter, node_group)
            obj.modifiers.move(obj.modifiers.find(mod.name), 0)  # apply on the base mesh
            with context.temp_override(object=obj, active_object=obj):
                bpy.ops.object.modifier_apply(modifier=mod.name)
    finally:
        mesh = cutter.data
        bpy.data.objects.remove(cutter)
        bpy.data.meshes.remove(mesh)
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = active
        bpy.ops.object.mode_set(mode='EDIT')


def cut_object_polygon(context, region, rv3d, polygon, objects, drawn, before_mirror=False):
    cutter = make_prism_cutter(context, region, rv3d, polygon, objects, drawn)
    return apply_cutter_object_mode(context, objects, cutter, before_mirror)


def cut_edit_polygon(context, region, rv3d, polygon, drawn):
    active = context.active_object
    objects = [o for o in context.objects_in_mode if o.type == 'MESH'] or [active]
    cutter = make_prism_cutter(context, region, rv3d, polygon, objects, drawn)
    apply_cutter_edit_mode(context, objects, cutter)


# ---------------------------------------------------------------------------
# Align to face
# ---------------------------------------------------------------------------

def face_under_mouse(context, xy, targets):
    """(world point, normal, object) of the first target face under the mouse, or None.
    Other objects in the way (cutters, etc.) are skipped. targets=None accepts any
    solid mesh (not wire / bounds display, so cutters are skipped)."""
    region, rv3d = context.region, context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, xy)
    ray = view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)
    edit_objects = [o for o in context.objects_in_mode if o.type == 'MESH'] if context.mode == 'EDIT_MESH' else []
    if edit_objects:
        return _face_under_mouse_edit(origin, ray, targets or edit_objects)
    depsgraph = context.evaluated_depsgraph_get()
    names = {o.name for o in targets} if targets else None
    for _ in range(16):
        hit, location, normal, _index, obj, _matrix = context.scene.ray_cast(depsgraph, origin, ray)
        if not hit:
            return None
        original = obj.original
        if (names is None and original.type == 'MESH' and original.display_type not in {'WIRE', 'BOUNDS'}) \
                or (names is not None and (obj.name in names or original.name in names)):
            normal = normal.normalized()
            if normal.dot(ray) > 0:
                normal = -normal
            return location, normal, original
        origin = location + ray * 1e-4
    return None


def _face_under_mouse_edit(origin, ray, targets):
    """Scene ray casts skip meshes in Edit Mode: cast against their edit meshes."""
    import bmesh
    from mathutils.bvhtree import BVHTree
    best = None
    for obj in targets:
        if obj.type != 'MESH' or obj.mode != 'EDIT':
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        tree = BVHTree.FromBMesh(bm)
        inv = obj.matrix_world.inverted()
        local_origin = inv @ origin
        local_dir = (inv.to_3x3() @ ray).normalized()
        location, normal, _index, _dist = tree.ray_cast(local_origin, local_dir)
        if location is None:
            continue
        world = obj.matrix_world @ location
        dist = (world - origin).length
        if best is None or dist < best[0]:
            n = (obj.matrix_world.inverted().transposed().to_3x3() @ normal).normalized()
            best = (dist, world, n, obj)
    if best is None:
        return None
    normal = best[2] if best[2].dot(ray) < 0 else -best[2]
    return best[1], normal, best[3]


class ScreenFrame:
    """Drawing in screen pixels (normal Smart Cut)."""
    aligned = False
    px = 1.0

    def __init__(self, region):
        self.region = region
        self.bounds = region_bounds(region)

    def from_mouse(self, region, rv3d, xy):
        return Vector(xy)

    def to_screen(self, region, rv3d, p):
        return Vector(p)


class FaceFrame:
    """Drawing on the plane of a face: 2D coordinates (u, v) on that plane.
    Moving points in this frame keeps them on the face."""
    aligned = True

    def __init__(self, region, rv3d, origin, normal, objects):
        self.origin = origin
        self.normal = normal
        center = Vector((region.width / 2, region.height / 2))
        a = view3d_utils.region_2d_to_location_3d(region, rv3d, center, origin)
        b = view3d_utils.region_2d_to_location_3d(region, rv3d, center + Vector((10, 0)), origin)
        right = b - a
        u = right - normal * right.dot(normal)
        if u.length < 1e-6:
            u = normal.orthogonal()
        self.u = u.normalized()
        self.v = normal.cross(self.u).normalized()
        # Size of one screen pixel on the face, around the hit point.
        s = view3d_utils.location_3d_to_region_2d(region, rv3d, origin) or center
        p0 = self.from_mouse(region, rv3d, s)
        p1 = self.from_mouse(region, rv3d, s + Vector((1, 0)))
        self.px = (p1 - p0).length if p0 is not None and p1 is not None else 0.01
        # Rectangle on the face plane covering the objects.
        corners = [self.to_frame(o.matrix_world @ Vector(c)) for o in objects for c in o.bound_box]
        xs = [c.x for c in corners] + [0.0]
        ys = [c.y for c in corners] + [0.0]
        m = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.25 + self.px * 50
        self.bounds = (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
        # How deep the objects go below the face.
        self.thickness = max(max((origin - o.matrix_world @ Vector(c)).dot(normal)
                                 for o in objects for c in o.bound_box), self.px)

    def to_frame(self, world):
        d = world - self.origin
        return Vector((d.dot(self.u), d.dot(self.v)))

    def to_world(self, p):
        return self.origin + self.u * p[0] + self.v * p[1]

    def from_mouse(self, region, rv3d, xy):
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, xy)
        ray = view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)
        hit = intersect_line_plane(origin, origin + ray, self.origin, self.normal)
        return self.to_frame(hit) if hit is not None else None

    def to_screen(self, region, rv3d, p, depth=0.0):
        return view3d_utils.location_3d_to_region_2d(region, rv3d, self.to_world(p) - self.normal * depth)


def face_prism(context, frame, polygon, depth, size):
    """Cutter for an aligned cut: from just above the face down to the depth."""
    lift = max(size * 1e-3, frame.px)
    near = [frame.to_world(p) + frame.normal * lift for p in polygon]
    far = [frame.to_world(p) - frame.normal * depth for p in polygon]
    return prism_from_world(context, near, far)


# ---------------------------------------------------------------------------
# Additive: new geometry instead of a cut
# ---------------------------------------------------------------------------

def _ccw(polygon):
    """Counter-clockwise in the frame, so the face normal matches the frame normal."""
    area = sum(a.x * b.y - b.x * a.y for a, b in zip(polygon, polygon[1:] + polygon[:1]))
    return list(polygon) if area >= 0 else list(reversed(polygon))


def add_object(context, frame, polygon, depth, material_from=None):
    """New object: the drawn shape as a face with a Solidify modifier, so the depth
    stays editable afterwards (modifier panel, Thickness)."""
    polygon = _ccw([Vector(p) for p in polygon])
    world = [frame.to_world(p) for p in polygon]
    origin = sum(world, Vector()) / len(world)
    mesh = bpy.data.meshes.new("GT_Add")
    mesh.from_pydata([w - origin for w in world], [], [list(range(len(world)))])
    mesh.update()
    obj = bpy.data.objects.new("GT_Add", mesh)
    context.collection.objects.link(obj)
    obj.location = origin
    if material_from is not None and material_from.type == 'MESH' and material_from.data.materials:
        mesh.materials.append(material_from.data.materials[0])
    solid = obj.modifiers.new("Depth", 'SOLIDIFY')
    solid.thickness = depth
    solid.offset = 1.0  # grow along the face normal, out of the surface
    solid.use_even_offset = True
    for o in context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    return obj


def add_edit_geometry(obj, frame, polygon, depth):
    """Edit Mode: add the extruded shape into the mesh being edited (selected)."""
    import bmesh
    polygon = _ccw([Vector(p) for p in polygon])
    inv = obj.matrix_world.inverted()
    bm = bmesh.from_edit_mesh(obj.data)
    for elem in list(bm.verts) + list(bm.edges) + list(bm.faces):
        elem.select = False
    bottom = [bm.verts.new(inv @ frame.to_world(p)) for p in polygon]
    top = [bm.verts.new(inv @ (frame.to_world(p) + frame.normal * depth)) for p in polygon]
    faces = [bm.faces.new(list(reversed(bottom))), bm.faces.new(top)]
    n = len(polygon)
    for i in range(n):
        j = (i + 1) % n
        faces.append(bm.faces.new((bottom[i], bottom[j], top[j], top[i])))
    bmesh.ops.recalc_face_normals(bm, faces=faces)
    for f in faces:
        f.select = True
    bm.select_flush(True)
    bmesh.update_edit_mesh(obj.data)


# ---------------------------------------------------------------------------
# Bevel (rounded corners on the drawn polyline)
# ---------------------------------------------------------------------------

def _round_corner(prev, p, nxt, radius, segments, max_t):
    """Points replacing corner p by an arc of the given radius (fillet), or [p]."""
    u = prev - p
    v = nxt - p
    if u.length < 1e-6 or v.length < 1e-6:
        return [p]
    u.normalize()
    v.normalize()
    cos_a = max(-1.0, min(1.0, u.dot(v)))
    angle = math.acos(cos_a)  # inner angle at the corner
    if angle < 1e-3 or angle > math.pi - 1e-3:
        return [p]  # folded back or straight: nothing to round
    t = radius / math.tan(angle / 2)
    if t > max_t:  # keep neighbouring roundings from overlapping
        t = max_t
        radius = t * math.tan(angle / 2)
    t1 = p + u * t
    t2 = p + v * t
    if segments <= 1:
        return [t1, t2]  # chamfer
    center = p + (u + v).normalized() * (radius / math.sin(angle / 2))
    a1 = math.atan2((t1 - center).y, (t1 - center).x)
    a2 = math.atan2((t2 - center).y, (t2 - center).x)
    delta = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi  # short way round
    return [center + Vector((math.cos(a1 + delta * i / segments), math.sin(a1 + delta * i / segments))) * radius
            for i in range(segments + 1)]


def bevel_polyline(points, radius, segments, closed, min_dist=2.0):
    """Round the corners of the drawn polyline (screen space). Open polylines keep
    their two end points; closed ones round every corner. min_dist is in the same
    units as the points (2 for pixels, smaller on a face)."""
    pts = clean_points(points, min_dist)
    n = len(pts)
    if radius <= 0 or n < 3:
        return pts
    corners = range(n) if closed else range(1, n - 1)
    out = []
    for i in range(n):
        if i not in corners:
            out.append(pts[i])
            continue
        prev, nxt = pts[i - 1], pts[(i + 1) % n]
        max_t = min((prev - pts[i]).length, (nxt - pts[i]).length) * 0.5
        out += _round_corner(prev, pts[i], nxt, radius, segments, max_t)
    out = clean_points(out, min_dist * 0.005)
    if closed and len(out) > 1 and (out[0] - out[-1]).length < min_dist * 0.005:
        out.pop()  # two roundings meeting where the shape closes
    return out


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _fill(polygon, color):
    polygon = [p for p in polygon if p is not None]
    if len(polygon) < 3:
        return
    tris = tessellate_polygon([[Vector((p[0], p[1], 0)) for p in polygon]])
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": [tuple(p) for p in polygon]}, indices=tris)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _lines(context, points, color, width, closed=False):
    points = [p for p in points if p is not None]
    if len(points) < 2:
        return
    seg = []
    for a, b in zip(points, points[1:] + (points[:1] if closed else [])):
        seg += [tuple(a), tuple(b)]
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.uniform_float("viewportSize", (context.region.width, context.region.height))
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'LINES', {"pos": seg}).draw(shader)


def _segments(context, pairs, color, width):
    seg = []
    for a, b in pairs:
        if a is not None and b is not None:
            seg += [tuple(a), tuple(b)]
    if not seg:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.uniform_float("viewportSize", (context.region.width, context.region.height))
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'LINES', {"pos": seg}).draw(shader)


def _dots(points, color, size):
    points = [p for p in points if p is not None]
    if not points:
        return
    shader = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    gpu.state.point_size_set(size)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'POINTS', {"pos": [tuple(p) for p in points]}).draw(shader)


def _text(pos, text, highlight):
    import blf
    font = 0
    blf.size(font, 13)
    if highlight:
        blf.color(font, 1.0, 0.8, 0.2, 1.0)
    else:
        blf.color(font, 1.0, 1.0, 1.0, 0.8)
    blf.position(font, pos[0] + 18, pos[1] - 22, 0)
    blf.draw(font, text)


REMOVE_COLOR = (1.0, 0.25, 0.2, 0.15)
DEPTH_COLOR = (1.0, 0.6, 0.2, 0.9)


def _draw_callback(op, context):
    region, rv3d = context.region, context.region_data
    frame = op.frame

    def scr(points, depth=0.0):
        if frame.aligned:
            return [frame.to_screen(region, rv3d, p, depth) for p in points]
        return [Vector(p) for p in points]

    gpu.state.blend_set('ALPHA')
    if op.stage == 'DEPTH':
        polygon, shape, closed = op.final_shape()
        _fill(scr(polygon), (0.3, 0.7, 1.0, 0.15) if op.add else REMOVE_COLOR)
        _lines(context, scr(shape), (1, 1, 1, 0.95), 2.0, closed=closed)
        depth = -op.depth if op.add else op.depth  # adding grows out of the face, cutting goes in
        if op.add:
            _fill(scr(polygon, depth), (0.3, 0.7, 1.0, 0.15))
        _lines(context, scr(shape, depth), DEPTH_COLOR, 2.0, closed=closed)
        _segments(context, zip(scr(shape), scr(shape, depth)), (1.0, 0.6, 0.2, 0.5), 1.0)
        if op.adjusting_bevel:
            _text(op.hud_pos, f"Bevel  radius {op.bevel_radius:.0f}px   segments {op.bevel_segments}", True)
        else:
            _text(op.hud_pos, f"Depth {op.depth:.3f}", True)
    elif op.draw_shape == 'LINE':
        if op.end is not None and (Vector(op.end) - Vector(op.start)).length > 1e-6:
            pts = [Vector(op.start), Vector(op.end)]
            _fill(scr(open_region(pts, op.flip, op._bounds(pts), op._min_dist())), REMOVE_COLOR)
            _lines(context, scr(pts), (1, 1, 1, 0.95), 2.0)
    else:
        polygon, shape, closed, raw = op.preview_shape()
        _fill(scr(polygon), (0.3, 0.7, 1.0, 0.15) if op.add else REMOVE_COLOR)
        _lines(context, scr(shape), (1, 1, 1, 0.95), 2.0, closed=closed)
        if op.bevel_radius > 0:
            _lines(context, scr(raw), (1, 1, 1, 0.25), 1.0, closed=closed)  # the sharp outline
        pts = scr(op.points)
        _dots(pts, (1, 1, 1, 1), 6.0)
        if len(pts) >= 3:
            _dots([pts[0]], (1.0, 0.8, 0.2, 1) if op.closing else (1, 1, 1, 1), 10.0)
        if op.adjusting_bevel or op.bevel_radius > 0:
            _text(op.hud_pos, f"Bevel  radius {op.bevel_radius:.0f}px   segments {op.bevel_segments}",
                  op.adjusting_bevel)
    if op.moving:
        _text((op.hud_pos[0], op.hud_pos[1] - 18), "Moving" + (" on the face" if frame.aligned else ""), True)
    _draw_hints(context, op)
    gpu.state.blend_set('NONE')


def _hint_lines(op):
    """Title and (key, action) pairs for what the tool is doing right now."""
    mode = "Add" if op.add else "Cut"
    shape = "Polyline" if op.draw_shape == 'POLYLINE' else "Line"
    title = f"Smart Cut  ·  {mode}  ·  {shape}" + ("  ·  Align to Face" if op.frame.aligned and op.align_to_face else "")
    if op.adjusting_bevel:
        return title + "  ·  Bevel", [
            ("Mouse left / right", "radius"),
            ("Wheel", "segments"),
            ("B / Click", "back to depth" if op.stage == 'DEPTH' else "back to drawing"),
            ("Enter", "add" if op.add and op.stage == 'DEPTH' else ("cut" if op.stage == 'DEPTH' else "finish")),
            ("Esc", "cancel"),
        ]
    if op.stage == 'DEPTH':
        lines = [("Mouse left / right", "depth"), ("Space (hold)", "move on the face")]
        if op.draw_shape == 'POLYLINE':
            lines += [("B", "bevel corners"), ("Ctrl Z", "back to drawing")]
        if not op.add and not op.final_closed:
            lines.append(("F", "flip side"))
        lines += [("Click / Enter", "add" if op.add else "cut"), ("Esc / Right click", "cancel")]
        return title + "  ·  Depth", lines
    if op.draw_shape == 'LINE':
        return title, [
            ("Drag", "draw the line"), ("Space (hold)", "move"), ("F", "flip side"),
            ("Shift", "snap angle"), ("Esc / Right click", "cancel"),
        ]
    lines = [("Click", "add point")]
    if op.add:
        lines.append(("First point / Enter / Right click", "close the shape"))
    else:
        lines += [("Click the first point", "cut inside"), ("Enter / Right click / Double click", "cut shaded side")]
    lines += [("Space (hold)", "move"), ("B", "bevel corners"), ("Ctrl Z / Backspace", "undo point")]
    if not op.add:
        lines.append(("F", "flip side"))
    lines += [("Shift", "snap angle"), ("Esc", "cancel")]
    return title, lines


def _draw_hints(context, op):
    """Shortcut list in the bottom-left corner of the viewport."""
    import blf
    title, lines = _hint_lines(op)
    font = 0
    size = 12
    blf.size(font, size)
    line_h = size * 1.55
    key_w = max(blf.dimensions(font, k)[0] for k, _ in lines) + 14
    width = max(blf.dimensions(font, title)[0],
                key_w + max(blf.dimensions(font, a)[0] for _, a in lines)) + 20
    height = line_h * (len(lines) + 1) + 14

    x, y = hint_panel_origin(context, width)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    quad = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": quad}, indices=((0, 1, 2), (0, 2, 3)))
    shader.uniform_float("color", (0.0, 0.0, 0.0, 0.35))
    batch.draw(shader)

    ty = y + height - line_h
    blf.color(font, 1.0, 0.8, 0.2, 1.0)
    blf.position(font, x + 10, ty, 0)
    blf.draw(font, title)
    for key, action in lines:
        ty -= line_h
        blf.color(font, 1.0, 1.0, 1.0, 0.95)
        blf.position(font, x + 10, ty, 0)
        blf.draw(font, key)
        blf.color(font, 1.0, 1.0, 1.0, 0.6)
        blf.position(font, x + 10 + key_w, ty, 0)
        blf.draw(font, action)


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

CLOSE_DISTANCE = 12  # px to the first point to close the polygon

STATUS = {
    'LINE': ("Smart Cut: drag a line  |  Hold Space: move  |  F: flip side  |  Shift: snap angle  |  "
             "Esc / Right click: cancel"),
    'POLYLINE': ("Smart Cut (polyline): click to add points  |  Click the first point: cut inside  |  "
                 "Enter / Right click / double click: cut the shaded side  |  Hold Space: move  |  "
                 "Ctrl Z / Backspace: undo point  |  B: bevel corners  |  F: flip side  |  Shift: snap angle  |  Esc: cancel"),
    'DEPTH': ("Smart Cut depth: move left / right  |  Hold Space: move on the face  |  F: flip side  |  "
              "Ctrl Z: back to drawing  |  Click / Enter: cut  |  Esc / Right click: cancel"),
    'ADD': ("Smart Cut (add): click to add points  |  Click the first point / Enter / Right click / double click: "
            "close the shape  |  Hold Space: move  |  Ctrl Z / Backspace: undo point  |  B: bevel corners  |  "
            "Shift: snap angle  |  Esc: cancel"),
    'DEPTH_ADD': ("Smart Cut depth: move left / right  |  Hold Space: move on the face  |  "
                  "Ctrl Z: back to drawing  |  Click / Enter: add  |  Esc / Right click: cancel"),
}


class GTOOLS_OT_smart_cut(bpy.types.Operator):
    """Draw a line or polyline to cut the object (the shaded side is removed). Ctrl click switches object"""
    bl_idname = "view3d.gtools_smart_cut"
    bl_label = "Smart Cut"
    bl_options = {'REGISTER', 'UNDO'}

    shape: bpy.props.EnumProperty(
        name="Shape",
        items=(
            ('LINE', "Line", "Drag a straight line"),
            ('POLYLINE', "Polyline", "Click points. Close the shape on the first point to cut the inside"),
        ),
        default='LINE',
    )
    operation: bpy.props.EnumProperty(
        name="Operation",
        items=(
            ('CUT', "Cut", "Remove the drawn shape (boolean)"),
            ('ADD', "Add", "Add the drawn polyline as new geometry with an editable depth"),
        ),
        default='CUT',
    )
    align_to_face: bpy.props.BoolProperty(
        name="Align to Face",
        description="Draw on the face under the mouse and cut into it, with an adjustable depth",
        default=False,
    )
    before_mirror: bpy.props.BoolProperty(
        name="Before Mirror",
        description="Object Mode: put the new cut before the Mirror modifier so the cut is mirrored too",
        default=True,
    )
    select_only: bpy.props.BoolProperty(
        name="Switch Object", description="Only switch object on click", default=False, options={'SKIP_SAVE'})
    toggle: bpy.props.BoolProperty(
        name="Toggle Selection",
        description="On a click, add/remove the object from the selection instead of replacing it",
        default=False, options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.area is not None and context.area.type == 'VIEW_3D'
                and obj is not None and obj.type == 'MESH')

    # -- setup ---------------------------------------------------------------

    def _targets(self, context):
        obj = context.active_object
        if obj.mode == 'EDIT':
            return [o for o in context.objects_in_mode if o.type == 'MESH'] or [obj]
        return [o for o in context.selected_objects if o.type == 'MESH'] or [obj]

    def invoke(self, context, event):
        mouse = (event.mouse_region_x, event.mouse_region_y)
        self.click_pos = mouse
        if self.select_only:
            return self._click(context)

        region, rv3d = context.region, context.region_data
        self.targets = self._targets(context)
        self.add = self.operation == 'ADD'
        self.draw_shape = 'POLYLINE' if self.add else self.shape
        self.hit_object = None
        self.frame = ScreenFrame(region)
        hit = None
        if self.align_to_face:
            # Adding can start on any object; cutting only on the objects being cut.
            hit = face_under_mouse(context, mouse, None if self.add else self.targets)
            if hit is None:
                self.report({'INFO'}, "Smart Cut: no face under the mouse, using the view")
        if hit is not None:
            self.frame = FaceFrame(region, rv3d, hit[0], hit[1], self.targets)
            self.hit_object = hit[2]
        elif self.add:
            # Adding without a face: draw on a plane facing the view, through the objects' center.
            lo, hi = _bounds_world(self.targets)
            view = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse)
            self.frame = FaceFrame(region, rv3d, (lo + hi) / 2, -view.normalized(), self.targets)

        start = self.frame.from_mouse(region, rv3d, mouse)
        self.start = start
        self.end = None
        self.flip = False
        self.points = [start]
        self.mouse = start
        self.closing = False
        self.first_release = True
        self.bevel_radius = 0.0
        self.bevel_segments = 4
        self.adjusting_bevel = False
        self.moving = False
        self.stage = 'DRAW'
        self.depth = 0.0
        self.hud_pos = mouse
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_callback, (self, context), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        self._update_status(context)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
        context.workspace.status_text_set(None)
        context.area.tag_redraw()

    def _update_status(self, context):
        if self.adjusting_bevel:
            text = (f"Smart Cut bevel: move left / right: radius ({self.bevel_radius:.0f}px)  |  "
                    f"Wheel: segments ({self.bevel_segments})  |  B / click: back to drawing  |  Enter: cut")
        elif self.stage == 'DEPTH':
            text = STATUS['DEPTH_ADD' if self.add else 'DEPTH']
        else:
            text = STATUS['ADD' if self.add else self.draw_shape]
        context.workspace.status_text_set(text)

    # -- helpers -------------------------------------------------------------

    def _frame_pos(self, context, event, anchor=None):
        """Mouse in the drawing frame (pixels, or face coordinates), with angle snap."""
        pos = self.frame.from_mouse(context.region, context.region_data,
                                    (event.mouse_region_x, event.mouse_region_y))
        if pos is None:
            return None
        if event.shift and anchor is not None:
            delta = pos - Vector(anchor)
            angle = round(math.atan2(delta.y, delta.x) / SNAP_STEP) * SNAP_STEP
            pos = Vector(anchor) + Vector((math.cos(angle), math.sin(angle))) * delta.length
        return pos

    def _screen_dist(self, context, a, b):
        """Distance in pixels between two points of the frame."""
        r, rv = context.region, context.region_data
        sa, sb = self.frame.to_screen(r, rv, a), self.frame.to_screen(r, rv, b)
        if sa is None or sb is None:
            return float('inf')
        return (Vector(sa) - Vector(sb)).length

    def _min_dist(self):
        return self.frame.px * 2  # 2 pixels, in drawing-frame units

    def _bevel(self, points, closed):
        return bevel_polyline(points, self.bevel_radius * self.frame.px, self.bevel_segments, closed,
                              self._min_dist())

    def _bounds(self, points):
        return bounds_with(self.frame.bounds, points, self.frame.px * 50)

    def preview_shape(self):
        """(fill polygon, shape line, closed, raw points) while drawing the polyline."""
        pts = [Vector(p) for p in self.points]
        if self.closing:
            shape = self._bevel(pts, True)
            return shape, shape, True, pts
        raw = clean_points(pts + [Vector(self.mouse)], 1e-6)
        if self.add:  # adding always makes a closed shape
            shape = self._bevel(raw, True) if len(raw) >= 3 else raw
            return shape, shape, True, raw
        shape = self._bevel(raw, False)
        polygon = (open_region(shape, self.flip, self._bounds(shape), self._min_dist())
                   if len(shape) >= 2 else [])
        return polygon, shape, False, raw

    def final_shape(self):
        """(cut polygon, shape line, closed) for the finished drawing (depth stage)."""
        shape = self._bevel(self.final_points, self.final_closed)
        if self.final_closed:
            return shape, shape, True
        return open_region(shape, self.flip, self._bounds(shape), self._min_dist()), shape, False

    def _move(self, context, event):
        """Hold Space: move everything drawn so far (on the face when aligned)."""
        pos = self.frame.from_mouse(context.region, context.region_data,
                                    (event.mouse_region_x, event.mouse_region_y))
        if pos is None or self._move_anchor is None:
            return
        delta = pos - self._move_anchor
        self._move_anchor = pos
        if self.stage == 'DEPTH':
            self.final_points = [Vector(p) + delta for p in self.final_points]
        elif self.draw_shape == 'LINE':
            self.start = Vector(self.start) + delta
            if self.end is not None:
                self.end = Vector(self.end) + delta
        else:
            self.points = [Vector(p) + delta for p in self.points]
            self.mouse = Vector(self.mouse) + delta

    # -- modal ---------------------------------------------------------------

    def modal(self, context, event):
        context.area.tag_redraw()
        if event.type == 'MOUSEMOVE':
            self.hud_pos = (event.mouse_region_x, event.mouse_region_y)
        if event.type == 'ESC':
            self._finish(context)
            return {'CANCELLED'}
        if event.type == 'F' and event.value == 'PRESS':
            self.flip = not self.flip
            return {'RUNNING_MODAL'}
        if event.type == 'SPACE':
            if event.value == 'PRESS' and not event.is_repeat:
                self.moving = True
                self._move_anchor = self.frame.from_mouse(
                    context.region, context.region_data, (event.mouse_region_x, event.mouse_region_y))
            elif event.value == 'RELEASE':
                self.moving = False
                if self.stage == 'DEPTH':  # keep the depth where it was
                    self._depth_x0 = event.mouse_region_x
                    self._depth_d0 = self.depth
            return {'RUNNING_MODAL'}
        if self.moving and event.type == 'MOUSEMOVE':
            self._move(context, event)
            return {'RUNNING_MODAL'}
        if event.type == 'B' and event.value == 'PRESS' and self.draw_shape == 'POLYLINE':
            self._toggle_bevel(context, event)
            return {'RUNNING_MODAL'}
        if self.adjusting_bevel:
            return self._modal_bevel(context, event)
        if self.stage == 'DEPTH':
            return self._modal_depth(context, event)
        if self.draw_shape == 'LINE':
            return self._modal_line(context, event)
        return self._modal_polyline(context, event)

    def _modal_line(self, context, event):
        if event.type in {'MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            pos = self._frame_pos(context, event, self.start)
            if pos is not None:
                self.end = pos
        elif event.type == 'RIGHTMOUSE':
            self._finish(context)
            return {'CANCELLED'}
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self.end is None or self._screen_dist(context, self.start, self.end) < 5:
                self._finish(context)
                return self._click(context)
            if self.frame.aligned:
                return self._enter_depth(context, event, [self.start, self.end], closed=False)
            self._finish(context)
            return self._cut_line(context)
        return {'RUNNING_MODAL'}

    def _toggle_bevel(self, context, event):
        """B: start / stop adjusting the bevel, while drawing or during the depth."""
        self.adjusting_bevel = not self.adjusting_bevel
        if self.adjusting_bevel:
            self._bevel_x0 = event.mouse_region_x
            self._bevel_r0 = self.bevel_radius
        else:
            self._resume(event)
        self._update_status(context)

    def _resume(self, event):
        """Back from the bevel: the depth carries on from where it was."""
        if self.stage == 'DEPTH':
            self._depth_x0 = event.mouse_region_x
            self._depth_d0 = self.depth

    def _modal_polyline(self, context, event):
        if event.type in {'MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            pos = self._frame_pos(context, event, self.points[-1])
            if pos is not None:
                self.mouse = pos
            self.closing = (len(self.points) >= 3 and
                            self._screen_dist(context, self.mouse, self.points[0]) < CLOSE_DISTANCE)
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self.first_release:
            # Press-drag-release on the first point also gives a first segment.
            self.first_release = False
            if self._screen_dist(context, self.mouse, self.points[0]) >= 5:
                self.points.append(self.mouse)
        elif event.type == 'LEFTMOUSE' and event.value == 'DOUBLE_CLICK':
            return self._finish_polyline(context, event, closed=False)
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if self.closing:
                return self._finish_polyline(context, event, closed=True)
            if self._screen_dist(context, self.mouse, self.points[-1]) >= 3:
                self.points.append(self.mouse)
        elif event.type in {'RET', 'NUMPAD_ENTER', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish_polyline(context, event, closed=False)
        elif self._is_undo(event):
            if len(self.points) > 1:
                self.points.pop()
                self.closing = False
            elif event.type == 'Z':  # Ctrl Z on the very first point: leave the tool
                self._finish(context)
                return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    @staticmethod
    def _is_undo(event):
        """Backspace or Ctrl Z (Cmd Z on macOS): remove the last point."""
        if event.value != 'PRESS':
            return False
        return event.type == 'BACK_SPACE' or (event.type == 'Z' and (event.ctrl or event.oskey)
                                              and not event.shift)

    def _modal_bevel(self, context, event):
        """B pressed: mouse left / right sets the radius, the wheel sets the segments.
        B again (or a click) goes back to drawing / depth; Enter finishes."""
        if event.type == 'MOUSEMOVE':
            self.bevel_radius = max(0.0, self._bevel_r0 + (event.mouse_region_x - self._bevel_x0))
        elif event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS', 'PAGE_UP'} and event.value == 'PRESS':
            self.bevel_segments = min(self.bevel_segments + 1, 64)
        elif event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS', 'PAGE_DOWN'} and event.value == 'PRESS':
            self.bevel_segments = max(self.bevel_segments - 1, 1)
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self.adjusting_bevel = False
            self._resume(event)
        elif event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self.adjusting_bevel = False
            if self.stage == 'DEPTH':
                self._finish(context)
                return self._add(context) if self.add else self._cut_aligned(context)
            return self._finish_polyline(context, event, closed=self.closing)
        elif event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            self.adjusting_bevel = False
            if self.stage == 'DEPTH':
                self._resume(event)
            else:
                return self._finish_polyline(context, event, closed=self.closing)
        self._update_status(context)
        return {'RUNNING_MODAL'}

    def _finish_polyline(self, context, event, closed):
        points = [Vector(p) for p in self.points]
        if not closed and self._screen_dist(context, self.mouse, points[-1]) >= 3:
            points.append(Vector(self.mouse))  # the point under the mouse counts
        points = clean_points(points, 1e-6)
        if self.add:
            closed = True  # an open polyline is closed from its last point to the first
        if (closed and len(points) < 3) or len(points) < 2:
            self._finish(context)
            return {'CANCELLED'}
        if self.frame.aligned:
            return self._enter_depth(context, event, points, closed)

        self._finish(context)
        points = self._bevel(points, closed)
        if closed:
            polygon = points
        else:
            bounds = cut_bounds(context.region, context.region_data, self.targets, points)
            polygon = open_region(points, self.flip, bounds)
        if len(polygon) < 3:
            return {'CANCELLED'}
        if self_intersects(polygon):
            self.report({'WARNING'}, "Smart Cut: the shape crosses itself, draw it without crossings")
            return {'CANCELLED'}
        return self._cut_polygon(context, polygon, points)

    # -- depth stage (align to face) -----------------------------------------

    def _enter_depth(self, context, event, points, closed):
        self.final_points = [Vector(p) for p in points]
        self.final_closed = closed
        polygon, _shape, _closed = self.final_shape()
        if len(polygon) < 3 or self_intersects(polygon):
            self._finish(context)
            self.report({'WARNING'}, "Smart Cut: the shape crosses itself, draw it without crossings")
            return {'CANCELLED'}
        self.stage = 'DEPTH'
        self.adjusting_bevel = False
        self.depth = self.frame.thickness * (0.25 if self.add else 0.5)
        self._depth_x0 = event.mouse_region_x
        self._depth_d0 = self.depth
        self._update_status(context)
        return {'RUNNING_MODAL'}

    def _modal_depth(self, context, event):
        if self._is_undo(event) and self.draw_shape == 'POLYLINE':
            # Back to drawing, with the points as they were.
            self.stage = 'DRAW'
            self.points = [Vector(p) for p in self.final_points]
            self.mouse = Vector(self.points[-1])
            self.closing = False
            self._update_status(context)
            return {'RUNNING_MODAL'}
        if event.type == 'MOUSEMOVE':
            self.depth = max(self.frame.px, self._depth_d0 + (event.mouse_region_x - self._depth_x0) * self.frame.px)
        elif event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}
        elif (event.type == 'LEFTMOUSE' and event.value == 'PRESS') or \
                (event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS'):
            self._finish(context)
            return self._add(context) if self.add else self._cut_aligned(context)
        return {'RUNNING_MODAL'}

    # -- actions -------------------------------------------------------------

    def _click(self, context):
        """A click without dragging switches object."""
        obj = context.active_object
        try:
            if obj is not None and obj.mode == 'EDIT':
                # Same as Alt Q: move Edit Mode to the object under the mouse.
                bpy.ops.object.transfer_mode('INVOKE_DEFAULT')
            else:
                bpy.ops.view3d.select(
                    toggle=self.toggle, deselect_all=not self.toggle,
                    location=(int(self.click_pos[0]), int(self.click_pos[1])))
        except RuntimeError as ex:
            print(f"G-Tools Smart Cut: could not switch object ({ex})")
            return {'CANCELLED'}
        return {'FINISHED'}

    def _cut_line(self, context):
        region, rv3d = context.region, context.region_data
        obj = context.active_object
        start, end = (self.end, self.start) if self.flip else (self.start, self.end)

        if obj.mode == 'EDIT':
            point, normal, _ = cut_plane(region, rv3d, start, end, obj.matrix_world.translation)
            cut_edit(context, point, normal)
            return {'FINISHED'}

        lo, hi = _bounds_world(self.targets)
        point, normal, direction = cut_plane(region, rv3d, start, end, (lo + hi) / 2)
        cut_object(context, self.targets, point, normal, direction, self.before_mirror)
        self._keep_selection(context, self.targets, obj)
        return {'FINISHED'}

    def _cut_polygon(self, context, polygon, drawn):
        region, rv3d = context.region, context.region_data
        obj = context.active_object
        if obj.mode == 'EDIT':
            cut_edit_polygon(context, region, rv3d, polygon, drawn)
            return {'FINISHED'}
        cut_object_polygon(context, region, rv3d, polygon, self.targets, drawn, self.before_mirror)
        self._keep_selection(context, self.targets, obj)
        return {'FINISHED'}

    def _cut_aligned(self, context):
        obj = context.active_object
        polygon, _shape, _closed = self.final_shape()
        lo, hi = _bounds_world(self.targets)
        cutter = face_prism(context, self.frame, polygon, self.depth, (hi - lo).length)
        if obj.mode == 'EDIT':
            apply_cutter_edit_mode(context, self.targets, cutter)
            return {'FINISHED'}
        apply_cutter_object_mode(context, self.targets, cutter, self.before_mirror)
        self._keep_selection(context, self.targets, obj)
        return {'FINISHED'}

    def _add(self, context):
        obj = context.active_object
        polygon, _shape, _closed = self.final_shape()
        if obj.mode == 'EDIT':
            add_edit_geometry(obj, self.frame, polygon, self.depth)
        else:
            add_object(context, self.frame, polygon, self.depth, self.hit_object or obj)
        return {'FINISHED'}

    @staticmethod
    def _keep_selection(context, objects, active):
        for o in objects:
            o.select_set(True)
        context.view_layer.objects.active = active


# ---------------------------------------------------------------------------
# Toolbar tools
# ---------------------------------------------------------------------------

_KEYMAP = (
    ("view3d.gtools_smart_cut", {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
    ("view3d.gtools_smart_cut", {"type": 'LEFTMOUSE', "value": 'PRESS', "ctrl": True},
     {"properties": [("select_only", True)]}),
    ("view3d.gtools_smart_cut", {"type": 'LEFTMOUSE', "value": 'PRESS', "ctrl": True, "shift": True},
     {"properties": [("select_only", True), ("toggle", True)]}),
    ("view3d.gtools_smart_cut", {"type": 'LEFTMOUSE', "value": 'PRESS', "shift": True},
     {"properties": [("toggle", True)]}),
)


def _draw_settings(context, layout, tool):
    props = tool.operator_properties("view3d.gtools_smart_cut")
    layout.prop(props, "operation", expand=True)
    row = layout.row(align=True)
    row.enabled = props.operation == 'CUT'  # adding always uses the polyline
    row.prop(props, "shape", expand=True)
    layout.prop(props, "align_to_face")
    if context.mode == 'OBJECT' and props.operation == 'CUT':  # Edit Mode cuts go into the base mesh anyway
        layout.prop(props, "before_mirror")


class GTOOLS_TL_smart_cut_object(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "gtools.smart_cut_object"
    bl_label = "Smart Cut"
    bl_description = (
        "Draw a line or polyline to cut the selected objects non-destructively.\n"
        "The shaded side is removed. F flips it, Shift snaps the angle, hold Space to move.\n"
        "Align to Face cuts into the face under the mouse with an adjustable depth.\n"
        "Add turns the polyline into a new object (depth editable in its Solidify).\n"
        "Ctrl click selects another object, Ctrl Shift click adds it to the selection"
    )
    bl_icon = "ops.mesh.bisect"
    bl_keymap = _KEYMAP
    draw_settings = staticmethod(_draw_settings)


class GTOOLS_TL_smart_cut_edit(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'EDIT_MESH'
    bl_idname = "gtools.smart_cut_edit"
    bl_label = "Smart Cut"
    bl_description = (
        "Draw a line or polyline to cut the mesh (destructive, marks the cut sharp).\n"
        "The shaded side is removed. F flips it, Shift snaps the angle, hold Space to move.\n"
        "Align to Face cuts into the face under the mouse with an adjustable depth.\n"
        "Add extrudes the polyline into the mesh instead of cutting.\n"
        "Ctrl click another object to switch Edit Mode to it"
    )
    bl_icon = "ops.mesh.bisect"
    bl_keymap = _KEYMAP
    draw_settings = staticmethod(_draw_settings)


_TOOLS = (GTOOLS_TL_smart_cut_object, GTOOLS_TL_smart_cut_edit)


def register():
    bpy.utils.register_class(GTOOLS_OT_smart_cut)
    for tool in _TOOLS:
        bpy.utils.register_tool(tool, separator=True, group=False)


def unregister():
    for tool in reversed(_TOOLS):
        bpy.utils.unregister_tool(tool)
    bpy.utils.unregister_class(GTOOLS_OT_smart_cut)
