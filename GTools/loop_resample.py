"""Loop Resample (Edit Mode): change how many sides a loop of faces has, keeping
its shape. A 16-sided cylinder becomes a 15-sided one that is still round.

It also knows bevels (MESHmachine style): select a bevel strip (an open grid of
quads) and the wheel changes its segments, keeping the round (exact arc if it is
round) and its two edges in place. The ends can stop on an n-gon or other faces:
they get the new profile. A flat chamfer going to more segments becomes a round
bevel, tangent to the faces on both sides.

For bevels, R switches the wheel between segments and the bevel's radius (its
size, scaled about the corner it rounds). Going down to 0 segments takes the bevel
away: back to the sharp corner. F (bevel strips) flips which way is the bevel's
profile, for a strip of one face where it can't tell.

Bevels that meet at corners (a beveled box: strips plus corner patches) are
rebuilt instead: the bevel is undone back to its sharp edges and corners, using
the flat faces around it, and Blender's bevel is run again with the new segments
and the same width. The bevel faces connected to the selection are included, so
the corners stay whole.

Select a loop of quads that goes all the way around (one row, or several rows
stacked, like the whole side of a cylinder) and run it from the Select pie
(Ctrl Space in Edit Mode) or the Edge / Face menus. Scroll to set the number of
sides, Enter or click to apply, Esc to cancel. Afterwards the side count can
still be changed in the Adjust Last Operation panel.

Each ring of vertices is resampled evenly along its own shape: an exact circle if
the ring is round, a smooth curve through the old vertices otherwise. Rings stay
lined up, so the flow of the rows is kept.

The ends of the loop can be open, closed by an n-gon cap, or by a triangle fan:
caps are rebuilt with the new vertices. If the loop runs into other unselected
faces, select the whole band (or the cap) and run it again. UVs on the loop are
not kept.
"""

import math

import bmesh
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from .sharp_subtract import hint_panel_origin


# ---------------------------------------------------------------------------
# Safety: every change is tried on a copy first and checked before the real mesh
# is touched (a half-built mesh, or one with edges shared by 3+ faces, can crash
# Blender when it is drawn or beveled).
# ---------------------------------------------------------------------------

class Unsafe(Exception):
    """This geometry can't be rebuilt safely: nothing gets changed."""


def _boundary_loops(bm):
    """How many separate borders (holes / open ends) the mesh has."""
    todo = {e for e in bm.edges if e.is_boundary}
    loops = 0
    while todo:
        loops += 1
        stack = [todo.pop()]
        while stack:
            e = stack.pop()
            for v in e.verts:
                for o in v.link_edges:
                    if o in todo:
                        todo.discard(o)
                        stack.append(o)
    return loops


def mesh_health(bm):
    seen, duplicates = set(), 0
    for f in bm.faces:
        key = frozenset(f.verts)
        if key in seen:
            duplicates += 1
        seen.add(key)
    return {
        "nonmanifold": sum(1 for e in bm.edges if len(e.link_faces) > 2),
        "duplicates": duplicates,
        "wire": sum(1 for e in bm.edges if not e.link_faces),
        "loose": sum(1 for v in bm.verts if not v.link_edges),
        "invalid": sum(1 for v in bm.verts if not all(math.isfinite(c) for c in v.co)),
        "borders": _boundary_loops(bm),
    }


_HEALTH_MESSAGES = {
    "nonmanifold": "edges shared by 3 or more faces",
    "duplicates": "faces on top of each other",
    "wire": "loose edges",
    "loose": "loose vertices",
    "invalid": "invalid vertex positions",
    "borders": "new holes",
}


def health_problem(before, after):
    """Message if the result is worse than the mesh was, else None."""
    for key, what in _HEALTH_MESSAGES.items():
        if after[key] > before[key]:
            return f"the result would have {what}: cancelled, nothing was changed"
    return None


def _local_problem(faces):
    """Quick check of a few faces (and their edges) after merging vertices."""
    faces = [f for f in faces if f.is_valid]
    seen = set()
    for f in faces:
        verts = list(f.verts)
        if len(verts) < 3 or len(set(verts)) != len(verts):
            return "faces collapse"
        key = frozenset(verts)
        if key in seen:
            return "faces end up on top of each other"
        seen.add(key)
        for e in f.edges:
            if len(e.link_faces) > 2:
                return "edges end up shared by 3 or more faces"
    return None


# ---------------------------------------------------------------------------
# Finding the band of faces
# ---------------------------------------------------------------------------

def _walk_row(start_face, exit_loop, faces):
    """Faces of one row, going around through opposite edges. Returns (faces, exit
    loops) if the row closes on itself inside the selection, else None."""
    row, exits = [start_face], [exit_loop]
    loop = exit_loop
    for _ in range(len(faces) + 1):
        other = loop.link_loop_radial_next
        if other is loop:
            return None  # open border
        face = other.face
        if face not in faces or len(face.verts) != 4:
            return None
        if face is start_face:
            return row, exits
        if face in row:
            return None
        row.append(face)
        loop = other.link_loop_next.link_loop_next  # opposite edge
        exits.append(loop)
    return None


def find_band(bm):
    """Rings of vertices (lists, lined up) of a band of selected quads that closes
    around, plus the band faces, or (None, message)."""
    selected = {f for f in bm.faces if f.select}
    faces = {f for f in selected if len(f.verts) == 4}
    if not faces:
        return None, "select a loop of faces"
    band_verts = {v for f in faces for v in f.verts}
    # Other selected faces are fine only if they are caps on the loop (like a
    # cylinder's n-gon, selected along with its sides in vertex mode).
    if any(not set(f.verts) <= band_verts for f in selected - faces):
        return None, "the loop must be made of quads"
    bm.faces.index_update()
    first = min(faces, key=lambda f: f.index)  # same start every time (sets have no order)
    row = None
    for k in (0, 1):
        loops = list(first.loops)
        row = _walk_row(first, loops[k], faces)
        if row is not None:
            break
    if row is None:
        return find_strip(bm, faces)
    row_faces, exits = row
    ring_a = [l.vert for l in exits]
    ring_b = [l.link_loop_next.vert for l in exits]
    used = set(row_faces)
    rows = [row_faces]

    def grow(ring):
        """Next ring beyond this one, through selected faces not used yet."""
        edge = bm.edges.get((ring[0], ring[1]))
        if edge is None:
            return None
        start = [f for f in edge.link_faces if f in faces and f not in used]
        if not start:
            return None
        face = start[0]
        index = {v: i for i, v in enumerate(ring)}
        shared = next(l for l in face.loops if l.edge is edge)
        for candidate in (shared.link_loop_next, shared.link_loop_prev):
            walked = _walk_row(face, candidate, faces)
            if walked is None:
                continue
            rfaces, rexits = walked
            new_ring = [None] * len(ring)
            ok = len(rexits) == len(ring)
            for l in rexits if ok else []:
                a, b = l.vert, l.link_loop_next.vert
                if a in index and b not in index:
                    new_ring[index[a]] = b
                elif b in index and a not in index:
                    new_ring[index[b]] = a
                else:
                    ok = False
                    break
            if ok and all(new_ring):
                used.update(rfaces)
                rows.append(rfaces)
                return new_ring
        return None

    rings = [ring_a, ring_b]
    while True:
        nxt = grow(rings[-1])
        if nxt is None:
            break
        rings.append(nxt)
    while True:
        prv = grow(rings[0])
        if prv is None:
            break
        rings.insert(0, prv)
    if used != faces:
        return find_network(bm, faces)  # not one band: maybe bevels meeting at corners
    cyclic = set(rings[0]) == set(rings[-1]) and len(rings) > 2
    if cyclic:
        rings.pop()
    return {"kind": 'LOOP', "rings": rings, "faces": used, "cyclic": cyclic}, None


def _cap(ring, band_faces):
    """What closes this end ring: ('open',), ('ngon', face), ('fan', center) or None."""
    ring_set = set(ring)
    outside = {f for v in ring for f in v.link_faces if f not in band_faces}
    if not outside:
        return ("open",)
    if len(outside) == 1:
        face = next(iter(outside))
        if ring_set <= set(face.verts) and len(face.verts) == len(ring):
            return ("ngon", face)
    if all(len(f.verts) == 3 for f in outside) and len(outside) == len(ring):
        centers = set.intersection(*[set(f.verts) - ring_set for f in outside])
        if len(centers) == 1:
            return ("fan", next(iter(centers)))
    return None


# ---------------------------------------------------------------------------
# Resampling a ring
# ---------------------------------------------------------------------------

def _plane(points):
    center = sum(points, Vector()) / len(points)
    cov = Matrix(((0.0,) * 3,) * 3)
    for p in points:
        d = p - center
        for i in range(3):
            for j in range(3):
                cov[i][j] += d[i] * d[j]
    normal = Vector((0.3, 0.5, 0.8))
    try:
        inv = cov.inverted()
        for _ in range(32):
            normal = (inv @ normal).normalized()
    except ValueError:
        for axis in (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))):
            if (cov @ axis).length < 1e-12:
                return center, axis
    return center, normal


def _catmull_rom(p0, p1, p2, p3, t):
    t2, t3 = t * t, t * t * t
    return 0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 + (-p0 + 3 * p1 - 3 * p2 + p3) * t3)


def resample_ring(points, count):
    """count points evenly along the closed ring through points, starting at points[0]
    and going the same way round."""
    n = len(points)
    center, normal = _plane(points)
    radii = [((p - center) - normal * (p - center).dot(normal)).length for p in points]
    mean_r = sum(radii) / n
    flat = max(abs((p - center).dot(normal)) for p in points) <= mean_r * 0.005
    if mean_r > 0 and flat and max(abs(r - mean_r) for r in radii) <= mean_r * 0.005:
        # Round: resample the exact circle.
        u = ((points[0] - center) - normal * (points[0] - center).dot(normal)).normalized()
        v = normal.cross(u)
        second = points[1] - center
        sign = 1.0 if second.dot(v) >= 0 else -1.0
        return [center + (u * math.cos(sign * 2 * math.pi * k / count)
                          + v * math.sin(sign * 2 * math.pi * k / count)) * mean_r for k in range(count)]
    # Any other shape: a smooth closed curve through the old points, by length.
    dense = []
    steps = 24
    for i in range(n):
        p0, p1, p2, p3 = points[i - 1], points[i], points[(i + 1) % n], points[(i + 2) % n]
        for s in range(steps):
            dense.append(_catmull_rom(p0, p1, p2, p3, s / steps))
    dense.append(dense[0])
    lengths = [0.0]
    for a, b in zip(dense, dense[1:]):
        lengths.append(lengths[-1] + (b - a).length)
    total = lengths[-1] or 1.0
    out, j = [], 0
    for k in range(count):
        target = total * k / count
        while j < len(lengths) - 2 and lengths[j + 1] < target:
            j += 1
        span = lengths[j + 1] - lengths[j] or 1.0
        out.append(dense[j].lerp(dense[j + 1], (target - lengths[j]) / span))
    return out


# ---------------------------------------------------------------------------
# Rebuilding the band
# ---------------------------------------------------------------------------

def check_band(band):
    """None if the band can be rebuilt, else a message."""
    rings, faces = band["rings"], band["faces"]
    inner = rings if band["cyclic"] else rings[1:-1]
    for ring in inner:
        if any(f not in faces for v in ring for f in v.link_faces):
            return "the loop runs into other faces in the middle"
    if not band["cyclic"]:
        for ring in (rings[0], rings[-1]):
            if _cap(ring, faces) is None:
                return "an end of the loop runs into other faces: select the whole band (or its cap)"
    return None


def resample_band(bm, band, count):
    rings, faces, cyclic = band["rings"], band["faces"], band["cyclic"]
    caps = [] if cyclic else [(0, _cap(rings[0], faces)), (len(rings) - 1, _cap(rings[-1], faces))]
    ref_face = min(faces, key=lambda f: f.index)
    material, smooth = ref_face.material_index, ref_face.smooth
    # Which way the band faces point, to wind the new faces the same way.
    ref_normals = {}
    for r in range(len(rings) - (0 if cyclic else 1)):
        a, b = rings[r], rings[(r + 1) % len(rings)]
        edge = bm.edges.get((a[0], b[0]))
        face = next((f for f in (edge.link_faces if edge else []) if f in faces), ref_face)
        ref_normals[r] = face.normal.copy()

    new_rings = [[bm.verts.new(p) for p in resample_ring([v.co.copy() for v in ring], count)] for ring in rings]
    new_faces = []
    for r in range(len(rings) - (0 if cyclic else 1)):
        a, b = new_rings[r], new_rings[(r + 1) % len(rings)]
        row = []
        for k in range(count):
            row.append(bm.faces.new((a[k], a[(k + 1) % count], b[(k + 1) % count], b[k])))
        row[0].normal_update()
        if row[0].normal.dot(ref_normals[r]) < 0:
            for f in row:
                f.normal_flip()
        new_faces += row

    old_caps = []
    for r, cap in caps:
        if cap[0] == "ngon":
            old = cap[1]
            ring = rings[r]
            order = [ring.index(v) for v in old.verts]
            forward = (order[1] - order[0]) % len(ring) == 1
            verts = new_rings[r] if forward else list(reversed(new_rings[r]))
            face = bm.faces.new(verts)
            face.material_index, face.smooth = old.material_index, old.smooth
            old_caps.append(old)
        elif cap[0] == "fan":
            center = cap[1]
            sample = next(f for f in center.link_faces if f not in faces)
            ring = rings[r]
            # Keep the fan wound like the old one.
            loop = next(l for l in sample.loops if l.vert is center)
            forward = (ring.index(loop.link_loop_next.vert) + 1) % len(ring) == ring.index(loop.link_loop_prev.vert)
            nr = new_rings[r]
            for k in range(count):
                a, b = nr[k], nr[(k + 1) % count]
                face = bm.faces.new((center, a, b) if forward else (center, b, a))
                face.material_index, face.smooth = sample.material_index, sample.smooth

    for f in new_faces:
        f.material_index, f.smooth = material, smooth
    old_verts = {v for ring in rings for v in ring}
    bmesh.ops.delete(bm, geom=list(old_verts), context='VERTS')
    for f in bm.faces:
        f.select = False
    for v in bm.verts:
        v.select = False
    for e in bm.edges:
        e.select = False
    for f in new_faces:
        f.select = True
    bm.select_flush(True)
    return new_faces


# ---------------------------------------------------------------------------
# Bevels: an open strip of quads, resampled across (its profile)
# ---------------------------------------------------------------------------

def find_strip(bm, faces):
    """A bevel-like strip: an open grid of quads. Its profiles are the short way
    across (the bevel segments); they get resampled, the long way stays."""
    from .curve_align import grid_selection
    selected = {v for f in faces for v in f.verts}
    # grid_selection reads vertex selection: make sure it sees exactly these faces
    rows = grid_selection(bm) if all(v.select for v in selected) else None
    if rows is None or {v for r in rows for v in r} != selected:
        return find_network(bm, faces)
    nr, nc = len(rows), len(rows[0])
    columns = [[rows[i][j] for i in range(nr)] for j in range(nc)]

    def turning(lines):
        """Mean total turn (radians) of the polylines: a bevel's profiles curve,
        the lines along it run straight (or turn only at its corners)."""
        total = 0.0
        for line in lines:
            pts = [v.co for v in line]
            for a, b, c in zip(pts, pts[1:], pts[2:]):
                d1, d2 = b - a, c - b
                if d1.length > 1e-9 and d2.length > 1e-9:
                    total += d1.angle(d2)
        return total / len(lines)

    def sharpest(lines):
        worst = 0.0
        for line in lines:
            pts = [v.co for v in line]
            for a, b, c in zip(pts, pts[1:], pts[2:]):
                d1, d2 = b - a, c - b
                if d1.length > 1e-9 and d2.length > 1e-9:
                    worst = max(worst, d1.angle(d2))
        return worst

    # A strip that turns hard corners along its length (chamfers / bevels mitred at a
    # box's corners) is rebuilt as a network, so Blender's bevel makes proper corners.
    if max(sharpest(rows), sharpest(columns)) > math.radians(60):
        network = find_network(bm, faces)
        if network[0] is not None:
            return network

    t_rows, t_cols = turning(rows), turning(columns)
    if abs(t_rows - t_cols) > math.radians(10):
        profiles = rows if t_rows > t_cols else columns
    elif t_rows < math.radians(1) and t_cols < math.radians(1):
        # Flat both ways (a chamfer): the profiles are the way across whose two
        # sides meet faces at a corner; the other way ends on caps facing apart.
        profiles = rows if _side_score(rows, faces) >= _side_score(columns, faces) else columns
    else:  # just as curved both ways (a bevel going round a corner): the short way across
        profiles = rows if nc <= nr else columns
    alt = columns if profiles is rows else rows  # the other way, for F (flip)
    return {"kind": 'BEVEL', "profiles": profiles, "alt_profiles": alt, "faces": faces, "bm": bm}, None


def _side_face(profiles, i, end, faces):
    """The face outside the strip along its side at profile i (end 0 or -1), with
    the side edge it shares: (face, edge) or (None, None)."""
    j = i + 1 if i + 1 < len(profiles) else i - 1
    edge = None
    for e in profiles[i][end].link_edges:
        if e.other_vert(profiles[i][end]) is profiles[j][end]:
            edge = e
            break
    if edge is None:
        return None, None
    outside = [f for f in edge.link_faces if f not in faces]
    return (outside[0], edge) if outside else (None, edge)


def _side_score(profiles, faces):
    """How much the faces on the two sides look like a corner (not two caps facing
    away from each other): higher is more corner-like."""
    if len(profiles) < 2:
        return -2.0
    a, _ = _side_face(profiles, 0, 0, faces)
    b, _ = _side_face(profiles, 0, -1, faces)
    if a is None or b is None:
        return -2.0
    return a.normal.dot(b.normal)


def _tangents(profiles, i, faces):
    """Directions from the two ends of profile i toward the corner the chamfer cut
    off (along the faces on each side), or None."""
    out = []
    for end in (0, -1):
        face, edge = _side_face(profiles, i, end, faces)
        if face is None:
            return None
        inward = None
        for loop in edge.link_loops:
            if loop.face is face:
                along = loop.link_loop_next.vert.co - loop.vert.co
                inward = face.normal.cross(along).normalized()
        if inward is None:
            return None
        out.append(-inward)
    return out


def round_chamfer(a, b, t1, t2, segments):
    """Rounded profile from a to b, tangent to the faces on both sides (t1 / t2 point
    from a / b toward the cut-off corner). An exact arc when the chamfer is even,
    a smooth conic otherwise, with even spacing."""
    from mathutils.geometry import intersect_line_line
    hit = intersect_line_line(a, a + t1, b, b + t2)
    if hit is None:
        return None
    corner = (hit[0] + hit[1]) / 2
    if (corner - a).dot(t1) <= 0 or (corner - b).dot(t2) <= 0:
        return None  # the faces don't meet in front of the chamfer
    # Rational quadratic Bezier a, corner, b: weight cos(half angle) gives a circle
    # when both sides are the same length.
    chord = (b - a)
    if chord.length < 1e-12:
        return None
    half = (corner - a).angle(chord) if (corner - a).length > 1e-12 else 0.0
    w = math.cos(half)
    dense = []
    for k in range(257):
        t = k / 256
        b0, b1, b2 = (1 - t) ** 2, 2 * t * (1 - t) * w, t * t
        dense.append((a * b0 + corner * b1 + b * b2) / (b0 + b1 + b2))
    lengths = [0.0]
    for p, q in zip(dense, dense[1:]):
        lengths.append(lengths[-1] + (q - p).length)
    total = lengths[-1] or 1.0
    out, j = [a.copy()], 0
    for k in range(1, segments):
        target = total * k / segments
        while j < len(lengths) - 2 and lengths[j + 1] < target:
            j += 1
        span = lengths[j + 1] - lengths[j] or 1.0
        out.append(dense[j].lerp(dense[j + 1], (target - lengths[j]) / span))
    return out + [b.copy()]


def _is_straight(points):
    a, b = points[0], points[-1]
    d = b - a
    if d.length < 1e-12:
        return True
    d.normalize()
    return all(((p - a) - d * (p - a).dot(d)).length <= (b - a).length * 1e-3 for p in points)


def _corner(a, b, t1, t2):
    """The corner the bevel rounded: where the faces on both sides meet."""
    from mathutils.geometry import intersect_line_line
    hit = intersect_line_line(a, a + t1, b, b + t2)
    if hit is None:
        return None
    corner = (hit[0] + hit[1]) / 2
    if (corner - a).dot(t1) <= 0 or (corner - b).dot(t2) <= 0:
        return None
    return corner


def shaped_profile(pts, tangents, segments, scale=1.0):
    """New points for a profile: its size scaled about the corner it rounds (scale 1
    keeps it), a flat chamfer going to more segments gets rounded, anything else is
    resampled along its own shape."""
    pts = [p.copy() for p in pts]
    if segments == 0:  # no bevel: back to the sharp corner
        corner = _corner(pts[0], pts[-1], tangents[0], tangents[1]) if tangents is not None else None
        return [corner if corner is not None else (pts[0] + pts[-1]) / 2]
    if tangents is not None and abs(scale - 1.0) > 1e-6:
        corner = _corner(pts[0], pts[-1], tangents[0], tangents[1])
        if corner is not None:
            pts = [corner + (p - corner) * scale for p in pts]
    if segments > 1 and tangents is not None and _is_straight(pts):
        rounded = round_chamfer(pts[0], pts[-1], tangents[0], tangents[1], segments)
        if rounded is not None:
            return rounded
    return resample_profile(pts, segments)


def profile_points(profiles, i, faces, segments, scale=1.0):
    return shaped_profile([v.co for v in profiles[i]], _tangents(profiles, i, faces), segments, scale)


def _circle(a, m, b):
    """Center, radius, normal of the circle through three points (None if in line)."""
    ab, am = b - a, m - a
    normal = ab.cross(am)
    if normal.length < 1e-12:
        return None
    normal.normalize()
    # Center = a + x*ab + y*am, equidistant from a, m, b.
    d11, d12, d22 = ab.dot(ab), ab.dot(am), am.dot(am)
    det = 2 * (d11 * d22 - d12 * d12)
    if abs(det) < 1e-18:
        return None
    x = (d22 * d11 - d12 * d22) / det
    y = (d11 * d22 - d12 * d11) / det
    center = a + ab * x + am * y
    return center, (a - center).length, normal


def resample_profile(points, segments):
    """segments + 1 points along the open profile, ends kept exactly: on the exact
    arc if the profile is round, else along a smooth curve through the old points."""
    a, b = points[0], points[-1]
    n = len(points)
    if n == 2 or segments == 1:
        return [a.lerp(b, k / segments) for k in range(segments + 1)]
    circle = _circle(a, points[n // 2], b)
    if circle is not None:
        center, radius, normal = circle
        round_ = all(abs((p - center).length - radius) <= radius * 0.005 and
                     abs((p - center).dot(normal)) <= radius * 0.005 for p in points)
        if round_:
            u = (a - center).normalized()
            v = normal.cross(u)
            end = b - center
            total = math.atan2(end.dot(v), end.dot(u))
            mid = points[n // 2] - center
            if math.atan2(mid.dot(v), mid.dot(u)) * total < 0 or total == 0:
                total = total - math.copysign(2 * math.pi, total or 1.0)  # go the way through the middle
            return [a] + [center + (u * math.cos(total * k / segments) + v * math.sin(total * k / segments)) * radius
                          for k in range(1, segments)] + [b]
    ext = [2 * a - points[1]] + list(points) + [2 * b - points[-2]]
    dense = []
    steps = 24
    for i in range(1, n):
        for s in range(steps):
            dense.append(_catmull_rom(ext[i - 1], ext[i], ext[i + 1], ext[i + 2], s / steps))
    dense.append(b)
    lengths = [0.0]
    for p, q in zip(dense, dense[1:]):
        lengths.append(lengths[-1] + (q - p).length)
    total = lengths[-1] or 1.0
    out, j = [a], 0
    for k in range(1, segments):
        target = total * k / segments
        while j < len(lengths) - 2 and lengths[j + 1] < target:
            j += 1
        span = lengths[j + 1] - lengths[j] or 1.0
        out.append(dense[j].lerp(dense[j + 1], (target - lengths[j]) / span))
    return out + [b]


def _profile_edges(bm, profile):
    edges = []
    for a, b in zip(profile, profile[1:]):
        e = bm.edges.get((a, b))
        if e is not None:
            edges.append(e)
    return edges


def _end_faces(bm, profile, faces):
    """Faces outside the strip that hold this end profile (an n-gon the bevel ends
    on, etc.), found through the profile's edges so a flat chamfer counts too."""
    return {f for e in _profile_edges(bm, profile) for f in e.link_faces if f not in faces}


def _end_face_plan(bm, profiles, new_profiles, faces):
    """{end face: its new vertex list} for the faces outside the strip that hold an
    end profile. A face holding both ends (a bevel that loops back onto the same
    n-gon) gets both replaced in one go. Raises Unsafe if a face can't take it."""
    plan = {}
    ends = ((profiles[0], new_profiles[0]),)
    if profiles[-1] is not profiles[0]:
        ends += ((profiles[-1], new_profiles[-1]),)
    for profile, new_profile in ends:
        for f in _end_faces(bm, profile, faces):
            verts = _replace_run(plan.get(f, list(f.verts)), profile, new_profile)
            if verts is None:
                raise Unsafe("an end of the bevel meets faces it can't be rebuilt into")
            plan[f] = verts
    for f, verts in plan.items():
        if len(verts) < 3 or len(set(verts)) != len(verts):
            raise Unsafe("an end face of the bevel would collapse")
    return plan


def check_strip(band):
    profiles, faces = band["profiles"], band["faces"]
    for profile in profiles[1:-1]:
        if any(f not in faces for v in profile[1:-1] for f in v.link_faces):
            return "the bevel runs into other faces in the middle"
    try:
        _end_face_plan(band.get("bm"), profiles, profiles, faces)
    except Unsafe as ex:
        return str(ex)
    return None


def _replace_run(face_verts, profile, new_profile):
    """face_verts with the run of profile verts replaced by new_profile (same ends,
    either direction), or None if the face doesn't hold that run in one piece."""
    n = len(face_verts)
    k = len(profile)
    for start in range(n):
        window = [face_verts[(start + i) % n] for i in range(k)]
        for prof, new in ((profile, new_profile), (profile[::-1], new_profile[::-1])):
            if window == prof:
                rest = [face_verts[(start + k + i) % n] for i in range(n - k)]
                return list(new) + rest
    return None


def resample_strip(bm, band, segments, scale=1.0):
    profiles, faces = band["profiles"], band["faces"]
    ref_face = min(faces, key=lambda f: f.index)
    material, smooth = ref_face.material_index, ref_face.smooth
    # Which way each row of faces points (between two profiles), to wind new ones alike.
    ref_normals = []
    for p0, p1 in zip(profiles, profiles[1:]):
        edge = bm.edges.get((p0[0], p0[1]))
        face = ref_face
        for f in (edge.link_faces if edge else []):
            if f in faces and (p1[0] in f.verts or p1[1] in f.verts):
                face = f
                break
        ref_normals.append(face.normal.copy())

    if segments == 0:
        # Un-bevel: every profile collapses onto the corner it rounded.
        corners = [profile_points(profiles, i, faces, 0)[0] for i in range(len(profiles))]
        for profile, corner in zip(profiles, corners):
            bmesh.ops.pointmerge(bm, verts=list(profile), merge_co=corner)
        return []
    new_points = [profile_points(profiles, i, faces, segments, scale) for i in range(len(profiles))]
    new_profiles = []
    for profile, pts in zip(profiles, new_points):
        new_profiles.append([profile[0]] + [bm.verts.new(p) for p in pts[1:-1]] + [profile[-1]])
    # A different radius moves the bevel's edges along the faces next to it.
    for profile, pts in zip(profiles, new_points):
        profile[0].co, profile[-1].co = pts[0], pts[-1]

    # End faces (like an n-gon the bevel ends on): swap in the new profile. Faces
    # that come out the same (same segments) are kept as they are.
    old_end_faces = set()
    for f, verts in _end_face_plan(bm, profiles, new_profiles, faces).items():
        existing = bm.faces.get(verts)
        if existing is f:  # comes out the same face
            continue
        if existing is not None:
            raise Unsafe("an end face of the bevel would land on another face")
        old_end_faces.add(f)
        face = bm.faces.new(verts)
        face.material_index, face.smooth = f.material_index, f.smooth
        face.normal_update()
        if face.normal.dot(f.normal) < 0:
            face.normal_flip()

    new_faces, kept = [], set()
    for r, (a, b) in enumerate(zip(new_profiles, new_profiles[1:])):
        row = []
        for k in range(segments):
            verts = (a[k], a[k + 1], b[k + 1], b[k])
            existing = bm.faces.get(verts)
            if existing is not None:
                if existing not in faces:
                    raise Unsafe("the new bevel would land on faces outside it")
                kept.add(existing)  # an old strip face that stays exactly the same
                row.append(existing)
            else:
                row.append(bm.faces.new(verts))
        row[0].normal_update()
        if row[0] not in kept and row[0].normal.dot(ref_normals[r]) < 0:
            for f in row:
                if f not in kept:
                    f.normal_flip()
        new_faces += row
    for f in new_faces:
        f.material_index, f.smooth = material, smooth

    # Remove the old strip: its faces, the old end faces, the old edges across it
    # (unless the new faces still use them) and the old inner vertices.
    new_edges = {e for f in new_faces for e in f.edges}
    old_across = [e for p in profiles for e in _profile_edges(bm, p) if e not in new_edges]
    bmesh.ops.delete(bm, geom=list((faces - kept) | old_end_faces), context='FACES_ONLY')
    old_across = [e for e in old_across if e.is_valid]
    if old_across:
        bmesh.ops.delete(bm, geom=old_across, context='EDGES')
    old_inner = [v for p in profiles for v in p[1:-1] if v.is_valid]
    if old_inner:
        bmesh.ops.delete(bm, geom=old_inner, context='VERTS')
    for elem in list(bm.verts) + list(bm.edges) + list(bm.faces):
        elem.select = False
    for f in new_faces:
        f.select = True
    bm.select_flush(True)
    return new_faces


# ---------------------------------------------------------------------------
# Bevel networks (bevels meeting at corners): un-bevel, then bevel again
# ---------------------------------------------------------------------------

def _planes(faces):
    """Distinct planes (point, unit normal) of the faces."""
    out = []
    for f in faces:
        n = f.normal.copy()
        if n.length < 0.5:
            continue
        p = f.verts[0].co.copy()
        if not any(abs(n.dot(m)) > 0.9999 and abs((p - q).dot(m)) < 1e-4 for q, m in out):
            out.append((p, n))
    return out


def _target(v, planes, limit):
    """Where the vertex sat before the bevel: on the edge (two planes) or the corner
    (three planes) it rounded, or None. Planes it is within `limit` of count."""
    near = sorted(((abs((v - p).dot(n)), p, n) for p, n in planes), key=lambda x: x[0])
    near = [x for x in near if x[0] <= limit]
    chosen = []
    for _d, p, n in near:
        if all(abs(n.dot(m)) < 0.999 for _q, m in chosen):
            if len(chosen) == 2 and abs(n.dot(chosen[0][1].cross(chosen[1][1]))) < 1e-3:
                continue  # would not pin down a point
            chosen.append((p, n))
        if len(chosen) == 3:
            break
    if len(chosen) == 3:
        (p1, n1), (p2, n2), (p3, n3) = chosen
        det = n1.dot(n2.cross(n3))
        if abs(det) < 1e-9:
            return None
        return (n2.cross(n3) * p1.dot(n1) + n3.cross(n1) * p2.dot(n2) + n1.cross(n2) * p3.dot(n3)) / det
    if len(chosen) == 2:
        (p1, n1), (p2, n2) = chosen
        direction = n1.cross(n2)
        if direction.length < 1e-9:
            return None
        direction.normalize()
        # A point on both planes, then the closest point of that line to v.
        m = Matrix((n1, n2, direction))
        try:
            base = m.inverted() @ Vector((p1.dot(n1), p2.dot(n2), v.dot(direction)))
        except ValueError:
            return None
        return base
    return None


def find_network(bm, faces):
    """Bevel faces that meet at corners (several bevels joined by corner patches, or
    chamfers meeting at mitred corners). Tries the careful way first (also taking the
    bevel faces connected to the selection, so corners stay whole), then the simple
    way: the selection as it is, between all the faces around it."""
    result = _network(bm, faces, careful=True)
    if result[0] is None:
        simple = _network(bm, faces, careful=False)
        if simple[0] is not None:
            return simple
    return result


def _network(bm, faces, careful):
    def around(region):
        return {f for e in {e for f in region for e in f.edges} for f in e.link_faces if f not in region}

    def narrow(f):
        return min(e.calc_length() for e in f.edges)

    # How thin a bevel face is here (its segments): faces around the selection that
    # are just as thin are more bevel (e.g. the bevel going down from a corner); the
    # faces the bevel rounds into are much wider. (A wide chamfer is as wide as the
    # faces around it: the simple way takes all of them as flat.)
    seg = sorted(narrow(f) for f in faces)
    seg_width = seg[len(seg) // 2]

    def flat_faces(candidates):
        if not careful:
            return list(candidates)
        return [f for f in candidates if narrow(f) > seg_width * 2.5]

    region = set(faces)
    if careful:
        # Flat faces selected along with the bevel (even the whole object): leave them
        # out, they are what the bevel rounds into.
        bevel_only = {f for f in region if narrow(f) <= seg_width * 2.5}
        if bevel_only and bevel_only != region:
            region = bevel_only
            seg = sorted(narrow(f) for f in region)
            seg_width = seg[len(seg) // 2]
    width = None
    # The faces around the bevels are flat: their planes say where the sharp edges were.
    for _ in range(64 if careful else 1):
        planes = _planes(flat_faces(around(region)))
        if len(planes) < 2:
            return None, "select a loop (cylinder), a bevel strip or the faces of a bevel"
        rails = [v for f in region for v in f.verts
                 if any(g not in region for g in v.link_faces)]
        gaps = []
        for v in rails:
            dists = sorted(((abs((v.co - p).dot(n)), n) for p, n in planes), key=lambda x: x[0])
            if len(dists) > 1 and dists[0][0] < 1e-4:
                own = dists[0][1]
                # The nearest plane the vertex is NOT on (a vertex on two faces' corner
                # sits on both: that says nothing about the width).
                other = next(((d, n) for d, n in dists[1:] if d > 1e-4 and own.cross(n).length > 1e-3), None)
                if other is not None:
                    gaps.append(other[0] / own.cross(other[1]).length)  # along the face to the old edge
        if width is None:  # measured once, on the selection itself
            if not gaps:
                return None, "couldn't find the faces the bevel sits between"
            gaps.sort()
            width = gaps[len(gaps) // 2]
        limit = width * 1.02
        if not careful:
            break
        # Grow into bevel faces next to the selection (e.g. the bevel going down from a
        # corner): faces whose vertices all sit within the bevel width of two planes.
        grow = set()
        flat = set(flat_faces(around(region)))
        for f in around(region):
            if f in flat:
                continue
            if all(sum(1 for p, n in planes if abs((v.co - p).dot(n)) <= limit) >= 2 for v in f.verts) \
                    and not any(abs(f.normal.dot(n)) > 0.9999 for _p, n in planes):
                grow.add(f)
        if not grow:
            break
        region |= grow
    # A bevel bends away from the faces it rounds into; faces carrying straight on
    # from their neighbours (a row in the middle of a cylinder) are not a bevel.
    for f in region:
        for e in f.edges:
            for g in e.link_faces:
                if g not in region and abs(f.normal.dot(g.normal)) > 0.9999:
                    return None, "this doesn't look like a bevel (it carries straight on into the faces around it)"
    targets = {}
    for v in {v for f in region for v in f.verts}:
        t = _target(v.co, planes, limit)
        if t is None:
            return None, "this doesn't look like a bevel (couldn't find the sharp edges it rounds)"
        targets[v] = t
    return {"kind": 'REBEVEL', "faces": region, "targets": targets, "width": width, "bm": bm,
            "segments": _segments_across(region)}, None


def _segments_across(region):
    """How many segments the bevel has: faces crossed from one side to the other."""
    best = None
    for f in region:
        if len(f.verts) != 4:
            continue
        for loop in f.loops:
            if not any(g not in region for g in loop.edge.link_faces):
                continue
            count, l = 1, loop
            for _ in range(256):
                opposite = l.link_loop_next.link_loop_next
                if any(g not in region for g in opposite.edge.link_faces):
                    break
                nxt = opposite.link_loop_radial_next
                if nxt is opposite or nxt.face not in region or len(nxt.face.verts) != 4:
                    count = None
                    break
                count += 1
                l = nxt
            if count:
                best = count if best is None else min(best, count)
    return best or 1


def unbevel(bm, region, targets):
    """Move every bevel vertex back onto the sharp edge or corner it rounded (merging
    the ones that meet) and return the sharp edges to bevel again. Raises Unsafe if
    the result isn't clean enough to hand to Blender's bevel."""
    key = lambda co: tuple(round(c, 5) for c in co)
    # The edges to bevel again: the bevel's borders, which become the sharp edges.
    edge_keys = set()
    for f in region:
        for e in f.edges:
            if any(g not in region for g in e.link_faces):
                a, b = (key(targets[v]) for v in e.verts)
                if a != b:
                    edge_keys.add(frozenset((a, b)))
    groups = {}
    for v, t in targets.items():
        groups.setdefault(key(t), (t, []))[1].append(v)
    survivors = []
    for t, verts in groups.values():
        verts = [v for v in verts if v.is_valid]
        if not verts:
            continue
        if len(verts) > 1:
            bmesh.ops.pointmerge(bm, verts=verts, merge_co=t)
        else:
            verts[0].co = t
        survivors += [v for v in verts if v.is_valid]
    touched = {f for v in survivors for f in v.link_faces}
    problem = _local_problem(touched)
    if problem:
        raise Unsafe(f"this doesn't look like a bevel that can be rebuilt ({problem})")
    edges = [e for e in {e for f in touched for e in f.edges}
             if frozenset((key(e.verts[0].co), key(e.verts[1].co))) in edge_keys]
    if not edges:
        raise Unsafe("couldn't rebuild the sharp edges of this bevel")
    for e in edges:
        if not 1 <= len(e.link_faces) <= 2 or e.calc_length() < 1e-6:
            raise Unsafe("the sharp edges under this bevel aren't clean (loose, shared or zero length)")
    return edges


def bevel_edges(bm, edges, segments, width):
    """BMesh's bevel with the settings Loop Resample uses (preview and result alike)."""
    return bmesh.ops.bevel(bm, geom=edges, offset=width, offset_type='OFFSET', segments=segments,
                           profile=0.5, affect='EDGES', clamp_overlap=False)


def _copy_band(band):
    """A copy of the mesh and the band's region / targets pointing into it."""
    bm = band["bm"]
    bm.verts.index_update()
    bm.faces.index_update()
    copy = bm.copy()
    copy.verts.ensure_lookup_table()
    copy.faces.ensure_lookup_table()
    targets = {copy.verts[v.index]: t for v, t in band["targets"].items()}
    region = {copy.faces[f.index] for f in band["faces"]}
    return copy, region, targets


def rebevel_preview(band, segments, width=None):
    """Edges (local coordinates, pairs) of what re-beveling would make: done on a
    copy of the mesh with the same un-bevel and bevel as the real one.
    Raises Unsafe if it can't be done safely."""
    copy, region, targets = _copy_band(band)
    try:
        edges = unbevel(copy, region, targets)
        if segments == 0:
            return [(e.verts[0].co.copy(), e.verts[1].co.copy()) for e in edges]
        result = bevel_edges(copy, edges, segments, width or band["width"])
        lines, seen = [], set()
        for f in result.get("faces", []):
            for e in f.edges:
                if e not in seen:
                    seen.add(e)
                    lines.append((e.verts[0].co.copy(), e.verts[1].co.copy()))
        return lines
    finally:
        copy.free()


def rebevel(bm, band, segments, width=None):
    """Un-bevel the network (every bevel vertex back onto its sharp edge or corner),
    then bevel those edges again with the new number of segments. Selects the new
    bevel faces (or the sharp edges, for 0 segments) and returns 'FACE' or 'EDGE'."""
    edges = unbevel(bm, band["faces"], band["targets"])
    for elem in list(bm.verts) + list(bm.edges) + list(bm.faces):
        elem.select = False
    if segments == 0:  # un-bevel only: the sharp edges stay selected
        for e in edges:
            e.select = True
        bm.select_mode = {'EDGE'}
        bm.select_flush_mode()
        return 'EDGE'
    result = bevel_edges(bm, edges, segments, width or band["width"])
    for f in result.get("faces", []):
        f.select = True
    bm.select_mode = {'FACE'}
    bm.select_flush(True)
    return 'FACE'


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _draw_view(op, context):
    gpu.state.blend_set('ALPHA')
    seg = []
    preview = op.preview
    m = op.matrix
    if op.band["kind"] == 'REBEVEL':
        for a, b in getattr(op, "preview_lines", []):
            seg += [tuple(m @ a), tuple(m @ b)]
    loop = op.band["kind"] == 'LOOP'
    for ring in preview:
        for a, b in zip(ring, ring[1:] + (ring[:1] if loop else [])):
            seg += [tuple(m @ a), tuple(m @ b)]
    for r0, r1 in zip(preview, preview[1:] + (preview[:1] if loop and op.band["cyclic"] else [])):
        for a, b in zip(r0, r1):
            seg += [tuple(m @ a), tuple(m @ b)]
    if seg:
        line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        line.uniform_float("viewportSize", (context.region.width, context.region.height))
        line.uniform_float("lineWidth", 2.0)
        line.uniform_float("color", (0.3, 0.85, 1.0, 0.95))
        batch_for_shader(line, 'LINES', {"pos": seg}).draw(line)
    gpu.state.blend_set('NONE')


def _draw_pixel(op, context):
    import blf
    gpu.state.blend_set('ALPHA')
    if op.band["kind"] == 'LOOP':
        title = f"Loop Resample  ·  loop  ·  {op.original} → {op.sides} sides"
    elif op.band["kind"] == 'REBEVEL':
        title = f"Loop Resample  ·  bevel with corners  ·  {op.original} → " + (f"{op.sides} segments" if op.sides else "sharp (no bevel)")
    else:
        title = f"Loop Resample  ·  bevel  ·  {op.original} → " + (f"{op.sides} segments" if op.sides else "sharp (no bevel)")
    if op.band["kind"] != 'LOOP':
        title += f"  ·  radius ×{op.radius:.2f}"
    if op.band["kind"] == 'LOOP':
        wheel_does = "sides"
    else:
        wheel_does = "bevel radius" if op.wheel == 'RADIUS' else "bevel segments"
    hints = [
        ("Wheel  (or + / -)", wheel_does),
        *([("R", "wheel: segments / radius")] if op.band["kind"] != 'LOOP' else []),
        *([("F", "flip: the other way is the bevel")] if op.band["kind"] == 'BEVEL' else []),
        ("Enter / Click", "apply"),
        ("Esc / Right click", "cancel"),
    ]
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

def detect(bm, flip=False):
    """(band, None) for what the selection is, or (None, message)."""
    band, message = find_band(bm)
    if band is not None:
        if band["kind"] == 'BEVEL' and flip:
            band["profiles"], band["alt_profiles"] = band["alt_profiles"], band["profiles"]
        if band["kind"] == 'LOOP':
            message = check_band(band)
        elif band["kind"] == 'BEVEL':
            message = check_strip(band)
        if message and band["kind"] in {'LOOP', 'BEVEL'}:
            # Not a clean cylinder / strip (e.g. the ring of a beveled box, which
            # meets other bevels at its corners): try it as a bevel network.
            network, _net_message = find_network(bm, set(band["faces"]))
            if network is not None:
                band, message = network, None
    if message:
        return None, message
    return band, None


def _backup(bm):
    me = bpy.data.meshes.new("GT_LoopResample_backup")
    copy = bm.copy()
    try:
        copy.to_mesh(me)
    finally:
        copy.free()
    return me


def _restore(bm, me):
    bm.clear()
    bm.from_mesh(me)


class GTOOLS_OT_loop_resample(bpy.types.Operator):
    """Change how many sides the selected loop of faces has, keeping its shape (scroll to set it)"""
    bl_idname = "mesh.gtools_loop_resample"
    bl_label = "Loop Resample"
    bl_options = {'REGISTER', 'UNDO'}

    sides: bpy.props.IntProperty(name="Sides / Segments", description="0 on a bevel takes it away (sharp corner)",
                                 min=0, max=512, default=16)
    flip: bpy.props.BoolProperty(name="Flip", description="Bevel strips: take the other direction as the "
                                                          "bevel's profile", default=False, options={'SKIP_SAVE'})
    radius: bpy.props.FloatProperty(
        name="Radius", description="Bevel size, relative to what it was (bevels only)",
        default=1.0, min=0.05, max=20.0, options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'

    def _band(self, context):
        bm = bmesh.from_edit_mesh(context.active_object.data)
        band, message = detect(bm, self.flip)
        return bm, band, message

    def _run(self, bm):
        """Find the band in bm and rebuild it. Returns (select mode, message):
        a message means nothing could be done."""
        band, message = detect(bm, self.flip)
        if message:
            return None, message
        try:
            if band["kind"] == 'LOOP':
                resample_band(bm, band, max(self.sides, 3))
                return 'FACE', None
            if band["kind"] == 'BEVEL':
                resample_strip(bm, band, max(self.sides, 0), self.radius)
                return 'FACE', None
            return rebevel(bm, band, max(self.sides, 0), band["width"] * self.radius), None
        except Unsafe as ex:
            return None, str(ex)

    def execute(self, context):
        obj = context.active_object
        bm = bmesh.from_edit_mesh(obj.data)
        before = mesh_health(bm)
        # 1. Dry run on a copy: the real mesh is only touched if this works and the
        #    result is clean.
        trial = bm.copy()
        try:
            mode, message = self._run(trial)
            if not message:
                message = health_problem(before, mesh_health(trial))
        except Exception as ex:
            print(f"G-Tools Loop Resample (dry run): {ex!r}")
            message = "this geometry can't be rebuilt (see the console): nothing was changed"
        finally:
            trial.free()
        if message:
            self.report({'WARNING'}, f"Loop Resample: {message}")
            return {'CANCELLED'}
        # 2. The real thing, with a backup in case something still goes wrong.
        backup = _backup(bm)
        try:
            mode, message = self._run(bm)
            if message:
                raise Unsafe(message)
        except Exception as ex:
            print(f"G-Tools Loop Resample: {ex!r}")
            _restore(bm, backup)
            bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
            self.report({'ERROR'}, "Loop Resample: failed, the mesh was restored")
            return {'CANCELLED'}
        finally:
            bpy.data.meshes.remove(backup)
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
        if mode == 'EDGE':  # un-beveled: the sharp edges are selected
            context.tool_settings.mesh_select_mode = (False, True, False)
        return {'FINISHED'}

    def invoke(self, context, event):
        bm, band, message = self._band(context)
        if not message and band["kind"] == 'REBEVEL':
            # Un-beveling must be clean before anything is previewed or beveled.
            try:
                rebevel_preview(band, 0)
            except Unsafe as ex:
                message = str(ex)
        if message:
            self.report({'WARNING'}, f"Loop Resample: {message}")
            return {'CANCELLED'}
        self.band = band
        if band["kind"] == 'LOOP':
            self.ring_points = [[v.co.copy() for v in ring] for ring in band["rings"]]
            self.original = len(band["rings"][0])
        elif band["kind"] == 'REBEVEL':
            self.ring_points = []
            self.tangents = []
            self.original = band["segments"]
        else:
            self.ring_points = [[v.co.copy() for v in p] for p in band["profiles"]]
            self.original = len(band["profiles"][0]) - 1
            # Side directions for rounding a flat chamfer (worked out now, while the
            # mesh data is at hand; the preview only needs them as plain vectors).
            self.tangents = [_tangents(band["profiles"], i, band["faces"]) for i in range(len(band["profiles"]))]
        self.sides = self.original
        self.radius = 1.0
        self.wheel = 'SEGMENTS'  # R switches the wheel to the bevel radius (bevels)
        if context.area is None or context.area.type != 'VIEW_3D':
            return self.execute(context)
        self.matrix = context.active_object.matrix_world.copy()
        self._update_preview()
        self._handles = (
            bpy.types.SpaceView3D.draw_handler_add(_draw_view, (self, context), 'WINDOW', 'POST_VIEW'),
            bpy.types.SpaceView3D.draw_handler_add(_draw_pixel, (self, context), 'WINDOW', 'POST_PIXEL'),
        )
        context.window_manager.modal_handler_add(self)
        self._status(context)
        return {'RUNNING_MODAL'}

    def _status(self, context):
        if self.band["kind"] == 'LOOP':
            text = "Loop Resample: wheel / + / -: sides  |  Enter / click: apply  |  Esc: cancel"
        else:
            what = "radius" if self.wheel == 'RADIUS' else "segments"
            text = (f"Loop Resample: wheel: {what}  |  R: wheel changes segments / radius  |  "
                    "Enter / click: apply  |  Esc: cancel")
        context.workspace.status_text_set(text)

    def _update_preview(self):
        if self.band["kind"] == 'REBEVEL':
            self.preview = []
            try:
                self.preview_lines = rebevel_preview(self.band, max(self.sides, 0),
                                                     self.band["width"] * self.radius)
            except Exception as ex:  # a preview must never stop the tool
                print(f"G-Tools Loop Resample preview: {ex}")
                self.preview_lines = []
            return
        if self.band["kind"] == 'LOOP':
            self.preview = [resample_ring(points, self.sides) for points in self.ring_points]
        else:
            self.preview = [shaped_profile(points, tangents, self.sides, self.radius)
                            for points, tangents in zip(self.ring_points, self.tangents)]

    def _finish(self, context):
        for h in self._handles:
            bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        context.workspace.status_text_set(None)
        context.area.tag_redraw()

    def modal(self, context, event):
        context.area.tag_redraw()
        is_bevel = self.band["kind"] in {'BEVEL', 'REBEVEL'}
        up = event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS', 'PAGE_UP', 'EQUAL'} and event.value == 'PRESS'
        down = event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS', 'PAGE_DOWN', 'MINUS'} and event.value == 'PRESS'
        if event.type == 'F' and event.value == 'PRESS' and self.band["kind"] == 'BEVEL':
            self.flip = not self.flip
            band = self.band
            band["profiles"], band["alt_profiles"] = band["alt_profiles"], band["profiles"]
            self.ring_points = [[v.co.copy() for v in p] for p in band["profiles"]]
            self.tangents = [_tangents(band["profiles"], i, band["faces"]) for i in range(len(band["profiles"]))]
            self.original = len(band["profiles"][0]) - 1
            self._update_preview()
        elif event.type == 'R' and event.value == 'PRESS' and is_bevel:
            self.wheel = 'RADIUS' if self.wheel == 'SEGMENTS' else 'SEGMENTS'
            self._status(context)
        elif (up or down) and self.wheel == 'RADIUS':
            self.radius = max(0.05, min(20.0, self.radius * (1.1 if up else 1 / 1.1)))
            self._update_preview()
        elif up:
            self.sides = min(self.sides + 1, 512)
            self._update_preview()
        elif down:
            self.sides = max(self.sides - 1, 3 if self.band["kind"] == 'LOOP' else 0)
            self._update_preview()
        elif (event.type in {'RET', 'NUMPAD_ENTER'} or event.type == 'LEFTMOUSE') and event.value == 'PRESS':
            self._finish(context)
            if self.sides == self.original and abs(self.radius - 1.0) < 1e-6:
                return {'CANCELLED'}
            return self.execute(context)
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}
        elif event.type in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}


def _menu(self, context):
    self.layout.operator(GTOOLS_OT_loop_resample.bl_idname)


def register():
    bpy.utils.register_class(GTOOLS_OT_loop_resample)
    bpy.types.VIEW3D_MT_edit_mesh_edges.append(_menu)
    bpy.types.VIEW3D_MT_edit_mesh_faces.append(_menu)


def unregister():
    bpy.types.VIEW3D_MT_edit_mesh_faces.remove(_menu)
    bpy.types.VIEW3D_MT_edit_mesh_edges.remove(_menu)
    bpy.utils.unregister_class(GTOOLS_OT_loop_resample)
