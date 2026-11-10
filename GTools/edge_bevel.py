"""Edge Bevel: click a sharp edge in Object Mode to bevel it non-destructively.

The bevel is a boolean, not a Bevel modifier: a cutter shaped like the bevel
profile (chamfer or rounded) runs along the edge and is subtracted with a plain
Geometry Nodes boolean (node group GT_Subtract). So it doesn't depend on the
topology around the edge: it cuts straight through n-gons and boundaries,
like HyperCursor.

By default it takes the edge you click plus its straight continuations (an edge
split by edge loops counts as one). Alt takes the whole loop along the surface
you clicked instead; every straight part of the loop gets its own cutter.

Cutters work like the other live booleans (<base>_Cutter_N, Cutters collection),
so Toggle Cutters, Smart Delete and Apply Booleans work with them. Convex edges
are cut (difference, GT_Subtract); concave edges are filled with a rounded piece
(union clipped to the object, GT_Fill).

Shading (tool header, or S / R while adjusting): Sharp Edges marks the edges where
the bevel meets the object as sharp (node groups GT_SharpSubtract / GT_SharpFill),
so the faces around keep their shading; Smooth Round shades the round smooth.

Ends (tool header, or E while adjusting): Blend fades the bevel out past both
ends, Extend runs it through the whole object, Trim ends flush with the faces
that close the edge. Concave fills always end flush with those faces.
Concave fills ignore this: they stop exactly at an open end and only go past an
end where a wall is there to take them, so they never add geometry in the air.
Before Mirror (tool header, on by default) puts the bevel before the Mirror
modifier.

Hovering only highlights edges that can be beveled. Over the faces of an earlier
bevel it highlights in orange: click to edit it, press X or Delete to remove it.
To bevel on top of a bevel, hover the face next to it (its edge is the nearest
sharp edge there) or right on the edge. The settings live on the cutter (custom property gt_edge_bevel).
"""

import math

import bmesh
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import intersect_point_line

from .sharp_subtract import _prepare_cutter, add_sharp_subtract, get_node_group, hint_panel_origin
from .smart_cut import _round_corner

FLAT_ANGLE = math.radians(0.5)  # below this two faces are coplanar, whatever the shading
STRAIGHT_ANGLE = math.radians(5)  # how far from straight an edge may turn and still count as the same edge


# ---------------------------------------------------------------------------
# Picking edges on the evaluated mesh
# ---------------------------------------------------------------------------

class EvaluatedMesh:
    """BMesh of the object as you see it (all modifiers), for picking."""

    def __init__(self, context, obj):
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        self.bm = bmesh.new()
        self.bm.from_mesh(mesh)
        evaluated.to_mesh_clear()
        self.bm.verts.ensure_lookup_table()
        self.bm.edges.ensure_lookup_table()
        self.bm.faces.ensure_lookup_table()
        self.bvh = BVHTree.FromBMesh(self.bm)
        self.sharp = self.bm.edges.layers.bool.get("sharp_edge")
        self.matrix = obj.matrix_world.copy()
        self.inverse = self.matrix.inverted()

    def free(self):
        self.bm.free()

    def is_sharp(self, edge):
        """Sharp the way it shades: marked sharp (also by Smooth by Angle, which is in
        the evaluated mesh) or next to a flat-shaded face. Edges between coplanar
        faces (n-gon splits) never count."""
        if len(edge.link_faces) != 2:
            return True  # open border
        if edge.calc_face_angle(0.0) < FLAT_ANGLE:
            return False
        if not edge.smooth:  # BMesh shows the mesh's sharp_edge attribute as edge.smooth = False
            return True
        if self.sharp is not None and edge[self.sharp]:
            return True
        f1, f2 = edge.link_faces
        return not (f1.smooth and f2.smooth)

    def can_bevel(self, edge):
        """Sharp and between exactly two faces (convex edges get cut, concave ones filled)."""
        if not self.is_sharp(edge) or len(edge.link_faces) != 2:
            return False
        f1, f2 = edge.link_faces
        return abs(f1.normal.dot(face_inward(f2, edge))) > 1e-4

    def ray(self, region, rv3d, xy):
        """First hit on this object under the mouse: (world point, face index) or (None, None).
        Uses the object's own mesh, so cutters sitting on its surface can't get in the way."""
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, xy)
        ray = view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)
        location, _normal, index, _dist = self.bvh.ray_cast(
            self.inverse @ origin, (self.inverse.to_3x3() @ ray).normalized())
        if location is None:
            return None, None
        return self.matrix @ location, index

    def pick(self, region, rv3d, xy):
        """(sharp edge closest to the mouse on the face under it, that face), or None."""
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, xy)
        ray = view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)
        lo = self.inverse @ origin
        ld = (self.inverse.to_3x3() @ ray).normalized()
        location, _normal, index, _dist = self.bvh.ray_cast(lo, ld)
        if location is None:
            return None
        face = self.bm.faces[index]
        best = None
        for edge in face.edges:
            if not self.can_bevel(edge):
                continue
            a, b = edge.verts[0].co, edge.verts[1].co
            closest, t = intersect_point_line(location, a, b)
            closest = a.lerp(b, max(0.0, min(1.0, t)))
            d = (closest - location).length
            if best is None or d < best[0]:
                best = (d, edge)
        return (best[1], face) if best else None

    def line(self, edge):
        """The edge plus its straight continuations: sharp edges almost in line with
        it (an edge split by edge loops counts as one edge)."""
        chain = [edge]
        seen = {edge}
        for start_vert in edge.verts:
            current, vert = edge, start_vert
            for _ in range(len(self.bm.edges)):
                here = (vert.co - current.other_vert(vert).co).normalized()
                best = None
                for e in vert.link_edges:
                    if e is current or e in seen or not self.is_sharp(e):
                        continue
                    turn = here.angle((e.other_vert(vert).co - vert.co).normalized(), math.pi)
                    if turn < STRAIGHT_ANGLE and (best is None or turn < best[0]):
                        best = (turn, e)
                if best is None:
                    break
                current = best[1]
                seen.add(current)
                chain.append(current)
                vert = current.other_vert(vert)
        return chain

    def runs(self, edges):
        """Split edges into straight runs (each run becomes one bevel cutter)."""
        left = list(edges)
        out = []
        while left:
            first = left.pop(0)
            run = [e for e in self.line(first) if e is first or e in left]
            for e in run[1:]:
                left.remove(e)
            out.append(run)
        return out

    def loop(self, edge, face=None, strict=False):
        """Sharp edges connected to this one, walked along the surface you clicked.
        Works through n-gons because it only looks at edges and face normals:
        at each corner it takes the sharp edge that borders a face parallel to the
        clicked one. If there is none (curved surfaces), it keeps going only when
        a single sharp edge continues in roughly the same direction."""
        if not edge.link_faces:
            return [edge]
        if face is None or face not in edge.link_faces:
            face = max(edge.link_faces, key=lambda f: (len(f.verts), f.calc_area()))
        guide = face.normal.copy()
        # Only follow edges about as sharp as the one picked: this keeps the loop on
        # the real corner and off the soft tangent edges of a bevel made earlier.
        min_angle = edge.calc_face_angle(0.0) * 0.4 if len(edge.link_faces) == 2 else 0.0
        chain = [edge]
        seen = {edge}
        for start_vert in edge.verts:
            current, vert = edge, start_vert
            for _ in range(len(self.bm.edges)):
                here = (vert.co - current.other_vert(vert).co).normalized()
                candidates = [e for e in vert.link_edges
                              if e is not current and e not in seen and self.is_sharp(e)
                              and (len(e.link_faces) != 2 or e.calc_face_angle(0.0) >= min_angle)]
                on_surface = [e for e in candidates
                              if any(f.normal.dot(guide) > 0.999 for f in e.link_faces)]

                def turn(e):
                    return here.angle((e.other_vert(vert).co - vert.co).normalized(), math.pi)

                if on_surface:
                    nxt = min(on_surface, key=turn)
                elif strict:
                    break
                else:
                    straight = [e for e in candidates if turn(e) < math.radians(50)]
                    if len(straight) != 1:
                        break
                    nxt = straight[0]
                current = nxt
                seen.add(nxt)
                chain.append(nxt)
                vert = nxt.other_vert(vert)
        return chain


# ---------------------------------------------------------------------------
# Bevel cutter
# ---------------------------------------------------------------------------

def face_inward(face, edge):
    """Direction (local) from the edge into the face, along the face. Taken from the
    face's winding at that edge, so it is right for any n-gon shape (a face's center
    can be on the wrong side, e.g. a U-shaped n-gon around a slot)."""
    for loop in edge.link_loops:
        if loop.face is face:
            along = loop.link_loop_next.vert.co - loop.vert.co
            return face.normal.cross(along).normalized()
    return Vector()


# ---------------------------------------------------------------------------
# Sweep: the picked edges as an ordered path, with the bevel section along it
# ---------------------------------------------------------------------------

def _ordered(edges):
    """Edges as one ordered path of vertices. Returns (verts, edges_in_order, closed)."""
    edge_set = set(edges)
    links = {}
    for e in edges:
        for v in e.verts:
            links.setdefault(v, []).append(e)
    ends = [v for v, es in links.items() if len(es) == 1]
    start = ends[0] if ends else edges[0].verts[0]
    verts, ordered, used = [start], [], set()
    vert = start
    while True:
        nxt = [e for e in links[vert] if e not in used and e in edge_set]
        if not nxt:
            break
        e = nxt[0]
        used.add(e)
        ordered.append(e)
        vert = e.other_vert(vert)
        if vert is start:
            return verts, ordered, True
        verts.append(vert)
    return verts, ordered, False


def _frame(ev, edge, vert_from, guide):
    """Axis and the two face directions of one edge, in world space. d1 is on the
    guide surface (the face you clicked, or a face parallel to it)."""
    matrix = ev.matrix
    normal_matrix = matrix.inverted().transposed().to_3x3()
    a = matrix @ vert_from.co
    b = matrix @ edge.other_vert(vert_from).co
    axis = (b - a).normalized()
    faces = list(edge.link_faces)
    if guide is not None:
        faces.sort(key=lambda f: -abs(f.normal.dot(guide)))
    dirs, normals = [], []
    for face in faces:
        d = matrix.to_3x3() @ face_inward(face, edge)
        dirs.append((d - axis * d.dot(axis)).normalized())
        normals.append((normal_matrix @ face.normal).normalized())
    side = normals[0].dot(dirs[1])  # > 0: the second face rises out of the first one = concave
    return {"axis": axis, "d1": dirs[0], "d2": dirs[1], "concave": side > 0,
            "flat": abs(side) < 1e-4 or dirs[0].dot(dirs[1]) < -0.9999}


def build_sweeps(ev, edges, guide=None):
    """Turn picked edges into sweeps: ordered paths split where the edge goes from
    convex to concave (those need a cut and a fill). Each sweep: world points,
    one frame per segment between corners, closed flag, end caps."""
    edges = [e for e in edges if len(e.link_faces) == 2]
    if not edges:
        return []
    verts, ordered, closed = _ordered(edges)
    frames = [_frame(ev, e, v, guide) for e, v in zip(ordered, verts)]
    matrix = ev.matrix

    # Corners: keep a vertex only where the path turns (an edge split by edge loops
    # is one straight segment) or where convex / concave changes.
    keep = []
    n = len(ordered)
    for i in range(n + (0 if closed else 1)):
        if not closed and i in (0, n):
            keep.append(i)
            continue
        prev_f, next_f = frames[i - 1], frames[i % n]
        turn = prev_f["axis"].angle(next_f["axis"], 0.0)
        if turn > STRAIGHT_ANGLE or prev_f["concave"] != next_f["concave"]:
            keep.append(i % n)
    if closed and not keep:
        keep = [0]

    # Split into sweeps of one kind (all convex or all concave).
    sweeps = []
    if closed and all(frames[k]["concave"] == frames[keep[0]]["concave"] for k in keep):
        idx = keep
        sweeps.append((idx, True))
    else:
        if closed:  # open the loop at a convex / concave change
            first = next(k for k in keep if frames[k - 1]["concave"] != frames[k]["concave"])
            pos = keep.index(first)
            keep = keep[pos:] + keep[:pos] + [first]
        current = [keep[0]]
        for k in keep[1:]:
            current.append(k)
            seg_frame = frames[current[-2] % n]
            next_frame = frames[k % n] if (k % n) < n else None
            if next_frame is not None and next_frame["concave"] != seg_frame["concave"] and k != keep[-1]:
                sweeps.append((current, False))
                current = [k]
        if len(current) > 1:
            sweeps.append((current, False))

    out = []
    for idx, is_closed in sweeps:
        seg_frames = [frames[i % n] for i in (idx if is_closed else idx[:-1])]
        if not seg_frames or any(f["flat"] for f in seg_frames):
            continue
        # An open path has one more vertex than edges; a closed one wraps around.
        vert_at = (lambda i: verts[i % n]) if closed else (lambda i: verts[i] if i < len(verts) else verts[i % n])
        points = [matrix @ vert_at(i).co for i in idx]
        sweep = {"points": points, "frames": seg_frames, "closed": is_closed,
                 "concave": seg_frames[0]["concave"], "cap_start": None, "cap_end": None}
        if not is_closed:
            first_edges = [ordered[idx[0] % n]]
            last_edges = [ordered[(idx[-1] - 1) % n]]
            sweep["cap_start"] = _end_cap(ev, first_edges, vert_at(idx[0]), -seg_frames[0]["axis"])
            sweep["cap_end"] = _end_cap(ev, last_edges, vert_at(idx[-1]), seg_frames[-1]["axis"])
        out.append(sweep)
    return out


def _end_cap(ev, run, vert, outward):
    """The plane of the face that really closes the path at this end (world point +
    normal pointing away from the path), or None. The bevel is cut flush to it, so
    it ends exactly on that face instead of with a step.

    Only a face (or a flat group of faces, like a triangulated cap) that touches
    BOTH faces of the edge counts, and it must face roughly along the edge. On
    curved or messy ends there is no such face and the bevel just ends square."""
    end_edge = run[-1] if vert in run[-1].verts else run[0]
    side_a, side_b = list(end_edge.link_faces)[:2] if len(end_edge.link_faces) >= 2 else (None, None)
    if side_a is None:
        return None
    side_faces = {f for e in run for f in e.link_faces}
    normal_matrix = ev.matrix.inverted().transposed().to_3x3()
    candidates = [f for f in vert.link_faces if f not in side_faces]
    best = None
    for face in candidates:
        flat_group = [f for f in candidates if abs(f.normal.dot(face.normal)) > 0.999]
        touched = {g for f in flat_group for e in f.edges if vert in e.verts for g in e.link_faces}
        if side_a not in touched or side_b not in touched:
            continue  # doesn't close this edge (just shares the corner point)
        n = (normal_matrix @ face.normal).normalized()
        if n.dot(outward) < 0:
            n = -n
        lean = n.dot(outward)
        if lean > 0.3 and (best is None or lean > best[0]):
            best = (lean, n)
    if best is None:
        return None
    return list(ev.matrix @ vert.co) + list(best[1])


ENDS = (
    ('TRIM', "Trim", "End flush with the faces that close the edge (clean, no extra geometry)"),
    ('EXTEND', "Extend", "Run the bevel all the way through the object along the edge"),
    ('BLEND', "Blend", "Fade the bevel out past both ends (cuts tapering facets into the faces around)"),
)


def _section(frame, width, segments):
    """2D bevel section for one segment: a point on the corner side (outside for a
    convex edge, inside the material for a concave one), then the arc / chamfer
    between the two tangent points on the faces. Plus the 3D axes of that 2D space."""
    d1, d2 = frame["d1"], frame["d2"]
    e1 = d1
    e2 = (d2 - d1 * d2.dot(d1)).normalized()
    a = Vector((1.0, 0.0))
    b = Vector((d2.dot(e1), d2.dot(e2)))
    angle = a.angle(b)
    radius = width * math.tan(angle / 2)
    arc = _round_corner(a * width * 2, Vector((0.0, 0.0)), b * width * 2, radius, max(segments, 1), width * 2)
    outside = -(a + b).normalized() * width * 0.5
    return [outside] + arc, e1, e2


def _corner_ring(point, fa, fb, sec_a, sec_b):
    """Section at a corner between segment a (coming in) and b (going out): each
    section point where its line along a meets its line along b (mitered), so
    the two segments join into one connected shape."""
    from mathutils.geometry import intersect_line_line
    (qa, a1, a2), (qb, b1, b2) = sec_a, sec_b
    ring = []
    for pa, pb in zip(qa, qb):
        oa = point + a1 * pa.x + a2 * pa.y
        ob = point + b1 * pb.x + b2 * pb.y
        if abs(fa["axis"].dot(fb["axis"])) > 0.9999:
            ring.append((oa + ob) / 2)
            continue
        hit = intersect_line_line(oa, oa + fa["axis"], ob, ob + fb["axis"])
        ring.append((hit[0] + hit[1]) / 2 if hit else (oa + ob) / 2)
    return ring


def _cutter_bmesh(sweep, width, segments, ends='BLEND', size=1.0, smooth=True):
    """The cutter (convex) or filler (concave): the bevel section swept along the
    path, mitered at the corners so a loop is one connected piece.

    Ends of an open path: TRIM ends flush with the faces closing the path, EXTEND
    goes through the whole object, BLEND tapers the section to nothing past both
    ends. Concave fills always end flush (anything past would stick out into the air)."""
    points, frames, closed = sweep["points"], sweep["frames"], sweep["closed"]
    sections = [_section(f, width, segments) for f in frames]
    count = len(sections[0][0])
    eps = max(width * 0.01, 1e-5)
    concave = sweep.get("concave")

    def flat(point, sec, scale=1.0):
        q, e1, e2 = sec
        return [point + (e1 * p.x + e2 * p.y) * scale for p in q]

    def capped(point, frame, sec, cap, offset):
        """Section projected along the edge onto the end face. None if that would
        stretch it out far (a face at a steep angle): then the end stays square."""
        q, e1, e2 = sec
        co, n = Vector(cap[:3]), Vector(cap[3:])
        axis = frame["axis"]
        if abs(axis.dot(n)) < 0.3:
            return None
        limit = width * 3 + abs(offset)
        ring = []
        for p in q:
            off = e1 * p.x + e2 * p.y
            s = ((co - point - off).dot(n) + offset) / axis.dot(n)
            if abs(s) > limit:
                return None
            ring.append(point + axis * s + off)
        return ring

    def end_rings(at_start):
        i = 0 if at_start else -1
        point, frame, sec = points[i], frames[i], sections[i]
        out_dir = -frame["axis"] if at_start else frame["axis"]
        cap = sweep.get("cap_start" if at_start else "cap_end")
        if concave:
            rings = [(cap and capped(point, frame, sec, cap, 0.0)) or flat(point, sec)]
        elif ends == 'EXTEND':
            rings = [flat(point + out_dir * size, sec)]
        elif ends == 'BLEND':
            rings = [flat(point + out_dir * width * 3, sec, 0.02), flat(point, sec)]
        else:  # TRIM: flush with the end face, a hair outside
            rings = [(cap and capped(point, frame, sec, cap, eps))
                     or flat(point + out_dir * eps, sec)]
        return rings if at_start else list(reversed(rings))

    rings = []
    if closed:
        n = len(points)
        for i in range(n):
            rings.append(_corner_ring(points[i], frames[i - 1], frames[i], sections[i - 1], sections[i]))
    else:
        rings += end_rings(True)
        for i in range(1, len(points) - 1):
            rings.append(_corner_ring(points[i], frames[i - 1], frames[i], sections[i - 1], sections[i]))
        rings += end_rings(False)

    bm = bmesh.new()
    vrings = [[bm.verts.new(p) for p in ring] for ring in rings]
    pairs = list(zip(vrings, vrings[1:]))
    if closed:
        pairs.append((vrings[-1], vrings[0]))
    else:
        bm.faces.new(vrings[0])
        bm.faces.new(list(reversed(vrings[-1])))
    for r0, r1 in pairs:
        for k in range(count):
            j = (k + 1) % count
            face = bm.faces.new((r0[k], r0[j], r1[j], r1[k]))
            # The rounded part shades smooth (the boolean carries this over to the object).
            face.smooth = smooth and 1 <= k < count - 1 and segments > 1
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


PROP = "gt_edge_bevel"  # custom property on the cutter with the bevel settings


def _plausible(bm, sweep, width, ends, size):
    """Sanity check of a cutter before it touches the object: closed (manifold) and
    no bigger than a bevel of this width along this path could be. A path picked
    over odd geometry can produce a twisted cutter that would eat the whole mesh."""
    if not all(e.is_manifold for e in bm.edges):
        return False
    # It must not run into itself (a path doubling back over odd geometry).
    tree = BVHTree.FromBMesh(bm)
    bm.faces.ensure_lookup_table()
    for i, j in tree.overlap(tree):
        if i == j:
            continue
        fi, fj = bm.faces[i], bm.faces[j]
        if not set(fi.verts) & set(fj.verts):
            return False
    pts = sweep["points"]
    length = sum((b - a).length for a, b in zip(pts, pts[1:]))
    if sweep["closed"]:
        length += (pts[0] - pts[-1]).length
    if ends == 'EXTEND':
        length += 2 * size
    else:
        length += 6 * width
    volume = abs(bm.calc_volume(signed=True))
    return 0 < volume <= length * (3 * width) ** 2


def _pack(sweep):
    return {
        "points": [c for p in sweep["points"] for c in p],
        "frames": [c for f in sweep["frames"] for c in (*f["axis"], *f["d1"], *f["d2"])],
        "closed": bool(sweep["closed"]), "concave": bool(sweep["concave"]),
        "cap_start": list(sweep.get("cap_start") or []), "cap_end": list(sweep.get("cap_end") or []),
    }


def _unpack(data):
    if "points" not in data:  # made by an older version: one straight run
        p0, axis = Vector(data["p0"]), Vector(data["axis"])
        frame = {"axis": axis, "d1": Vector(data["d1"]), "d2": Vector(data["d2"]),
                 "concave": bool(data.get("concave", False))}
        return {"points": [p0 + axis * data["start"], p0 + axis * data["end"]], "frames": [frame],
                "closed": False, "concave": frame["concave"],
                "cap_start": list(data["cap_start"]) if data.get("cap_start") else None,
                "cap_end": list(data["cap_end"]) if data.get("cap_end") else None}
    pts = list(data["points"])
    fr = list(data["frames"])
    concave = bool(data["concave"])
    frames = []
    for i in range(0, len(fr), 9):
        frames.append({"axis": Vector(fr[i:i + 3]), "d1": Vector(fr[i + 3:i + 6]), "d2": Vector(fr[i + 6:i + 9]),
                       "concave": concave})
    return {"points": [Vector(pts[i:i + 3]) for i in range(0, len(pts), 3)], "frames": frames,
            "closed": bool(data["closed"]), "concave": concave,
            "cap_start": list(data["cap_start"]) or None, "cap_end": list(data["cap_end"]) or None}


class BoolBevel:
    """One cutter (or filler) + boolean modifier for one sweep."""

    def __init__(self, context, obj, sweep, width, segments, ends='BLEND', before_mirror=True,
                 mark_sharp=True, smooth=True):
        self.obj = obj
        self.profile = sweep
        self.ends = ends
        self.mark_sharp = mark_sharp
        self.smooth = smooth
        self.size = max(Vector(obj.dimensions).length, 0.01)  # how far "Extend" goes
        mesh = bpy.data.meshes.new("EdgeBevel")
        self.cutter = bpy.data.objects.new("EdgeBevel", mesh)
        context.scene.collection.objects.link(self.cutter)
        self.update(width, segments)
        _prepare_cutter(context, obj, self.cutter)
        # Concave edges are filled (union), convex ones cut (difference). With mark_sharp
        # the edges where the bevel meets the object are marked sharp, so the flat faces
        # around it keep flat shading instead of smearing into the round.
        self.mod = add_sharp_subtract(obj, self.cutter, self._group(), before_mirror)

    def _group(self):
        return get_node_group(sharp=self.mark_sharp, union=bool(self.profile.get("concave")))

    def set_shading(self, mark_sharp, smooth):
        """Change sharp marks / smooth round on an existing bevel."""
        changed = mark_sharp != self.mark_sharp
        self.mark_sharp, self.smooth = mark_sharp, smooth
        if changed:
            node_group = self._group()
            from .sharp_subtract import _object_input
            identifier = _object_input(node_group)
            self.mod.node_group = node_group
            props = getattr(self.mod, "properties", None)
            socket = getattr(getattr(props, "inputs", None), identifier, None)
            if socket is not None:
                socket.value = self.cutter
            else:
                self.mod[identifier] = self.cutter
        self.update(self.width, self.segments)

    @classmethod
    def attach(cls, obj, mod, cutter):
        """Wrap a bevel made earlier (to edit or delete it)."""
        self = cls.__new__(cls)
        self.obj, self.mod, self.cutter = obj, mod, cutter
        data = cutter[PROP]
        self.profile = _unpack(data)
        self.width, self.segments = data["width"], int(data["segments"])
        self.ends = data.get("ends", 'TRIM')
        self.size = data.get("size", max(obj.dimensions) or 1.0)
        self.mark_sharp = bool(data.get("mark_sharp", False))
        self.smooth = bool(data.get("smooth", True))
        self.valid = True
        return self

    def update(self, width, segments, ends=None):
        self.width, self.segments = width, segments
        if ends is not None:
            self.ends = ends
        data = _pack(self.profile)
        data.update(width=width, segments=segments, ends=self.ends, size=self.size,
                    mark_sharp=self.mark_sharp, smooth=self.smooth)
        self.cutter[PROP] = data
        bm = _cutter_bmesh(self.profile, max(width, 1e-6), segments, self.ends, self.size, self.smooth)
        self.valid = _plausible(bm, self.profile, width, self.ends, self.size)
        # Keep the cutter's origin at its middle, so it is easy to grab later.
        center = sum((v.co for v in bm.verts), Vector()) / len(bm.verts)
        for v in bm.verts:
            v.co -= center
        bm.to_mesh(self.cutter.data)
        bm.free()
        self.cutter.data.update()
        self.cutter.location = center

    def remove(self):
        self.obj.modifiers.remove(self.mod)
        mesh = self.cutter.data
        bpy.data.objects.remove(self.cutter)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    def anchor(self):
        """A world point on the bevel (for mouse sensitivity)."""
        return self.profile["points"][0]

    def surface_lines(self, lift):
        """World lines showing the bevel on the surface: where it starts on each face
        and the middle of the round, along the whole path."""
        sweep = self.profile
        rings = []
        pts, frames = sweep["points"], sweep["frames"]
        sections = [_section(f, self.width, self.segments) for f in frames]
        n = len(pts)
        for i in range(n):
            if sweep["closed"]:
                rings.append(_corner_ring(pts[i], frames[i - 1], frames[i], sections[i - 1], sections[i]))
            elif i in (0, n - 1):
                q, e1, e2 = sections[0 if i == 0 else -1]
                rings.append([pts[i] + e1 * p.x + e2 * p.y for p in q])
            else:
                rings.append(_corner_ring(pts[i], frames[i - 1], frames[i], sections[i - 1], sections[i]))
        count = len(rings[0])
        picks = [1, count // 2 + 1 if count > 3 else 1, count - 1]
        lines = []
        pairs = list(zip(rings, rings[1:])) + ([(rings[-1], rings[0])] if sweep["closed"] else [])
        for r0, r1 in pairs:
            for k in picks:
                mid = (r0[k] + r1[k]) / 2
                out = (mid - (r0[0] + r1[0]) / 2).normalized() * -lift
                lines += [r0[k] + out, r1[k] + out]
        return lines


def _mod_cutter(mod):
    """The cutter object of a GT_Subtract / GT_Union modifier, or None."""
    if mod.type != 'NODES' or mod.node_group is None or \
            not mod.node_group.name.startswith(("GT_Subtract", "GT_Union", "GT_SharpSubtract", "GT_SharpUnion", "GT_Fill", "GT_SharpFill")):
        return None
    for item in mod.node_group.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.socket_type == 'NodeSocketObject':
            props = getattr(mod, "properties", None)
            socket = getattr(getattr(props, "inputs", None), item.identifier, None)
            return socket.value if socket is not None else mod.get(item.identifier)
    return None


def find_bevels(obj):
    """Edge bevels on the object: list of BoolBevel."""
    out = []
    for mod in obj.modifiers:
        cutter = _mod_cutter(mod)
        if cutter is not None and PROP in cutter:
            out.append(BoolBevel.attach(obj, mod, cutter))
    return out


def bevel_at(obj, location):
    """The edge bevel whose rounded surface is at this world point, or None."""
    for bevel in find_bevels(obj):
        mesh = bevel.cutter.data
        matrix = bevel.cutter.matrix_world
        tree = BVHTree.FromPolygons([matrix @ v.co for v in mesh.vertices], [p.vertices for p in mesh.polygons])
        nearest = tree.find_nearest(location)
        if nearest[0] is not None and nearest[3] < max(bevel.width * 0.05, 1e-4):
            return bevel
    return None


# ---------------------------------------------------------------------------
# Drawing (while adjusting)
# ---------------------------------------------------------------------------

def _draw_callback(op, context):
    region, rv3d = context.region, context.region_data
    seg = []
    for e in ([] if op.editing else op.edges):
        a = view3d_utils.location_3d_to_region_2d(region, rv3d, op.evaluated.matrix @ e.verts[0].co)
        b = view3d_utils.location_3d_to_region_2d(region, rv3d, op.evaluated.matrix @ e.verts[1].co)
        if a is not None and b is not None:
            seg += [tuple(a), tuple(b)]
    gpu.state.blend_set('ALPHA')
    if seg:
        shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        shader.uniform_float("viewportSize", (region.width, region.height))
        shader.uniform_float("lineWidth", 2.0)
        shader.uniform_float("color", (0.3, 0.85, 1.0, 0.6))
        batch_for_shader(shader, 'LINES', {"pos": seg}).draw(shader)
    import blf
    font = 0
    blf.size(font, 13)
    blf.color(font, 1.0, 0.8, 0.2, 1.0)
    x, y = op.hud_pos
    blf.position(font, x + 18, y - 22, 0)
    kind = (("chamfer" if op.segments == 1 else f"{op.segments} segments") + f"   ends: {op.end_mode.lower()}"
            + f"   sharp edges: {'on' if op.sharp_mode else 'off'}   round: {'smooth' if op.smooth_mode else 'flat'}")
    if op.editing:
        blf.draw(font, f"Editing bevel  width {op.width:.3f}   {kind}")
    else:
        mode = "loop" if op.use_loop else "edge"
        blf.draw(font, f"Bevel  width {op.width:.3f}   {kind}   ({mode}, {len(op.bevels)} cutters)")
    _draw_hints(context, EDIT_HINTS if op.editing else HINTS)
    gpu.state.blend_set('NONE')


EDIT_HINTS = [
    ("Mouse left / right", "width"),
    ("Wheel", "segments (1 = chamfer)"),
    ("E", "ends: trim / extend / blend"),
    ("S", "sharp edges on / off"),
    ("R", "round smooth / flat"),
    ("X / Delete", "delete this bevel"),
    ("Click / Enter", "confirm"),
    ("Esc / Right click", "cancel (keep it as it was)"),
]

HINTS = [
    ("Mouse left / right", "width"),
    ("Wheel", "segments (1 = chamfer)"),
    ("E", "ends: trim / extend / blend"),
    ("S", "sharp edges on / off"),
    ("R", "round smooth / flat"),
    ("Alt", "edge / whole loop"),
    ("Shift click", "add another edge"),
    ("Click / Enter", "confirm"),
    ("Esc / Right click", "cancel"),
]


def _draw_hints(context, hints):
    import blf
    font, size = 0, 12
    blf.size(font, size)
    line_h = size * 1.55
    title = "Edge Bevel" + ("  ·  Edit" if hints is EDIT_HINTS else "")
    key_w = max(blf.dimensions(font, k)[0] for k, _ in hints) + 14
    width = max(blf.dimensions(font, title)[0], key_w + max(blf.dimensions(font, a)[0] for _, a in hints)) + 20
    height = line_h * (len(hints) + 1) + 14
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
    for key, action in hints:
        ty -= line_h
        blf.color(font, 1, 1, 1, 0.95)
        blf.position(font, x + 10, ty, 0)
        blf.draw(font, key)
        blf.color(font, 1, 1, 1, 0.6)
        blf.position(font, x + 10 + key_w, ty, 0)
        blf.draw(font, action)


# ---------------------------------------------------------------------------
# Hover preview (while the tool is active, before clicking)
# ---------------------------------------------------------------------------

_cache = {}           # object name -> EvaluatedMesh (rebuilt when the depsgraph changes)
_hover = {}           # what is under the mouse: segments / face triangles in world space
_state = {"modal": False, "handle": None}


def _clear_cache():
    for ev in _cache.values():
        ev.free()
    _cache.clear()


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph):
    if _state["modal"]:
        return
    for update in depsgraph.updates:
        if isinstance(update.id, bpy.types.Object) and update.id.original.name in _cache:
            ev = _cache.pop(update.id.original.name)
            ev.free()
            _hover.clear()


def _cached(context, obj):
    ev = _cache.get(obj.name)
    if ev is None or ev.matrix != obj.matrix_world:
        if ev is not None:
            ev.free()
        ev = EvaluatedMesh(context, obj)
        # Lines are lifted a little off the surface so they don't flicker into it.
        coords = [v.co for v in ev.bm.verts]
        span = max((max(c[i] for c in coords) - min(c[i] for c in coords)) for i in range(3)) if coords else 1.0
        ev.lift = max(obj.matrix_world.to_scale()) * span * 0.0015
        ev.sharp_segments = _segments_world(ev, [e for e in ev.bm.edges if ev.can_bevel(e)])
        _cache[obj.name] = ev
    return ev


def _offset(ev, vert):
    """World position nudged out along the normal, so lines draw over the surface."""
    n = (ev.matrix.inverted().transposed().to_3x3() @ vert.normal).normalized()
    return ev.matrix @ vert.co + n * ev.lift


def _segments_world(ev, edges):
    pts = []
    for e in edges:
        pts += [_offset(ev, e.verts[0]), _offset(ev, e.verts[1])]
    return pts


def _tool_active(context):
    try:
        tool = context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
    except (AttributeError, TypeError):
        return False
    return tool is not None and tool.idname == GTOOLS_TL_edge_bevel.bl_idname


def _draw_hover():
    context = bpy.context
    if _state["modal"] or not _hover or not _tool_active(context):
        return
    if _hover.get("area") != context.area.as_pointer():
        return
    line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    if "batches" not in _hover:
        def batch(key):
            pts = _hover.get(key)
            return batch_for_shader(line, 'LINES', {"pos": pts}) if pts else None
        _hover["batches"] = (batch("sharp"), batch("loop"), batch("bevel"))
    sharp_b, loop_b, bevel_b = _hover["batches"]
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(False)
    line.uniform_float("viewportSize", (context.region.width, context.region.height))
    if bevel_b is not None:  # an existing bevel: click to edit, X to delete
        line.uniform_float("lineWidth", 3.0)
        line.uniform_float("color", (1.0, 0.6, 0.2, 1.0))
        bevel_b.draw(line)
    if sharp_b is not None:  # everything that can be beveled
        line.uniform_float("lineWidth", 1.5)
        line.uniform_float("color", (0.3, 0.85, 1.0, 0.35))
        sharp_b.draw(line)
    if loop_b is not None:  # what a click would bevel
        line.uniform_float("lineWidth", 3.5)
        line.uniform_float("color", (0.3, 0.85, 1.0, 1.0))
        loop_b.draw(line)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


class GTOOLS_OT_edge_bevel_hover(bpy.types.Operator):
    """Preview which edges Edge Bevel would take (runs on mouse move while the tool is active)"""
    bl_idname = "view3d.gtools_edge_bevel_hover"
    bl_label = "Edge Bevel Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and context.mode == 'OBJECT'

    def invoke(self, context, event):
        if _state["modal"]:
            return {'PASS_THROUGH'}
        mouse = (event.mouse_region_x, event.mouse_region_y)
        had = bool(_hover)
        obj = _object_under_mouse(context, mouse)
        picked = None
        if obj is not None:
            ev = _cached(context, obj)
            kind, target = decide(context, obj, ev, mouse)
            bevel = target if kind == 'bevel' else None
            picked = target if kind == 'edge' else None
            if bevel is not None:
                key = (obj.name, "bevel", bevel.cutter.name)
                if _hover.get("key") != key:
                    _hover.clear()
                    _hover.update(key=key, area=context.area.as_pointer(), sharp=ev.sharp_segments,
                                  bevel=bevel.surface_lines(ev.lift), bevel_name=bevel.cutter.name)
                    context.area.tag_redraw()
                return {'PASS_THROUGH'}
        if picked is None:
            if obj is not None:  # over an object, no sharp edge right here: show what can be beveled
                key = (obj.name, None)
                if _hover.get("key") != key:
                    _hover.clear()
                    _hover.update(key=key, area=context.area.as_pointer(), loop=[],
                                  sharp=_cache[obj.name].sharp_segments)
                    context.area.tag_redraw()
            elif had:
                _hover.clear()
                context.area.tag_redraw()
            return {'PASS_THROUGH'}
        edge, face = picked
        key = (obj.name, edge.index, face.index, event.alt)
        if _hover.get("key") != key:
            ev = _cache[obj.name]
            edges = ev.loop(edge, face) if event.alt else ev.line(edge)
            _hover.clear()
            _hover.update(key=key, area=context.area.as_pointer(), sharp=ev.sharp_segments,
                          loop=_segments_world(ev, edges))
            context.area.tag_redraw()
        return {'PASS_THROUGH'}


def _is_manifold(context, obj):
    """Is the object, as it shows now (all modifiers), a closed mesh?"""
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    bm = bmesh.new()
    bm.from_mesh(mesh)
    ok = all(e.is_manifold for e in bm.edges)
    bm.free()
    evaluated.to_mesh_clear()
    return ok


EDGE_ON_BEVEL_PX = 4  # over an earlier bevel, only this close to an edge does a click add a new bevel


def _edge_screen_distance(ev, region, rv3d, edge, xy):
    a = view3d_utils.location_3d_to_region_2d(region, rv3d, ev.matrix @ edge.verts[0].co)
    b = view3d_utils.location_3d_to_region_2d(region, rv3d, ev.matrix @ edge.verts[1].co)
    if a is None or b is None:
        return float('inf')
    p = Vector(xy)
    d = b - a
    t = 0.0 if d.length_squared < 1e-9 else max(0.0, min(1.0, (p - a).dot(d) / d.length_squared))
    return (p - (a + d * t)).length


def decide(context, obj, ev, xy):
    """What a click here would do: ('edge', (edge, face)), ('bevel', bevel) or (None, None).
    Over the faces of an earlier bevel: edit it (only right on one of its edges does a
    click add a new bevel there). Anywhere else: bevel the nearest sharp edge, which
    next to an earlier bevel can be the edge it made (bevel on bevel)."""
    region, rv3d = context.region, context.region_data
    location, _face = ev.ray(region, rv3d, xy)
    if location is None:
        return None, None
    picked = ev.pick(region, rv3d, xy)
    bevel = bevel_at(obj, location)
    if bevel is not None:
        if picked is not None and _edge_screen_distance(ev, region, rv3d, picked[0], xy) <= EDGE_ON_BEVEL_PX:
            return 'edge', picked
        return 'bevel', bevel
    if picked is not None:
        return 'edge', picked
    return None, None


def _object_under_mouse(context, xy, with_location=False):
    region, rv3d = context.region, context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, xy)
    ray = view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)
    depsgraph = context.evaluated_depsgraph_get()
    for _ in range(16):
        hit, location, _n, _i, obj, _m = context.scene.ray_cast(depsgraph, origin, ray)
        if not hit:
            break
        original = obj.original
        if original.type == 'MESH' and original.display_type not in {'WIRE', 'BOUNDS'}:
            return (original, location) if with_location else original
        origin = location + ray * 1e-4
    return (None, None) if with_location else None


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class GTOOLS_OT_edge_bevel(bpy.types.Operator):
    """Click a sharp edge to bevel it with a boolean (non-destructive). Move the mouse for the width"""
    bl_idname = "view3d.gtools_edge_bevel"
    bl_label = "Edge Bevel"
    bl_options = {'REGISTER', 'UNDO'}

    ends: bpy.props.EnumProperty(name="Ends", items=ENDS, default='TRIM')
    before_mirror: bpy.props.BoolProperty(
        name="Before Mirror",
        description="Put the bevel before the Mirror modifier so it is mirrored too",
        default=True,
    )
    mark_sharp: bpy.props.BoolProperty(
        name="Sharp Edges",
        description="Mark sharp where the bevel meets the object, so the faces around it keep their own "
                    "shading (no smearing into the round)",
        default=True,
    )
    smooth_round: bpy.props.BoolProperty(
        name="Smooth Round",
        description="Shade the rounded part of the bevel smooth (off: flat facets)",
        default=True,
    )
    select_only: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})
    toggle: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and context.mode == 'OBJECT'

    def invoke(self, context, event):
        mouse = (event.mouse_region_x, event.mouse_region_y)
        self.end_mode = self.ends
        self.sharp_mode = self.mark_sharp
        self.smooth_mode = self.smooth_round
        if self.select_only:
            bpy.ops.view3d.select(toggle=self.toggle, deselect_all=not self.toggle,
                                  location=(int(mouse[0]), int(mouse[1])))
            return {'FINISHED'}
        obj, location = _object_under_mouse(context, mouse, with_location=True)
        if obj is None:
            return {'PASS_THROUGH'}
        self.obj = obj
        self.hud_pos = mouse
        self.x0 = event.mouse_region_x
        kind, target = decide(context, obj, _cached(context, obj), mouse)
        if kind == 'bevel':
            return self._start_edit(context, target)
        self.editing = False
        self.evaluated = EvaluatedMesh(context, obj)
        self._was_manifold = all(e.is_manifold for e in self.evaluated.bm.edges)  # before the new bevel
        picked = self.evaluated.pick(context.region, context.region_data, mouse)
        if picked is None:
            self.evaluated.free()
            self.report({'INFO'}, "Edge Bevel: no sharp edge there")
            return {'CANCELLED'}
        edge = picked[0]

        self.use_loop = event.alt
        self.picked = [picked]
        r, rv = context.region, context.region_data
        p = obj.matrix_world @ edge.verts[0].co
        s = view3d_utils.location_3d_to_region_2d(r, rv, p) or Vector(mouse)
        a = view3d_utils.region_2d_to_location_3d(r, rv, s, p)
        b = view3d_utils.region_2d_to_location_3d(r, rv, s + Vector((1, 0)), p)
        self.px = max((b - a).length, 1e-6)  # one pixel of mouse movement, in world units
        self.width = self.px * 10
        self.segments = 4
        self.w0 = self.width
        self.bevels = []
        self.edges = []
        self.skipped = False
        self.loop_fallback = False
        if not self._rebuild(context):
            self.evaluated.free()
            self.report({'WARNING'}, "Edge Bevel: couldn't build a clean bevel there" if self.skipped
                        else "Edge Bevel: only sharp edges between two faces can be beveled")
            return {'CANCELLED'}
        if self.loop_fallback:
            self.report({'INFO'}, "Edge Bevel: no clean loop there, beveling the single edge")
        elif self.skipped:
            self.report({'INFO'}, "Edge Bevel: part of the edges was left out (odd geometry there)")

        self._start_modal(context,
                          "Edge Bevel: move left / right: width  |  Wheel: segments (1 = chamfer)  |  E: ends  |  "
                          "S: sharp edges  |  R: smooth / flat round  |  "
                          "Alt: edge / loop  |  Shift click: add edge  |  Click / Enter: confirm  |  "
                          "Esc / Right click: cancel")
        return {'RUNNING_MODAL'}

    def _start_modal(self, context, status):
        if getattr(self, "_was_manifold", None) is None:  # editing: the object as it is now
            self._was_manifold = _is_manifold(context, self.obj)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_callback, (self, context), 'WINDOW', 'POST_PIXEL')
        _state["modal"] = True
        _hover.clear()
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(status)

    def _start_edit(self, context, bevel):
        """Click on a bevel made earlier: change its width / segments, or delete it."""
        self.editing = True
        self.evaluated = None
        self.use_loop = False
        self.edges = []
        self.bevels = [bevel]
        self.width, self.segments = bevel.width, bevel.segments
        self.end_mode = bevel.ends
        self.sharp_mode, self.smooth_mode = bevel.mark_sharp, bevel.smooth
        self.original = (bevel.width, bevel.segments, bevel.ends)
        self.original_shading = (bevel.mark_sharp, bevel.smooth)
        r, rv = context.region, context.region_data
        p = bevel.anchor()
        s = view3d_utils.location_3d_to_region_2d(r, rv, p) or Vector(self.hud_pos)
        a = view3d_utils.region_2d_to_location_3d(r, rv, s, p)
        b = view3d_utils.region_2d_to_location_3d(r, rv, s + Vector((1, 0)), p)
        self.px = max((b - a).length, 1e-6)
        self.w0 = self.width
        self._start_modal(context,
                          "Edge Bevel (edit): move left / right: width  |  Wheel: segments  |  E: ends  |  S: sharp  |  R: round  |  "
                          "X / Delete: delete  |  Click / Enter: confirm  |  Esc / Right click: cancel")
        return {'RUNNING_MODAL'}

    def _collect(self):
        edges = []
        for e, face in self.picked:
            for x in (self.evaluated.loop(e, face) if self.use_loop else self.evaluated.line(e)):
                if x not in edges:
                    edges.append(x)
        return edges

    def _rebuild(self, context):
        """(Re)create the cutters: one per picked edge / loop (split only where a loop
        goes from convex to concave), mitered at the corners."""
        for bevel in self.bevels:
            bevel.remove()
        self.bevels = []
        self.edges = self._collect()
        for e, face in self.picked:
            guide = face.normal if face in e.link_faces else None
            # Loop: if it can't be built cleanly, retry only on the flat surface, then
            # settle for the single (straight) edge.
            attempts = ['loop', 'strict', 'line'] if self.use_loop else ['line']
            for attempt in attempts:
                if attempt == 'line':
                    picked_edges = self.evaluated.line(e)
                else:
                    picked_edges = self.evaluated.loop(e, face, attempt == 'strict')
                picked_edges = [x for x in picked_edges if self.evaluated.can_bevel(x)]
                made, bad = [], False
                for sweep in build_sweeps(self.evaluated, picked_edges, guide):
                    bevel = BoolBevel(context, self.obj, sweep, self.width, self.segments,
                                      self.end_mode, self.before_mirror, self.sharp_mode, self.smooth_mode)
                    if bevel.valid:
                        made.append(bevel)
                    else:  # would wreck the mesh: leave this part out
                        bevel.remove()
                        bad = True
                if bad and attempt != attempts[-1]:
                    for bevel in made:
                        bevel.remove()
                    continue
                if attempt == 'line' and self.use_loop:
                    self.loop_fallback = True
                self.skipped = self.skipped or bad
                self.bevels += made
                break
        return bool(self.bevels)

    def _update(self):
        for bevel in self.bevels:
            old = (bevel.width, bevel.segments, bevel.ends)
            bevel.update(self.width, self.segments, self.end_mode)
            if not bevel.valid:  # too wide for this geometry: keep the last good shape
                bevel.update(*old)
                self.width, self.segments, self.end_mode = old

    def _end(self, context):
        _state["modal"] = False
        stale = _cache.pop(self.obj.name, None)  # the object just got new modifiers
        if stale is not None:
            stale.free()
        bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
        context.workspace.status_text_set(None)
        context.area.tag_redraw()
        if self.evaluated is not None:
            self.evaluated.free()
        self.obj.select_set(True)
        context.view_layer.objects.active = self.obj

    def modal(self, context, event):
        context.area.tag_redraw()
        if event.type == 'MOUSEMOVE':
            self.hud_pos = (event.mouse_region_x, event.mouse_region_y)
            self.width = max(self.px, self.w0 + (event.mouse_region_x - self.x0) * self.px)
            self._update()
        elif event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS', 'PAGE_UP'} and event.value == 'PRESS':
            self.segments = min(self.segments + 1, 64)
            self._update()
        elif event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS', 'PAGE_DOWN'} and event.value == 'PRESS':
            self.segments = max(self.segments - 1, 1)
            self._update()
        elif event.type in {'S', 'R'} and event.value == 'PRESS':
            if event.type == 'S':
                self.sharp_mode = not self.sharp_mode
            else:
                self.smooth_mode = not self.smooth_mode
            for bevel in self.bevels:
                bevel.set_shading(self.sharp_mode, self.smooth_mode)
        elif event.type == 'E' and event.value == 'PRESS':
            ids = [e[0] for e in ENDS]
            self.end_mode = ids[(ids.index(self.end_mode) + 1) % len(ids)]
            self._update()
        elif self.editing and event.type in {'X', 'DEL'} and event.value == 'PRESS':
            self.bevels[0].remove()
            self._end(context)
            self.report({'INFO'}, "Edge Bevel deleted")
            return {'FINISHED'}
        elif self.editing and event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self.bevels[0].set_shading(*self.original_shading)
            self.bevels[0].update(*self.original)  # back to how it was
            self._end(context)
            return {'CANCELLED'}
        elif self.editing and event.type in {'LEFT_ALT', 'RIGHT_ALT'} or \
                (self.editing and event.type == 'LEFTMOUSE' and event.shift):
            pass  # no edge picking while editing
        elif event.type in {'LEFT_ALT', 'RIGHT_ALT'} and event.value == 'PRESS':
            self.use_loop = not self.use_loop
            if not self._rebuild(context):  # nothing convex in that mode: go back
                self.use_loop = not self.use_loop
                self._rebuild(context)
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS' and event.shift:
            picked = self.evaluated.pick(context.region, context.region_data,
                                         (event.mouse_region_x, event.mouse_region_y))
            if picked is not None and picked[0] not in [p[0] for p in self.picked]:
                self.picked.append(picked)
                self._rebuild(context)
        elif (event.type == 'LEFTMOUSE' and event.value == 'PRESS') or \
                (event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS'):
            self._end(context)
            if self._was_manifold and not _is_manifold(context, self.obj):
                self.report({'WARNING'}, "Edge Bevel: the result has open / broken geometry here. "
                                         "Try another width, Ends: Extend, or a single edge (Ctrl Z to undo)")
            return {'FINISHED'}
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            for bevel in self.bevels:
                bevel.remove()
            self._end(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class GTOOLS_OT_edge_bevel_delete(bpy.types.Operator):
    """Delete the edge bevel under the mouse"""
    bl_idname = "view3d.gtools_edge_bevel_delete"
    bl_label = "Delete Edge Bevel"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and context.mode == 'OBJECT'

    def invoke(self, context, event):
        mouse = (event.mouse_region_x, event.mouse_region_y)
        obj = _object_under_mouse(context, mouse)
        bevel = None
        if obj is not None:
            location, _face = _cached(context, obj).ray(context.region, context.region_data, mouse)
            bevel = bevel_at(obj, location) if location is not None else None
        if bevel is None:
            return {'PASS_THROUGH'}  # not over a bevel: X keeps doing what it normally does
        bevel.remove()
        _hover.clear()
        stale = _cache.pop(obj.name, None)
        if stale is not None:
            stale.free()
        context.area.tag_redraw()
        self.report({'INFO'}, "Edge Bevel deleted")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Toolbar tool
# ---------------------------------------------------------------------------

class GTOOLS_TL_edge_bevel(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "gtools.edge_bevel"
    bl_label = "Edge Bevel"
    bl_description = (
        "Hover to preview: every sharp edge is outlined and the edge a click would bevel is highlighted.\n"
        "Click a sharp edge to bevel it with a boolean cutter (non-destructive, cuts through n-gons).\n"
        "Move the mouse for the width, wheel for segments (1 = chamfer), Alt for the whole loop,\n"
        "Shift click to add edges.\n"
        "Over the faces of an earlier bevel it turns orange: click to edit it, X / Delete to remove it.\n"
        "To bevel on top of it, hover the face next to it or right on its edge"
    )
    bl_icon = "ops.mesh.bevel"

    @staticmethod
    def draw_settings(context, layout, tool):
        props = tool.operator_properties("view3d.gtools_edge_bevel")
        layout.prop(props, "ends", expand=True)
        layout.prop(props, "mark_sharp")
        layout.prop(props, "smooth_round")
        layout.prop(props, "before_mirror")

    bl_keymap = (
        ("view3d.gtools_edge_bevel_hover", {"type": 'MOUSEMOVE', "value": 'ANY', "any": True}, None),
        ("view3d.gtools_edge_bevel_delete", {"type": 'X', "value": 'PRESS'}, None),
        ("view3d.gtools_edge_bevel_delete", {"type": 'DEL', "value": 'PRESS'}, None),
        ("view3d.gtools_edge_bevel", {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
        ("view3d.gtools_edge_bevel", {"type": 'LEFTMOUSE', "value": 'PRESS', "alt": True}, None),
        ("view3d.gtools_edge_bevel", {"type": 'LEFTMOUSE', "value": 'PRESS', "ctrl": True},
         {"properties": [("select_only", True)]}),
        ("view3d.gtools_edge_bevel", {"type": 'LEFTMOUSE', "value": 'PRESS', "ctrl": True, "shift": True},
         {"properties": [("select_only", True), ("toggle", True)]}),
    )


def register():
    bpy.utils.register_class(GTOOLS_OT_edge_bevel)
    bpy.utils.register_class(GTOOLS_OT_edge_bevel_hover)
    bpy.utils.register_class(GTOOLS_OT_edge_bevel_delete)
    bpy.utils.register_tool(GTOOLS_TL_edge_bevel, after={"gtools.smart_cut_object"}, separator=False, group=False)
    _state["handle"] = bpy.types.SpaceView3D.draw_handler_add(_draw_hover, (), 'WINDOW', 'POST_VIEW')
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister():
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _state["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_state["handle"], 'WINDOW')
        _state["handle"] = None
    _hover.clear()
    _clear_cache()
    bpy.utils.unregister_tool(GTOOLS_TL_edge_bevel)
    bpy.utils.unregister_class(GTOOLS_OT_edge_bevel_delete)
    bpy.utils.unregister_class(GTOOLS_OT_edge_bevel_hover)
    bpy.utils.unregister_class(GTOOLS_OT_edge_bevel)
