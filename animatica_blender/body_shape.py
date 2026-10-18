"""The character's body as spheres inside its skin, sent with Paste Motion so the server can keep
its limbs out of it.

A retarget copies the source's limb directions; on a body built differently (a belly, a cartoon's
round torso and short arms) a hand the source rested on its hip goes into the target's. The server
has the skeleton, not the mesh, so the request carries the body's shape: spheres set inside the
skinned surface in the rest pose, each carried by a joint the request sends (its centre from that
joint, MMCP frame, metres). Inside the surface, they under-fill the body: two of them overlapping
is a limb really inside the body, never a near miss.

How they are placed, per joint:

* the joint's vertices are those its deform bone (or a deform bone the request folds into it, as
  Rigify's tweak halves) weighs on most;
* candidate centres lie along the bone and, across it, as far out as its vertices reach (a torso is
  wider than deep, so spheres on its axis alone would leave its sides empty);
* a candidate counts if it is inside the skin (behind the face nearest to it) and its sphere
  reaches to the nearest skin vertex less a margin;
* the biggest are kept, each clear of the centres of those kept before it, a few per joint.

``body_spheres`` returns ``[]`` for a character without a skinned mesh; the request then goes
without a body and the server transfers the motion as before.
"""
from __future__ import annotations

from typing import Any

import bpy
import numpy as np
from mathutils import Vector, kdtree
from mathutils.bvhtree import BVHTree

from . import coords, request_builder

#: spheres kept per joint at most
PER_JOINT = 12
#: smallest sphere worth sending (m)
MIN_RADIUS = 0.012


def skinned_meshes(armature_obj) -> list:
    return [o for o in bpy.data.objects if o.type == 'MESH' and o.visible_get()
            and any(m.type == 'ARMATURE' and m.object is armature_obj and m.show_viewport for m in o.modifiers)]


def _rest_vertices(armature_obj, meshes, with_bvh: bool = False):
    """Each mesh's vertices in the rest pose, world space; with ``with_bvh``, also each mesh's
    BVH in the rest pose (its own space), built in C from the evaluated mesh."""
    data = armature_obj.data
    saved = data.pose_position
    data.pose_position = 'REST'
    try:
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        out, bvhs = [], []
        for o in meshes:
            ev = o.evaluated_get(dg)
            me = ev.to_mesh()
            co = np.empty(len(me.vertices) * 3)
            me.vertices.foreach_get("co", co)
            ev.to_mesh_clear()
            mw = np.array(o.matrix_world)
            out.append(co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3])
            if with_bvh:
                bvhs.append(BVHTree.FromObject(o, dg))
        return (out, bvhs) if with_bvh else out
    finally:
        data.pose_position = saved
        bpy.context.view_layer.update()


def _owner_joint(armature_obj, sent: list[str]) -> dict[str, str]:
    """Each deform bone's joint in the request: itself if sent, else the nearest sent bone above
    it in the deform tree (Rigify's tweak halves, Auto-Rig Pro's second limb halves), else the
    nearest sent bone by its head."""
    sent_set = set(sent)
    deform = request_builder.detect_deform_bones(armature_obj)
    try:
        tree = request_builder._build_deform_parent_map(armature_obj, set(deform))
    except Exception:                                              # noqa: BLE001
        tree = {}
    bones = armature_obj.data.bones
    heads = {n: bones[n].head_local for n in sent if n in bones}
    out = {}
    for b in deform:
        j, seen = b, set()
        while j is not None and j not in sent_set and j not in seen:
            seen.add(j)
            j = tree.get(j)
        if j is None or j not in sent_set:
            h = bones[b].head_local
            j = min(heads, key=lambda n: (heads[n] - h).length) if heads else None
        if j is not None:
            out[b] = j
    return out


def _vertex_joints(mesh_obj, owner: dict[str, str]) -> list[str | None]:
    names = {g.index: owner.get(g.name) for g in mesh_obj.vertex_groups}
    out = []
    for v in mesh_obj.data.vertices:
        best, w = None, 0.0
        for g in v.groups:
            j = names.get(g.group)
            if j is not None and g.weight > w:
                best, w = j, g.weight
        out.append(best)
    return out


class _Inside:
    """Inside-the-skin test: the nearest skin point's face faces away from the point. The skin is
    whole here (nothing cut out of it), so its outward normals settle it; a rig made of shells
    that overlap at the joints (a robot's) only loses a candidate where two shells meet, which
    is safe. Ray parity is not: inside two overlapping shells a ray crosses two surfaces."""

    def __init__(self, meshes, bvhs):
        self.items = [(bvh, o.matrix_world, o.matrix_world.inverted()) for o, bvh in zip(meshes, bvhs)]

    def __call__(self, p) -> bool:
        p = Vector(p)
        best, inside = None, False
        for bvh, mw, inv in self.items:
            loc, nrm, _fi, _d = bvh.find_nearest(inv @ p)
            if loc is None:
                continue
            w_loc = mw @ loc
            dist = (p - w_loc).length
            if best is None or dist < best:
                w_n = (inv.transposed().to_3x3() @ nrm).normalized()
                best, inside = dist, (p - w_loc).dot(w_n) < 0
        return inside


def _margin(meshes) -> float:
    """Half the skin's median edge length (the skin between vertices lies closer than they do),
    2 mm to 1.5 cm."""
    lens = []
    for o in meshes:
        me = o.data
        if not len(me.edges):
            continue
        co = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        ed = np.empty(len(me.edges) * 2, dtype=np.int64)
        me.edges.foreach_get("vertices", ed)
        ed = ed.reshape(-1, 2)[:: max(1, len(me.edges) // 4000)]
        scale = np.mean(np.abs(np.array(o.matrix_world.to_scale())))
        lens.append(np.linalg.norm(co[ed[:, 0]] - co[ed[:, 1]], axis=1) * scale)
    if not lens:
        return 0.005
    return float(np.clip(0.5 * np.median(np.concatenate(lens)), 0.002, 0.015))


def body_spheres(armature_obj, skeleton: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Spheres inside ``armature_obj``'s skin, each on a joint of ``skeleton`` (the request's; built
    if not given): ``[{"joint", "center", "radius"}]``, the centre from the joint in the rest pose,
    MMCP frame. ``[]`` without a skinned mesh."""
    meshes = skinned_meshes(armature_obj)
    if not meshes:
        return []
    if skeleton is None:
        skeleton = request_builder.armature_to_skeleton(armature_obj)
    sent = [j["name"] for j in skeleton["joints"]]
    owner = _owner_joint(armature_obj, sent)
    verts, bvhs = _rest_vertices(armature_obj, meshes, with_bvh=True)
    allv = np.concatenate(verts)
    tree = kdtree.KDTree(len(allv))
    for i, p in enumerate(allv.tolist()):
        tree.insert(p, i)
    tree.balance()
    inside = _Inside(meshes, bvhs)
    margin = _margin(meshes)
    per_joint: dict[str, list[np.ndarray]] = {}
    for o, co in zip(meshes, verts):
        labels = np.array([j or "" for j in _vertex_joints(o, owner)], dtype=object)
        for j in set(labels.tolist()) - {""}:
            per_joint.setdefault(j, []).append(co[labels == j])

    mw = armature_obj.matrix_world
    bones = armature_obj.data.bones
    out = []
    for j in sent:
        if j not in bones or j not in per_joint:
            continue
        pts = np.concatenate(per_joint[j])
        if len(pts) < 8:
            continue
        head = np.array(mw @ bones[j].head_local)
        tail = np.array(mw @ bones[j].tail_local)
        axis = tail - head
        length = float(np.linalg.norm(axis))
        if length < 1e-4:
            continue
        u = axis / length
        # how far the joint's vertices reach along the bone and across it
        rel = pts - head
        along = rel @ u
        lo, hi = np.percentile(along, 5), np.percentile(along, 95)
        across = rel - np.outer(along, u)
        cov = across.T @ across / len(across)
        w, vecs = np.linalg.eigh(cov)
        side1, side2 = vecs[:, 2], vecs[:, 1]
        reach1 = float(np.percentile(np.abs(across @ side1), 80))
        reach2 = float(np.percentile(np.abs(across @ side2), 80))
        n = int(np.clip(np.ceil((hi - lo) / max(0.05, 0.5 * min(reach1, reach2) + 1e-6)), 2, 8))
        # along the bone, and across it in eight directions at two depths (a torso is wider
        # than deep: its sides need spheres of their own)
        cands = []
        ring = [(np.cos(a), np.sin(a)) for a in np.linspace(0, 2 * np.pi, 8, endpoint=False)]
        for t in np.linspace(lo, hi, n):
            c = head + u * t
            cands.append(c)
            for f in (0.35, 0.7):
                for ca, sa in ring:
                    off = side1 * ca * reach1 + side2 * sa * reach2
                    if np.linalg.norm(off) * f > 0.02:
                        cands.append(c + off * f)
        # biggest first; the (costly) inside test only for those not already covered
        found = []
        for c in cands:
            _co, _i, d = tree.find(c)
            if d - margin >= MIN_RADIUS:
                found.append((d - margin, c))
        found.sort(key=lambda rc: -rc[0])
        kept: list[tuple[float, np.ndarray]] = []
        for r, c in found:
            if all(np.linalg.norm(c - kc) > 0.5 * max(r, kr) for kr, kc in kept) and inside(c):
                kept.append((r, c))
                if len(kept) >= PER_JOINT:
                    break
        for r, c in kept:
            off = coords.blender_pos_to_mmcp(c - head)
            out.append({"joint": j, "center": [round(float(v), 5) for v in off], "radius": round(float(r), 5)})
    return out
