# SPDX-License-Identifier: GPL-3.0-or-later
"""Export a take as a game clip: root motion on a ground ``root`` bone.

Engines take root motion from a bone at the ground: Unreal's root-motion
settings and Force Root Lock, Unity's Bake Into Pose, Godot's
``root_motion_track`` all read the travel off it, and play the clip in place by
ignoring it. So a game clip is one asset with two readings, and this is it:

* a ``root`` bone is added at the origin, the rig's top bone (the hips) under
  it -- or, on a rig that has one, its own root is used;
* the root carries the take's travel (inplace.py: the path the body moves
  along and the way it faces), from the origin facing forward (-Y);
* the hips carry the rest: the sway, surge and bounce of the steps, and the
  height.

Travel that In place took out is put back on the root (it was kept at
Accept); a take that travels gives its travel to the root. A loop is cut to
start on the left foot's touch-down and, when it runs within 12 degrees of a
straight, sideways or diagonal direction, turned to run exactly along it.

Exported by a background Blender on a copy of the file (the live scene is not
touched), as FBX for Unreal or Unity, or glTF for Godot and the web, with a
``.json`` beside it: foot sync markers, the distance and speed curves, the
trajectory, and motion-matching samples of it (0.33, 0.67, 1 s ahead).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator
from mathutils import Matrix

#: engine presets: file format and the settings its importer wants
ENGINES = [
    ("UNREAL", "Unreal", "FBX; the armature object named 'Armature' so Unreal adds no extra root bone"),
    ("UNITY", "Unity", "FBX with scale and axes applied (Generic or Humanoid, root motion from 'root')"),
    ("GODOT", "Godot / glTF", "glTF binary (.glb); set the AnimationTree's root_motion_track to 'root'"),
]
#: a loop within this of a straight, sideways or diagonal direction is turned onto it
SNAP_WITHIN = math.radians(12.0)
FORWARD = -math.pi / 2                 # -Y, the way a Blender character faces
FUTURE = (1 / 3, 2 / 3, 1.0)           # motion-matching trajectory samples, s ahead


def _rigid(x, y, yaw, z=0.0) -> Matrix:
    c, s = math.cos(yaw), math.sin(yaw)
    return Matrix(((c, -s, 0, x), (s, c, 0, y), (0, 0, 1, z), (0, 0, 0, 1)))


def _yaw_of(M) -> float:
    return math.atan2(M[1][0], M[0][0])


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def _is_root(name: str) -> bool:
    return name.rsplit(":", 1)[-1].lower() in {"root", "root_m", "rootbone"}


def _flat(M) -> list:
    return [float(M[i][j]) for i in range(4) for j in range(4)]


def plan(arm, action, scene, *, root_z=False, align_phase=True, snap=True) -> dict:
    """Everything the background export needs, worked out here, on the live
    take: per frame the root's pose and the carried bones' (armature space).
    Samples the take (moves the playhead, and puts it back)."""
    from . import inplace, key_poses
    rm = inplace.root_motion(arm, action, scene)
    if rm is None:
        raise RuntimeError("no take to export")
    fps = scene.render.fps / scene.render.fps_base
    frames = rm["frames"]
    n = len(frames)
    top = next(pb for pb in arm.pose.bones if pb.parent is None)
    has_root = _is_root(top.name)
    carriers = [c.name for c in top.children] if has_root else [top.name]
    A = arm.matrix_world.copy()

    # the carried bones, frame by frame, as the take has them now
    keep = scene.frame_current
    cur = {b: [] for b in carriers}
    key_poses._baking = True
    try:
        for f in frames:
            scene.frame_set(f)
            for b in carriers:
                cur[b].append(arm.pose.bones[b].matrix.copy())
    finally:
        scene.frame_set(keep)
        key_poses._baking = False

    tr = rm["travel"]
    sz = rm["support_z"] if root_z else [0.0] * n
    Rp = [_rigid(x, y, yaw) for x, y, yaw in tr]                     # the travel on the floor
    Rt = [_rigid(x, y, yaw, z) for (x, y, yaw), z in zip(tr, sz)]    # ...and what the root carries
    H_inv = _rigid(rm["hold"][0], rm["hold"][1], 0.0).inverted()
    # the take as it travels, in the world: put back what In place took out
    # (never a height: that stayed in the hips)
    W = {b: [(Rp[k] @ H_inv if rm["in_place"] else Matrix.Identity(4)) @ A @ cur[b][k] for k in range(n)]
         for b in carriers}

    # a loop: start on the left foot's touch-down, the cycle's travel added past its end
    loop = bool(rm["loop"]) and n > 3
    shift = 0
    markers = {k: [f - frames[0] for f in v] for k, v in rm["markers"].items()}
    if loop and align_phase:
        m = n - 1
        left = [k for k in markers.get("LeftFootDown", []) if 0 < k < m]
        if left and 0 not in markers.get("LeftFootDown", []):
            shift = left[0]
            cyc = Rp[m] @ Rp[0].inverted()
            Rt = (Rt + [cyc @ r for r in Rt[1:]])[shift:shift + n]
            for b in carriers:
                W[b] = (W[b] + [cyc @ w for w in W[b][1:]])[shift:shift + n]
            markers = {k: sorted({(f - shift) % m for f in v}) for k, v in markers.items()}

    # normalise: the clip starts at the origin, on the floor, facing forward (-Y);
    # a loop's run is turned exactly onto the nearest straight/diagonal direction
    facing0 = rm["facing0"]
    f0 = (facing0 + (_yaw_of(Rt[0]) - tr[0][2])) if facing0 is not None else FORWARD
    phi = FORWARD - f0
    snapped = None
    if loop and snap:
        d = Rt[-1].translation - Rt[0].translation
        if d.length > 0.05:
            run = math.atan2(d.y, d.x)
            rel = _wrap(run - f0)
            nominal = round(rel / (math.pi / 4)) * (math.pi / 4)
            if abs(rel - nominal) <= SNAP_WITHIN:
                phi = FORWARD + nominal - run
                snapped = round(math.degrees(nominal))
    floor = rm["floor"] if abs(rm["floor"]) > 0.02 else 0.0
    p0 = Rt[0].translation
    N = Matrix.Rotation(phi, 4, 'Z') @ Matrix.Translation((-p0.x, -p0.y, -floor))
    R0i = Rt[0].inverted()
    root_w = [N @ r @ R0i @ N.inverted() for r in Rt]           # the root, in the world
    # the exported armature sits at the origin (its own turn and scale kept)
    Ae = A.copy()
    Ae.translation = (0.0, 0.0, 0.0)
    Aei = Ae.inverted()
    root_arm = [Aei @ r @ Ae for r in root_w]
    root_rest = top.bone.matrix_local.copy() if has_root else Matrix.Identity(4)
    root_pose = [r @ root_rest for r in root_arm]               # armature space
    basis = {b: [] for b in carriers}
    for b in carriers:
        rel_rest = root_rest.inverted() @ arm.data.bones[b].matrix_local
        for k in range(n):
            q_e = Aei @ N @ W[b][k]
            basis[b].append(_flat(rel_rest.inverted() @ root_pose[k].inverted() @ q_e))
    root_basis = [_flat(root_rest.inverted() @ p) for p in root_pose]

    # the curves and the trajectory a game reads, in the clip's own frame (m, s, +Z up, -Y forward)
    traj = [[r.translation.x, r.translation.y, r.translation.z, _yaw_of(r)] for r in root_w]
    for k in range(1, n):                                        # an unwrapped heading
        while traj[k][3] - traj[k - 1][3] > math.pi:
            traj[k][3] -= 2 * math.pi
        while traj[k][3] - traj[k - 1][3] < -math.pi:
            traj[k][3] += 2 * math.pi
    dist, speed = [0.0], []
    for k in range(1, n):
        dist.append(dist[-1] + math.hypot(traj[k][0] - traj[k - 1][0], traj[k][1] - traj[k - 1][1]))
    for k in range(n):
        a, b = max(k - 1, 0), min(k + 1, n - 1)
        speed.append((dist[b] - dist[a]) * fps / max(b - a, 1))
    cyc_w = root_w[-1] @ root_w[0].inverted()

    def ahead(k, dt):
        j = k + dt * fps
        lap = Matrix.Identity(4)
        if loop:
            while j > n - 1:
                j -= n - 1
                lap = lap @ cyc_w
        j = min(j, n - 1)
        i = int(j)
        M = root_w[i] if i == j or i + 1 >= n else root_w[i].lerp(root_w[i + 1], j - i)
        rel = root_w[k].inverted() @ lap @ M
        yaw = _yaw_of(rel)
        return [round(rel.translation.x, 4), round(rel.translation.y, 4),
                round(math.cos(yaw + FORWARD), 4), round(math.sin(yaw + FORWARD), 4)]

    if loop and not shift and 0 in markers.get("LeftFootDown", []):
        starts_on = "LeftFootDown"
    else:
        starts_on = "LeftFootDown" if shift else None
    meta = {
        "fps": fps, "frame_count": n, "loop": loop, "root_bone": top.name if has_root else "root",
        "axes": {"up": "+Z", "forward": "-Y", "units": "m", "note": "Blender axes; the file carries its engine's"},
        "phase": {"starts_on": starts_on, "shifted_by_frames": shift},
        "direction_snapped_deg": snapped,
        "root_motion": {"distance_m": round(dist[-1], 4), "duration_s": round((n - 1) / fps, 4),
                        "average_speed_mps": round(dist[-1] / max((n - 1) / fps, 1e-6), 4),
                        "turn_deg": round(math.degrees(traj[-1][3] - traj[0][3]), 2),
                        "spans": rm["spans"]},
        "markers": [{"name": k, "frame": f, "time_s": round(f / fps, 4)} for k, v in markers.items() for f in v],
        "curves": {"distance_m": [round(v, 4) for v in dist], "speed_mps": [round(v, 4) for v in speed]},
        "trajectory": [[round(v, 5) for v in t] for t in traj],
        "motion_matching": {"ahead_s": list(FUTURE),
                            "note": "per frame, per sample: [x, y] in the root's frame, facing [dx, dy]",
                            "samples": [[ahead(k, dt) for dt in FUTURE] for k in range(n)]},
    }
    return {"armature": arm.name, "action": action.name, "frames": frames, "shift": shift,
            "loop": loop, "add_root": not has_root, "root": top.name if has_root else "root",
            "top": top.name, "carriers": carriers, "root_basis": root_basis, "basis": basis,
            "armature_matrix": _flat(Ae), "meta": meta}


def run(job: dict, blend: str, timeout: float = 600.0) -> list[str]:
    """Export in a background Blender on *blend*; returns the files written."""
    script = os.path.join(os.path.dirname(__file__), "game_export_job.py")
    fd, job_path = tempfile.mkstemp(suffix=".json", prefix="animatica_export_")
    with os.fdopen(fd, "w") as fh:
        json.dump(job, fh)
    try:
        proc = subprocess.run([bpy.app.binary_path, "-b", "--factory-startup", blend, "--python", script,
                               "--", job_path], capture_output=True, text=True, timeout=timeout)
    finally:
        os.unlink(job_path)
    out = [ln.split(" ", 1)[1].strip() for ln in proc.stdout.splitlines() if ln.startswith("EXPORTED ")]
    if proc.returncode != 0 or not out:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-12:])
        raise RuntimeError(f"export failed:\n{tail}")
    return out


def _export_action(arm):
    ad = arm.animation_data
    if ad is not None and ad.action is not None:
        return ad.action
    if ad is not None:        # accepted: the last strip on the Animatica track
        for track in reversed(ad.nla_tracks):
            if track.strips:
                return track.strips[-1].action
    return None


class ANIMATICA_OT_export_game_clip(Operator):
    """Export the take as a game clip: root motion on a ground root bone"""
    bl_idname = "animatica.export_game_clip"
    bl_label = "Export Game Clip"
    bl_description = (
        "Export the take for a game engine: a ground 'root' bone carries its "
        "travel (from the origin, facing forward), the hips the rest. One "
        "clip plays with root motion or in place; a .json beside it has the "
        "foot sync markers, distance and speed curves and the trajectory"
    )
    bl_options = {'REGISTER'}

    engine: EnumProperty(name="Engine", items=ENGINES, default="UNREAL")
    variants: EnumProperty(
        name="Clips",
        items=[("BOTH", "Root Motion + In Place", "Two files: the root travelling, and held at the origin"),
               ("ROOT_MOTION", "Root Motion", "The root carries the travel"),
               ("IN_PLACE", "In Place", "The root stays at the origin (same hips as root motion)")],
        default="BOTH",
    )
    directory: StringProperty(name="Folder", subtype='DIR_PATH', default="//game_export/")
    clip_name: StringProperty(name="Name", description="File name; the take's name if empty", default="")
    align_phase: BoolProperty(name="Start Loops on Left Foot", default=True,
                              description="Cut a loop to start where the left foot comes down, "
                                          "so walk, jog and run cycles line up")
    snap_direction: BoolProperty(name="Snap Loop Direction", default=True,
                                 description="Turn a loop running within 12° of straight, sideways or "
                                             "diagonal onto exactly that direction")
    root_z: BoolProperty(name="Root Height", default=False,
                         description="The root also carries the height of the ground under the feet "
                                     "(stairs, ledges); off, it stays on the floor")

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "animatica", None)
        arm = getattr(s, "target_armature", None)
        return arm is not None and arm.type == 'ARMATURE' and _export_action(arm) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def execute(self, context):
        s = context.scene.animatica
        arm = s.target_armature
        action = _export_action(arm)
        ad = arm.animation_data
        was = ad.action
        try:
            if ad.action is None:          # an accepted take plays from the NLA: read it as the action
                ad.action = action
            job = plan(arm, action, context.scene, root_z=self.root_z,
                       align_phase=self.align_phase, snap=self.snap_direction)
        except Exception as exc:           # noqa: BLE001
            self.report({'ERROR'}, f"Export: {exc}")
            return {'CANCELLED'}
        finally:
            if ad.action != was:
                ad.action = was
        folder = bpy.path.abspath(self.directory)
        os.makedirs(folder, exist_ok=True)
        name = bpy.path.clean_name(self.clip_name or action.name)
        job.update({"engine": self.engine, "variants": self.variants, "folder": folder, "name": name})
        tmp = os.path.join(tempfile.mkdtemp(prefix="animatica_export_"), "take.blend")
        bpy.ops.wm.save_as_mainfile(filepath=tmp, copy=True, check_existing=False)
        try:
            files = run(job, tmp)
        except Exception as exc:           # noqa: BLE001
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            try:
                os.unlink(tmp)
                os.rmdir(os.path.dirname(tmp))
            except OSError:
                pass
        self.report({'INFO'}, "Exported " + ", ".join(os.path.basename(f) for f in files))
        return {'FINISHED'}


_classes = (ANIMATICA_OT_export_game_clip,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
