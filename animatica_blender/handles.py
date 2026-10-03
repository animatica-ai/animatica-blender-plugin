# SPDX-License-Identifier: GPL-3.0-or-later
"""The Autopose tool: handles on the character, with no bones added to the rig.

The Autoposer used to build a control rig -- non-deforming bones in the
artist's armature, made in Edit Mode. That is what a rigger does not want an
add-on doing, and what a rig linked from another file (or a library override
of one, the way characters arrive in a production) does not allow at all.

Here the handles are the add-on's own: drawn over the joints they steer,
kept on the scene (which is always local, whoever owns the rig), and solved
from the pose the rig has *now*. A drag is a tool in the left toolbar, as
Blender's own posing is:

    press   the pose as it stands: where every switched-on handle's joint is,
            and its turn if it keeps it
    drag    that one handle moves, the others stay pinned, the poser solves
            the body between them -- live, on the deform bones
    release Auto Keying on: the pose is keyed here. Off: it stays as posed,
            as a pose made with G or R does, until the frame changes

One drag is one undo step, and the rig is never touched outside it.

Handle state -- on or off, its slack, whether it keeps its turn, picked or
not, and where a Look-at aims -- is per armature, by name.
"""

from __future__ import annotations

import math
import os

import bpy
import gpu
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import ui_style as st

TOOL_ID = "animatica.autopose"
#: what Blender's own tool was before the Autopose tool, to go back to
FALLBACK_TOOL = "builtin.select_box"

#: handles the tool starts with on a rig it has not seen (the rest can be added)
DEFAULTS = ("C_cog_CTRL", "L_arm_IK_CTRL", "R_arm_IK_CTRL", "L_foot_IK_CTRL", "R_foot_IK_CTRL",
            "C_head_AIM_CTRL")
#: in front of the head, where a Look-at starts (metres)
AIM_OFFSET = 0.45
#: tolerance of the joints held still while another is dragged (metres): they
#: were just where the artist left them
HOLD_TOL = 0.005
#: how loosely the hips keep the way they face while another handle is dragged
FACING_TOL = 0.08

MAX_PARTS = 40
#: screen radius of a handle (logical px), by kind: importance reads from size
RADIUS = {"cog": 9.0, "root": 9.0, "ik": 7.5, "aim": 6.5, "guide": 5.5, "pos": 6.0}
HIT = 4.0                    # px of slack around a handle that still grabs it


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AnimaticaHandle(bpy.types.PropertyGroup):
    """One handle of one armature. Named after the control it was in the old
    rig (``L_arm_IK_CTRL``), so the picker, the joint map and the rig
    definition keep one vocabulary."""
    arm: StringProperty()
    ap_joint: StringProperty()
    ap_kind: StringProperty()
    ap_ety: IntProperty()             # 0 position, 2 look-at
    ap_enabled: BoolProperty(default=True)
    ap_tol_m: FloatProperty(default=0.005, min=0.001, max=0.5)
    ap_rot: BoolProperty(default=False)
    ap_rot_tol_m: FloatProperty(default=0.005, min=0.001, max=0.5)
    select: BoolProperty(default=False)
    aim: FloatVectorProperty(size=3, subtype='TRANSLATION')
    aim_set: BoolProperty(default=False)
    aim_frame: IntProperty(default=-1)    # the frame the Look-at was placed on


class Ref:
    """A handle, shaped like the old control bone the picker was written for:
    ``ref.name``, ``ref.bone.ap_enabled``, ``ref.bone.get("ap_kind")``."""
    __slots__ = ("item",)

    def __init__(self, item):
        object.__setattr__(self, "item", item)

    @property
    def name(self):
        return self.item.name

    @property
    def bone(self):
        return self

    def get(self, key, default=None):
        return {"ap_kind": self.item.ap_kind, "ap_ety": self.item.ap_ety,
                "ap_joint": self.item.ap_joint}.get(key, default)

    def __getattr__(self, key):
        return getattr(object.__getattribute__(self, "item"), key)

    def __setattr__(self, key, value):
        setattr(self.item, key, value)

    def __eq__(self, other):
        return isinstance(other, Ref) and other.item.name == self.item.name \
            and other.item.arm == self.item.arm

    def __hash__(self):
        return hash((self.item.arm, self.item.name))


def _store(scene):
    return getattr(scene, "animatica_handles", None)


def items(scene, arm) -> list:
    """The handles of ``arm``, in the rig definition's order. Read-only: safe
    in a draw (see :func:`ensure` for the first time)."""
    store = _store(scene)
    if store is None or arm is None:
        return []
    return [h for h in store if h.arm == arm.name]


def refs(scene, arm) -> list:
    return [Ref(h) for h in items(scene, arm)]


def ref(scene, arm, name):
    return next((r for r in refs(scene, arm) if r.name == name), None)


def has(scene, arm) -> bool:
    return bool(items(scene, arm))


def specs(arm) -> list:
    """Every handle the rig definition has that this rig has a joint for."""
    from .autoposer import poser
    return [sp for sp in poser.rig_def() if poser.joint_bone(arm, sp["joint"]) is not None]


def add(scene, arm, name):
    """Add the handle ``name`` (a rig definition control) to ``arm``; it
    starts on its joint, so nothing moves until it is dragged."""
    if any(h.name == name for h in items(scene, arm)):
        return None
    spec = next((sp for sp in specs(arm) if sp["name"] == name), None)
    if spec is None:
        return None
    h = _store(scene).add()
    h.name, h.arm = name, arm.name
    h.ap_joint, h.ap_kind, h.ap_ety = spec["joint"], spec["kind"], int(spec["ety"])
    h.ap_tol_m, h.ap_rot_tol_m = float(spec["tol"]), float(spec["rot_tol"])
    if h.ap_ety == 2:
        _seed_aim(arm, h)
    return h


def remove(scene, arm, names) -> int:
    store = _store(scene)
    gone = 0
    for i in reversed(range(len(store))):
        if store[i].arm == arm.name and store[i].name in names:
            store.remove(i)
            gone += 1
    return gone


def ensure(scene, arm) -> list:
    """The handles of ``arm``, the default set if it has none yet."""
    if not items(scene, arm):
        for name in DEFAULTS:
            add(scene, arm, name)
    return items(scene, arm)


# ---------------------------------------------------------------------------
# Where a handle is
# ---------------------------------------------------------------------------

def _joint_pb(arm, joint):
    from .autoposer import poser
    return poser.joint_pose_bone(arm, joint)


def _seed_point(arm, h) -> Vector | None:
    """Where a Look-at starts: in front of the joint, the way it faces as posed."""
    from .autoposer import poser
    pb = _joint_pb(arm, h.ap_joint)
    if pb is None:
        return None
    local = (pb.matrix.translation
             + (AIM_OFFSET / max(poser.unit_scale(arm), 1e-6)) * poser._aim_dir(arm, h.ap_joint).normalized())
    return arm.matrix_world @ local


def _seed_aim(arm, h, frame=None):
    p = _seed_point(arm, h)
    if p is None:
        return
    h.aim = p
    h.aim_set = True
    h.aim_frame = int(bpy.context.scene.frame_current if frame is None else frame)


def _aim_stale(h, frame=None) -> bool:
    """A Look-at placed on another frame: there the head looked somewhere else.
    Kept, it turned the head to a point left behind -- 10 degrees of neck and
    a dragged hand 30 degrees off -- so the pose on this frame starts from
    where the head looks on it."""
    return h.aim_frame != int(bpy.context.scene.frame_current if frame is None else frame)


def world(arm, h) -> Vector | None:
    """Where the handle is drawn and grabbed, in world space: on its joint,
    or the point a Look-at aims at."""
    if h.ap_ety == 2:
        if not h.aim_set:
            return None
        if _aim_stale(h):
            return _seed_point(arm, h) or Vector(h.aim)
        return Vector(h.aim)
    pb = _joint_pb(arm, h.ap_joint)
    if pb is None:
        return None
    if h.ap_kind == "root":
        # on the ground under the hips
        p = arm.matrix_world @ pb.matrix.translation
        return Vector((p.x, p.y, arm.matrix_world.translation.z))
    return arm.matrix_world @ pb.matrix.translation


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------

def snapshot(arm, hs, frame=None) -> dict:
    """The pose as it stands, for the handles: each joint's position (world)
    and turn (armature space), and the deform bones' bases to go back to.
    ``frame``: the one the rig is posed as (the current one by default); a
    Look-at placed on another starts again in front of the head."""
    for h in hs:
        if h.ap_ety == 2 and h.aim_set and _aim_stale(h, frame):
            _seed_aim(arm, h, frame)
    joints = {}
    for h in hs:
        pb = _joint_pb(arm, h.ap_joint)
        if pb is not None:
            joints[h.ap_joint] = (arm.matrix_world @ pb.matrix.translation, pb.matrix.to_3x3())
    bases = {pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones}
    return {"joints": joints, "bases": bases,
            "aims": {h.name: Vector(h.aim) for h in hs if h.ap_ety == 2 and h.aim_set}}


def restore(arm, snap) -> None:
    for name, basis in snap["bases"].items():
        pb = arm.pose.bones.get(name)
        if pb is not None:
            pb.matrix_basis = basis


def effectors(arm, hs, snap, dragged=None, target=None, *, whole=False, targets=None,
              rot_over=None) -> list:
    """The switched-on handles as the poser's effectors: each where its joint
    was at the press, the dragged one at ``target`` (world) -- or, ``whole``,
    every one moved by the same amount, the body carried as it is.

    ``targets`` moves several at once (G on the picked handles), by name, to
    world positions; ``rot_over`` turns joints (R), by handle name, to an
    armature-space orientation the joint then keeps."""
    from .autoposer import poser
    inv = arm.matrix_world.inverted()
    targets = dict(targets or {})
    rot_over = rot_over or {}
    delta = None
    if dragged is not None and target is not None:
        if whole:
            start = snap["aims"].get(dragged.name) if dragged.ap_ety == 2 else \
                (snap["joints"].get(dragged.ap_joint) or (None,))[0]
            delta = (target - start) if start is not None else None
        else:
            targets[dragged.name] = target
    picked = {}
    for h in hs:
        moved = h.name in targets
        turned = h.name in rot_over
        if not h.ap_enabled and not moved and not turned:
            continue
        if h.ap_ety == 2:
            p = snap["aims"].get(h.name)
            if p is None:
                continue
            if moved:
                p = targets[h.name]
            elif delta is not None:
                p = p + delta
            q = poser.to_poser(arm, inv @ p)
            picked[(h.ap_joint, 2)] = (0.0, {"joint": h.ap_joint, "type": "lookat",
                                             "pos": [q.x, q.y, q.z]})
            continue
        j = snap["joints"].get(h.ap_joint)
        if j is None:
            continue
        p, R = j
        if moved:
            t = targets[h.name]
            p = Vector((t.x, t.y, p.z)) if h.ap_kind == "root" else t   # the root slides on the ground
        elif delta is not None:
            p = p + delta
        tol = float(h.ap_tol_m)
        key = (h.ap_joint, 0)
        if key not in picked or picked[key][0] > tol:      # root and cog both drive the hips
            q = poser.to_poser(arm, inv @ p)
            picked[key] = (tol, {"joint": h.ap_joint, "type": "pos", "pos": [q.x, q.y, q.z],
                                 "tol": tol})
        if h.ap_kind in ("cog", "root") and not (h.ap_rot or turned):
            # the body keeps the way it faces, loosely: with only the hips' place
            # held, the poser was free to answer with a body turned half round
            R3 = poser.rot_to_poser(arm, R @ poser._rest3(arm, h.ap_joint).inverted())
            rk = (h.ap_joint, 1)
            if rk not in picked:
                picked[rk] = (FACING_TOL, {"joint": h.ap_joint, "type": "rot", "tol": FACING_TOL,
                                           "rot": [R3[0][0], R3[1][0], R3[2][0],
                                                   R3[0][1], R3[1][1], R3[2][1]]})
        if h.ap_rot or turned:
            R = rot_over.get(h.name, R)
            rtol = float(h.ap_rot_tol_m)
            R3 = poser.rot_to_poser(arm, R @ poser._rest3(arm, h.ap_joint).inverted())
            rk = (h.ap_joint, 1)
            if rk not in picked or picked[rk][0] > rtol:
                picked[rk] = (rtol, {"joint": h.ap_joint, "type": "rot", "tol": rtol,
                                     "rot": [R3[0][0], R3[1][0], R3[2][0],
                                             R3[0][1], R3[1][1], R3[2][1]]})
    return [e for _t, e in picked.values()]


def solve(context, arm, eff, floor_below=None) -> str:
    """Solve the body for ``eff`` and put it on the rig's deform bones.
    '' when it worked, else why not, in a sentence."""
    from .autoposer import engine, poser
    n_pos = sum(1 for e in eff if e["type"] == "pos")
    if n_pos == 0:
        return "Switch on a handle first"
    try:
        eng = engine.get()
    except engine.NotReady as e:
        return str(e)
    scene = context.scene
    floor = bool(getattr(scene, "ap_floor", True))
    try:
        out = poser.pose_on_ground(eng, arm, eff, bone_lengths=poser._bone_lengths(arm),
                                   ik_refine=bool(getattr(scene, "ap_use_ik", True)),
                                   floor=floor, floor_joints="feet", hard_floor=floor,
                                   toe_roll=floor, toe_tips=poser.toe_tips(arm) if floor else None,
                                   floor_below=floor_below)
    except engine.NotReady as e:
        return str(e)
    except Exception as e:                                  # noqa: BLE001
        return f"The solve failed: {e}"
    poser._apply(arm, out["names"], out["joints"], out["rotations_6d"],
                 poser._pins(arm, eff, out, floor))
    context.view_layer.update()
    return ""


def floor_cap(arm, snap):
    """The highest the floor may be for a pose (poser Y): its lowest joint,
    a little under it -- the floor holds a pose up, it never pushes it."""
    from .autoposer import poser
    pts = [p for p, _R in snap["joints"].values()]
    if not pts:
        return None
    inv = arm.matrix_world.inverted()
    low = min(pts, key=lambda v: v.z)
    return poser.to_poser(arm, inv @ low).y - 0.03


def resolve(context, arm) -> str:
    """Solve the pose as it stands -- after a handle was switched off, or its
    slack changed."""
    hs = items(context.scene, arm)
    return solve(context, arm, effectors(arm, hs, snapshot(arm, hs)))


def key_pose(context, arm) -> int:
    """Key an edit at this frame (Auto Keying): into the take where there is
    one, as a key pose where there is not."""
    from . import key_poses, pose_edit
    action = pose_edit._editing_action(arm)
    if action is None:
        if arm.animation_data is None:
            arm.animation_data_create()
        action = bpy.data.actions.new(f"{arm.name}Action")
        arm.animation_data.action = action
    frame = int(context.scene.frame_current)
    n = pose_edit.write_channels(action, frame, pose_edit.pose_channels(arm),
                                 pose_edit.edit_key_type(arm, frame))
    key_poses.invalidate_plan()
    key_poses.flash_keyed(frame)
    from .operators import keep_take
    keep_take(context)              # fine-tuning a take is keeping it
    return n


def problem(context, arm) -> str:
    """Why the tool cannot pose ``arm`` here, or ''."""
    from .autoposer import engine, poser
    if arm is None:
        return "Pick the character to pose first"
    why = poser.rig_problem(arm)
    if why and why != poser.LINKED:
        return why
    status = engine.status()
    if not (status["runtime"] and status["model"]):
        return poser.NO_MODEL
    return ""


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

def _arm(context):
    from . import properties
    s = getattr(context.scene, "animatica", None)
    return properties._live_armature(s.target_armature) if s is not None else None


def tool_active(context) -> bool:
    try:
        tool = context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
    except Exception:                                       # noqa: BLE001
        return False
    return tool is not None and tool.idname == TOOL_ID


def _u() -> float:
    return bpy.context.preferences.system.ui_scale


#: G / R under way: where, and the axis (or plane) it is locked to -- drawn as
#: Blender draws its own: a line in the axis colour through the pivot
_guide = {"center": None, "axis": ""}

#: what the mouse is over, by area, for the hover ring
_hover: dict = {}
#: the drag, for drawing the target and the label
_drag = {"active": False, "name": "", "error": ""}


def _rv3d(context):
    rv3d = getattr(context, "region_data", None)
    if rv3d is None:
        space = getattr(context, "space_data", None)
        rv3d = getattr(space, "region_3d", None)
    return rv3d


def _projected(context, arm):
    """``[(item, (x, y), radius)]`` for each handle in front of the view."""
    region, rv3d = context.region, _rv3d(context)
    if region is None or rv3d is None:
        return []
    u = _u()
    out = []
    for h in items(context.scene, arm):
        p = world(arm, h)
        if p is None:
            continue
        co = view3d_utils.location_3d_to_region_2d(region, rv3d, p)
        if co is None:
            continue
        out.append((h, (co.x, co.y), RADIUS.get(h.ap_kind, 6.0) * u))
    return out


def _circle(cx, cy, r, n=24):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
            for k in range(n)]


def _ring(cx, cy, r, w, color):
    pts = _circle(cx, cy, r)
    st.lines(list(zip(pts, pts[1:] + pts[:1])), w, color)


def _shape(h, cx, cy, r):
    """The handle's outline: a circle, a square for the body, a diamond for an aim."""
    if h.ap_kind in ("cog", "root"):
        return [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
    if h.ap_ety == 2:
        return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    return _circle(cx, cy, r)


def draw(context, highlighted=True):
    arm = _arm(context)
    if arm is None:
        return
    screen = getattr(context, "screen", None)
    if screen is not None and screen.is_animation_playing:
        return          # playing: the handles belong to the frame you stopped on
    u = _u()
    hot = _hover.get(context.area.as_pointer()) if highlighted else None
    gpu.state.blend_set('ALPHA')
    # a Look-at's line from the head to where it aims
    region, rv3d = context.region, _rv3d(context)
    for h in items(context.scene, arm):
        if h.ap_ety == 2 and h.aim_set and h.ap_enabled:
            pb = _joint_pb(arm, h.ap_joint)
            if pb is None:
                continue
            a = view3d_utils.location_3d_to_region_2d(region, rv3d, arm.matrix_world @ pb.matrix.translation)
            b = view3d_utils.location_3d_to_region_2d(region, rv3d, world(arm, h))
            if a is not None and b is not None:
                # where the head looks: a line from it, with a head on the line
                col = (1, 1, 1, 0.6)
                st.lines([((a.x, a.y), (b.x, b.y))], max(1.0, 1.4 * u), col)
                d = Vector((b.x - a.x, b.y - a.y))
                if d.length > 12 * u:
                    d.normalize()
                    n = Vector((-d.y, d.x))
                    tip = Vector((b.x, b.y)) - d * 9 * u
                    st.polygon([tuple(tip + d * 6 * u), tuple(tip - d * 2 * u + n * 4 * u),
                                tuple(tip - d * 2 * u - n * 4 * u)], col)
    from . import wormhole
    if not wormhole.shown(context):
        wormhole.draw_propagation(context, with_offsets=False)   # through time, where the body is
    if _guide["center"] is not None and _guide["axis"]:
        from .curve_edit import _theme_axis_colours
        colours = _theme_axis_colours()
        for ax in _guide["axis"]:
            a3 = _guide["center"] - _AXES[ax] * 20.0
            b3 = _guide["center"] + _AXES[ax] * 20.0
            a = view3d_utils.location_3d_to_region_2d(region, rv3d, a3)
            b = view3d_utils.location_3d_to_region_2d(region, rv3d, b3)
            if a is None or b is None:
                c = view3d_utils.location_3d_to_region_2d(region, rv3d, _guide["center"])
                a = a or c
                b = b or c
            if a is not None and b is not None:
                st.lines([((a.x, a.y), (b.x, b.y))], max(1.0, 1.3 * u), (*colours[ax], 0.8))
    for h, (x, y), r in _projected(context, arm):
        on = h.ap_enabled
        is_hot = hot == h.name or (_drag["active"] and _drag["name"] == h.name)
        rr = r * (1.18 if is_hot else 1.0)
        shape = _shape(h, x, y, rr)
        # a dark rim first, so a handle reads on a light body as on a dark one
        st.polygon(_shape(h, x, y, rr + 1.6 * u), (0.0, 0.0, 0.0, 0.45))
        if on:
            st.polygon(shape, st.with_alpha(st.TUTUJI_PINK, 1.0 if is_hot else 0.92))
            if h.ap_rot:            # keeps its turn too: a dot in the dot
                st.polygon(_circle(x, y, rr * 0.38, 12), st.WHITE)
        else:
            st.polygon(shape, (0.12, 0.12, 0.13, 0.55))
            st.lines(list(zip(shape, shape[1:] + shape[:1])), max(1.0, 1.4 * u),
                     (1, 1, 1, 0.95 if is_hot else 0.7))
        if h.select:
            ring = _shape(h, x, y, rr + 3.0 * u)
            st.lines(list(zip(ring, ring[1:] + ring[:1])), max(1.0, 1.8 * u), st.WHITE)
    gpu.state.blend_set('NONE')


def hit(context, x, y):
    """The handle under ``(x, y)`` (region px), nearest first, or None."""
    arm = _arm(context)
    if arm is None:
        return None
    best, best_d = None, None
    for h, (hx, hy), r in _projected(context, arm):
        d = math.hypot(hx - x, hy - y)
        if d <= r + HIT * _u() and (best_d is None or d < best_d):
            best, best_d = h, d
    return best


class ANIMATICA_GT_handles(bpy.types.Gizmo):
    """Every handle: draws them, and says which one is under the mouse."""
    bl_idname = "ANIMATICA_GT_handles"

    def draw(self, context):
        draw(context, True)

    def test_select(self, context, location):
        import os
        if os.environ.get("AP_DEBUG"):
            print("TS", tuple(location), [(x.name, xy) for x, xy, r in _projected(context, _arm(context))][:3],
                  context.region.width if context.region else None)
        from . import key_poses
        if key_poses.playing():
            return -1              # hidden while it plays: nothing to grab (a drag fought the playback)
        h = hit(context, *location)
        name = h.name if h is not None else ""
        key = context.area.as_pointer()
        if _hover.get(key) != name:
            _hover[key] = name
            context.area.tag_redraw()
        if h is None:
            return -1
        names = [it.name for it in items(context.scene, _arm(context))]
        return min(names.index(h.name), MAX_PARTS - 1) if h.name in names else -1


class ANIMATICA_GGT_handles(bpy.types.GizmoGroup):
    bl_idname = "ANIMATICA_GGT_handles"
    bl_label = "Autopose Handles"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        if not tool_active(context):
            return False
        arm = _arm(context)
        if arm is None:
            return False
        from .autoposer import poser
        if not items(context.scene, arm) or (
                arm.name not in _tidied and (
                    (poser.has_controls(arm) and not poser.edit_problem(arm))
                    or any(h.ap_ety == 2 and not h.aim_set for h in items(context.scene, arm)))):
            # active with no handles (a new character, a reload), or picked from the
            # toolbar on a rig that still carries the old control bones
            _ensure_later(arm.name)
        return True

    def setup(self, context):
        self.gz = self.gizmos.new(ANIMATICA_GT_handles.bl_idname)
        for part in range(MAX_PARTS):
            self.gz.target_set_operator("animatica.handle_drag", index=part).index = part
        self.gz.use_tooltip = True
        self.gz.use_draw_hover = False
        self.gz.use_draw_modal = True      # the handles stay drawn while one is dragged


def _ensure_later(arm_name):
    """Give the character its handles from a timer: a gizmo poll is a draw,
    and a draw must not write data."""
    if _pending.get(arm_name):
        return
    _pending[arm_name] = True

    def run():
        _pending.pop(arm_name, None)
        _tidied.add(arm_name)              # once a session: a rig that will not tidy is not asked forever
        arm = bpy.data.objects.get(arm_name)
        scene = bpy.context.scene
        if arm is None or scene is None:
            return None
        if not items(scene, arm):
            ensure(scene, arm)
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type != 'VIEW_3D':
                    continue
                region = next((r for r in a.regions if r.type == 'WINDOW'), None)
                with bpy.context.temp_override(window=w, area=a, region=region):
                    retire_old_rig(bpy.context, arm)
                    for h in items(scene, arm):
                        if h.ap_ety == 2 and not h.aim_set:
                            _seed_aim(arm, h)
                a.tag_redraw()
            break
        return None
    bpy.app.timers.register(run, first_interval=0.05)


_pending: dict = {}
_tidied: set = set()


class ANIMATICA_OT_handle_drag(bpy.types.Operator):
    """Drag to pose the body while the other handles hold. Shift-drag moves the whole body.
    Ctrl-click switches the handle on or off. Click picks it (Shift-click adds to the picks)"""
    bl_idname = "animatica.handle_drag"
    bl_label = "Drag Handle"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    axis: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})   # X, Y, Z, a plane (XY...), or '' for the view

    @classmethod
    def description(cls, context, properties):
        arm = _arm(context)
        hs = items(context.scene, arm) if arm else []
        if not (0 <= properties.index < len(hs)):
            return cls.__doc__
        from .autoposer import poser
        h = hs[properties.index]
        label = poser.control_label(h.ap_joint, h.ap_kind)
        state = ("on, so the pose keeps this joint here" + (", rotation included" if h.ap_rot else "")
                 if h.ap_enabled else "off, so the pose decides where this joint goes")
        return (f"{label}, {state}.\nDrag to move it and pose the body. Shift-drag moves the whole body. "
                "Click to pick it, then press G to move or R to turn, or use its gizmo. "
                "Ctrl-click switches it on or off")

    def invoke(self, context, event):
        arm = _arm(context)
        hs = items(context.scene, arm) if arm else []
        if not (0 <= self.index < len(hs)):
            return {'CANCELLED'}
        h = hs[self.index]
        self._name = h.name
        self._was_on = bool(h.ap_enabled)     # Esc puts it back as it was
        if event.ctrl and not self.axis:
            h.ap_enabled = not h.ap_enabled
            if not h.ap_enabled:
                resolve(context, arm)
            context.area.tag_redraw()
            return {'FINISHED'}
        why = problem(context, arm)
        if why:
            self.report({'WARNING'}, why)
            return {'CANCELLED'}
        self._arm = arm
        self._snap = snapshot(arm, hs)
        self._start = world(arm, h)
        self._press = (event.mouse_region_x, event.mouse_region_y)
        self._moved = False
        self._shift = event.shift
        # through time: the frames around this one follow, with a falloff -- when
        # the edit is being kept (Auto Keying) or the wormhole shows it
        from . import carry, wormhole
        from .autoposer import poser
        s = context.scene.animatica
        self._f0 = int(context.scene.frame_current)
        self._radius = self._radius0 = int(s.trail_radius)
        jb = poser.joint_bone(arm, h.ap_joint) if h.ap_ety != 2 else None
        self._bone = jb.name if jb is not None else ""
        # through time when the edit is kept (Auto Keying) or the ghosts show it
        # (the wormhole, or the plain onion skins: both re-pose as you drag)
        self._through = bool(context.scene.tool_settings.use_keyframe_insert_auto or wormhole.editable(context))
        self._floor = floor_cap(arm, self._snap)
        # one frame or many, the edit is measured the same way (radius 0: one)
        self._carry = carry.Carry(context, arm, self._f0, self._radius if self._through else 0,
                                  self._snap["bases"], dragged=self._bone, held=carry.held_ends(arm, hs, h))
        # the solve of the pose as it is, nothing moved: the edit is measured
        # from it, so what the poser would change anyway is not part of it
        if not solve(context, arm, effectors(arm, hs, self._snap), self._floor):
            self._carry.set_base({pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones})
        restore(arm, self._snap)
        context.view_layer.update()
        _drag.update(active=True, name=h.name, error="")
        self._hint(context, "")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _hint(self, context, err):
        from .autoposer import poser
        h = self._item(context)
        what = poser.control_label(h.ap_joint, h.ap_kind) if h else "Handle"
        reach = (f"   |   Wheel: \u00b1{self._radius} frames follow"
                 if getattr(self, "_through", False) else "")
        context.area.header_text_set(
            f"Move {what}   |   Shift: Whole Body{reach}   |   Esc: Cancel"
            + (f"   |   {err}" if err else ""))

    def _item(self, context):
        return next((h for h in items(context.scene, self._arm) if h.name == self._name), None)

    def _target(self, context, event):
        return constrained(context, (event.mouse_region_x, event.mouse_region_y), self._start, self.axis)

    def modal(self, context, event):
        # an error mid-drag, or Blender ending the operator, must not leave the
        # add-on believing a drag is under way (ghosts stop refilling, edits are
        # ignored, the header text stays) -- it cancels, as Esc does
        try:
            return self._modal(context, event)
        except Exception:                               # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.cancel(context)
            return {'CANCELLED'}

    def cancel(self, context):
        try:
            self._finish(context, cancel=True)
        except Exception:                               # noqa: BLE001
            from . import handles as _h, wormhole as _w
            _h.abort_drags(context)

    def _modal(self, context, event):
        if event.type == 'INBETWEEN_MOUSEMOVE':
            return {'RUNNING_MODAL'}    # each one a solve: a trackpad's stream of them queued up, and the drag lagged
        if event.type == 'MOUSEMOVE':
            dx = event.mouse_region_x - self._press[0]
            dy = event.mouse_region_y - self._press[1]
            if not self._moved and math.hypot(dx, dy) < 3 * _u():
                return {'RUNNING_MODAL'}
            self._moved = True
            h = self._item(context)
            if h is None:
                return self._finish(context, cancel=True)
            if not h.ap_enabled:
                h.ap_enabled = True            # grabbing a joint means: put it here
            target = self._target(context, event)
            if h.ap_ety == 2:
                h.aim = target
                h.aim_frame = self._f0
            hs = items(context.scene, self._arm)
            from . import wormhole
            # the Autoposer, always: the body follows and the switched-on handles
            # hold -- through time too, where the frames around this one then
            # take a share of what the solve changed
            eff = effectors(self._arm, hs, self._snap, h, target, whole=event.shift)
            failed = solve(context, self._arm, eff, self._floor)
            err = failed
            if not err and not any(x.ap_enabled for x in hs if x.name != h.name):
                err = "No other handle holds: the whole body follows. Switch some on (Ctrl-click) to pin them"
            if not failed:
                self._carry.move_whole(event.shift)
                self._carry.set_after({pb.name: pb.matrix_basis.copy() for pb in self._arm.pose.bones})
                # this frame as the edit leaves it -- the frames around it the same way
                bases = self._carry.shown_at_f0()
                for name in self._carry.edited:
                    self._arm.pose.bones[name].matrix_basis = bases[name]
                context.view_layer.update()
                if self._through:
                    wormhole.show_through(context, self._carry, self._bone)
            _drag["error"] = err
            self._hint(context, err)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if self._through and event.type in {'WHEELUPMOUSE', 'PAGE_UP', 'WHEELDOWNMOUSE', 'PAGE_DOWN'} \
                and event.value == 'PRESS':
            # how far through time: as proportional editing, wheel down widens
            step = 1 if event.type in {'WHEELDOWNMOUSE', 'PAGE_UP'} else -1
            self._radius = max(0, min(60, self._radius + step))
            context.scene.animatica.trail_radius = self._radius
            self._carry.radius = self._radius
            from . import wormhole
            wormhole.show_through(context, self._carry, self._bone, force=True)
            self._hint(context, _drag["error"])
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return self._finish(context)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish(context, cancel=True)
        return {'RUNNING_MODAL'}

    def _finish(self, context, cancel=False):
        from . import wormhole
        _drag.update(active=False, name="", error="")
        context.area.header_text_set(None)
        if not (not cancel and self._moved and self._through and self._carry.changed()):
            wormhole.end_through(context, None)
        if cancel:
            context.scene.animatica.trail_radius = self._radius0
            restore(self._arm, self._snap)
            for h in items(context.scene, self._arm):
                if h.name in self._snap["aims"]:
                    h.aim = self._snap["aims"][h.name]
                if h.name == self._name:
                    h.ap_enabled = getattr(self, "_was_on", h.ap_enabled)
            context.view_layer.update()
            context.area.tag_redraw()
            return {'CANCELLED'}
        if not self._moved and not self.axis:
            # a click: pick it (Shift adds to the picked ones)
            for h in items(context.scene, self._arm):
                if h.name == self._name:
                    h.select = (not h.select) if self._shift else True
                elif not self._shift:
                    h.select = False
            context.area.tag_redraw()
            return {'FINISHED'}
        if self._through and self._carry.changed():
            self._keep_through(context)
        elif context.scene.tool_settings.use_keyframe_insert_auto:
            key_pose(context, self._arm)
        context.area.tag_redraw()
        return {'FINISHED'}

    def _keep_through(self, context):
        """This frame keyed as posed, and the frames around it with their share."""
        from . import key_poses, wormhole
        n = wormhole.end_through(context, self._carry)
        key_poses.flash_keyed(self._f0)
        self.report({'INFO'}, wormhole.edit_report(self._carry, n))


def abort_drags(context=None) -> None:
    """Forget any drag under way (an operator that died, a file loaded): the
    flags that say one is, the preview through time and the header text."""
    from . import carry, wormhole
    _drag.update(active=False, name="", error="")
    wormhole._drag.update(active=False, points=None, parents=None, error="", bent=None)
    wormhole.propagation.update(f0=None, radius=0, frames={}, bone="", at=0.0, carry=None, trail={})
    try:
        carry.clear_live()
    except Exception:                                   # noqa: BLE001
        pass
    area = getattr(context, "area", None) if context is not None else None
    if area is not None:
        try:
            area.header_text_set(None)
        except Exception:                               # noqa: BLE001
            pass


_AXES = {"X": Vector((1.0, 0.0, 0.0)), "Y": Vector((0.0, 1.0, 0.0)), "Z": Vector((0.0, 0.0, 1.0))}


def constrained(context, co, origin, axis=""):
    """Where the mouse at region ``co`` puts a point that started at
    ``origin``: on an axis (X, Y, Z), in a plane (XY, YZ, ZX), or in the plane
    facing the view -- Blender's own move, in words."""
    from mathutils.geometry import intersect_line_plane
    from .curve_edit import _closest_on_axis
    region, rv3d = context.region, _rv3d(context)
    o = view3d_utils.region_2d_to_origin_3d(region, rv3d, co)
    d = view3d_utils.region_2d_to_vector_3d(region, rv3d, co)
    if len(axis) == 1:
        return _closest_on_axis(origin, _AXES[axis], o, d) or origin
    if len(axis) == 2:
        a, b = (_AXES[c] for c in axis)
        normal = a.cross(b)
    else:
        normal = rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
    return intersect_line_plane(o, o + d * 10000.0, origin, normal) or origin


def picked_items(scene, arm) -> list:
    return [h for h in items(scene, arm) if h.select]


class ANIMATICA_OT_handle_transform(bpy.types.Operator):
    """Move (G) or turn (R) the picked handles to pose the body, like Blender's own
    G and R. Press X, Y or Z to lock an axis (Shift locks the plane across it). Click
    or press Enter to confirm, and press Esc or right-click to cancel"""
    bl_idname = "animatica.handle_transform"
    bl_label = "Move Handles"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(items=(('MOVE', "Move", ""), ('ROTATE', "Rotate", "")))

    @classmethod
    def poll(cls, context):
        return tool_active(context) and _arm(context) is not None

    def invoke(self, context, event):
        arm = _arm(context)
        sel = picked_items(context.scene, arm) if arm else []
        if not sel:
            return {'PASS_THROUGH'}          # nothing picked: Blender's own G / R, on the bones
        if self.mode == 'ROTATE':
            sel = [h for h in sel if h.ap_ety != 2]
            if not sel:
                return {'PASS_THROUGH'}
        why = problem(context, arm)
        if why:
            self.report({'WARNING'}, why)
            return {'CANCELLED'}
        self._arm = arm
        self._names = [h.name for h in sel]
        hs = items(context.scene, arm)
        self._snap = snapshot(arm, hs)
        self._was = {h.name: (h.ap_enabled, h.ap_rot) for h in sel}
        self._starts = {h.name: world(arm, h) for h in sel}
        pts = [p for p in self._starts.values() if p is not None]
        if not pts:
            return {'PASS_THROUGH'}          # none of them has a place in the scene to move from
        self._center = sum(pts, Vector()) / len(pts)
        self._mouse0 = (event.mouse_region_x, event.mouse_region_y)
        self._grab0 = constrained(context, self._mouse0, self._center)
        self._axis = ""
        self._error = ""
        c2 = view3d_utils.location_3d_to_region_2d(context.region, _rv3d(context), self._center)
        self._c2 = c2 if c2 is not None else Vector(self._mouse0)
        self._angle0 = math.atan2(self._mouse0[1] - self._c2.y, self._mouse0[0] - self._c2.x)
        for h in sel:
            h.ap_enabled = True
        # through time, as a drag is: a turned hand turns on the frames around
        # it too, with the falloff, and the ghosts and the trail show it live
        from . import carry, wormhole
        from .autoposer import poser
        self._f0 = int(context.scene.frame_current)
        self._radius = int(context.scene.animatica.trail_radius)
        self._through = bool(context.scene.tool_settings.use_keyframe_insert_auto or wormhole.editable(context))
        picked = {poser.joint_bone(arm, h.ap_joint).name for h in sel
                  if h.ap_ety != 2 and poser.joint_bone(arm, h.ap_joint) is not None}
        self._bone = next(iter(picked)) if len(picked) == 1 else ""
        self._floor = floor_cap(arm, self._snap)
        self._carry = carry.Carry(context, arm, self._f0, self._radius if self._through else 0,
                                  self._snap["bases"], dragged=self._bone,
                                  held=[b for b in carry.held_ends(arm, hs) if b not in picked])
        # the solve of the pose as it is, nothing moved or turned: the edit is
        # measured from it, so the poser's own small changes are not part of it
        still = (effectors(arm, hs, self._snap, rot_over={h.name: self._snap["joints"][h.ap_joint][1]
                                                          for h in sel if h.ap_joint in self._snap["joints"]})
                 if self.mode == 'ROTATE' else
                 effectors(arm, hs, self._snap, targets={n: p for n, p in self._starts.items() if p is not None}))
        if not solve(context, arm, still, self._floor):
            self._carry.set_base({pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones})
        restore(arm, self._snap)
        context.view_layer.update()
        _drag.update(active=True, name=sel[0].name, error="")
        _guide.update(center=self._center.copy(), axis="")
        self._header(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _header(self, context):
        what = "Move" if self.mode == 'MOVE' else "Rotate"
        lock = f" along {self._axis}" if len(self._axis) == 1 else (f" in {self._axis}" if self._axis else "")
        context.area.header_text_set(f"{what} {len(self._names)} handle{'s' if len(self._names) != 1 else ''}"
                                     f"{lock}   |   X/Y/Z: Axis   |   Shift: Plane   |   Click/Enter: Confirm   "
                                     f"|   Esc/Right-click: Cancel" + (f"   |   {self._error}" if self._error else ""))

    def _apply(self, context, event):
        co = (event.mouse_region_x, event.mouse_region_y)
        hs = items(context.scene, self._arm)
        if self.mode == 'MOVE':
            now = constrained(context, co, self._center, self._axis)
            start = constrained(context, self._mouse0, self._center, self._axis)
            d = now - start
            targets = {n: self._starts[n] + d for n in self._names if self._starts.get(n) is not None}
            for h in hs:
                if h.name in targets and h.ap_ety == 2:
                    h.aim = targets[h.name]
                    h.aim_frame = self._f0
            eff = effectors(self._arm, hs, self._snap, targets=targets)
        else:
            ang = math.atan2(co[1] - self._c2.y, co[0] - self._c2.x) - self._angle0
            rv3d = _rv3d(context)
            axis = _AXES[self._axis] if len(self._axis) == 1 else rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
            Rw = Matrix.Rotation(-ang if len(self._axis) != 1 else ang, 3, axis)
            A = self._arm.matrix_world.to_3x3().normalized()
            Ra = A.inverted() @ Rw @ A                      # the turn, in armature space
            rot = {}
            for h in hs:
                if h.name in self._names and h.ap_joint in self._snap["joints"]:
                    rot[h.name] = Ra @ self._snap["joints"][h.ap_joint][1]
            eff = effectors(self._arm, hs, self._snap, rot_over=rot)
        self._error = solve(context, self._arm, eff, self._floor)
        if not self._error:
            # this frame as the edit leaves it, and the frames around it with it
            self._carry.set_after({pb.name: pb.matrix_basis.copy() for pb in self._arm.pose.bones})
            bases = self._carry.shown_at_f0()
            for name in self._carry.edited:
                self._arm.pose.bones[name].matrix_basis = bases[name]
            context.view_layer.update()
            if self._through:
                from . import wormhole
                wormhole.show_through(context, self._carry, self._bone)
        _drag["error"] = self._error
        context.area.tag_redraw()

    def modal(self, context, event):
        # an error mid-drag, or Blender ending the operator, must not leave the
        # add-on believing a drag is under way (ghosts stop refilling, edits are
        # ignored, the header text stays) -- it cancels, as Esc does
        try:
            return self._modal(context, event)
        except Exception:                               # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.cancel(context)
            return {'CANCELLED'}

    def cancel(self, context):
        try:
            self._finish(context, cancel=True)
        except Exception:                               # noqa: BLE001
            from . import handles as _h, wormhole as _w
            _h.abort_drags(context)

    def _modal(self, context, event):
        if event.type == 'INBETWEEN_MOUSEMOVE':
            return {'RUNNING_MODAL'}    # each one a solve: a trackpad's stream of them queued up, and the drag lagged
        if event.type == 'MOUSEMOVE':
            self._apply(context, event)
            return {'RUNNING_MODAL'}
        if event.type in {'X', 'Y', 'Z'} and event.value == 'PRESS':
            plane = {"X": "YZ", "Y": "ZX", "Z": "XY"}[event.type] if event.shift else event.type
            self._axis = "" if self._axis == plane else plane
            if self.mode == 'ROTATE' and len(self._axis) == 2:
                self._axis = event.type
            _guide["axis"] = self._axis
            self._apply(context, event)
            self._header(context)
            return {'RUNNING_MODAL'}
        if (event.type == 'LEFTMOUSE' and event.value == 'PRESS') or \
                (event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS'):
            return self._finish(context)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish(context, cancel=True)
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}                 # look around mid-move, as Blender lets you
        return {'RUNNING_MODAL'}

    def _finish(self, context, cancel=False):
        _drag.update(active=False, name="", error="")
        _guide.update(center=None, axis="")
        context.area.header_text_set(None)
        hs = {h.name: h for h in items(context.scene, self._arm)}
        from . import key_poses, wormhole
        keep = not cancel and self._through and self._carry.changed()
        if not keep:
            wormhole.end_through(context, None)
        if cancel:
            restore(self._arm, self._snap)
            for n, (en, rot) in self._was.items():
                if n in hs:
                    hs[n].ap_enabled, hs[n].ap_rot = en, rot
                    if n in self._snap["aims"]:
                        hs[n].aim = self._snap["aims"][n]
            context.view_layer.update()
            context.area.tag_redraw()
            return {'CANCELLED'}
        if self.mode == 'ROTATE':
            for n in self._names:
                if n in hs:
                    hs[n].ap_rot = True         # turned by hand: it keeps that turn
        if keep:
            n = wormhole.end_through(context, self._carry)
            key_poses.flash_keyed(self._f0)
            self.report({'INFO'}, wormhole.edit_report(self._carry, n))
        elif context.scene.tool_settings.use_keyframe_insert_auto:
            key_pose(context, self._arm)
        context.area.tag_redraw()
        return {'FINISHED'}


class ANIMATICA_GGT_handle_xform(bpy.types.GizmoGroup):
    """Blender's move gizmo on the picked handle: an arrow per axis, a plane per
    pair, a ring for the view -- the theme's colours, the artist's gizmo size."""
    bl_idname = "ANIMATICA_GGT_handle_xform"
    bl_label = "Autopose Handle Gizmo"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'3D', 'PERSISTENT'}

    @classmethod
    def poll(cls, context):
        if not tool_active(context):
            return False
        arm = _arm(context)
        if arm is None:
            return False
        sel = picked_items(context.scene, arm)
        return len(sel) == 1 and not _drag["active"]

    def setup(self, context):
        from .curve_edit import ANIMATICA_GGT_curve_point as cp, _theme_axis_colours, _PLANES
        colours = _theme_axis_colours()
        self._handles = []
        for axis in ("X", "Y", "Z"):
            arrow = self.gizmos.new("GIZMO_GT_arrow_3d")
            arrow.draw_style = 'NORMAL'
            arrow.length = 1.0
            arrow.line_width = 2.0
            arrow.matrix_offset = Matrix.Translation((0.0, 0.0, 0.3))   # the centre is the ring's
            cp._paint(arrow, colours[axis], 1.0)
            self._handles.append((axis, arrow))
            plane = self.gizmos.new("GIZMO_GT_primitive_3d")
            plane.draw_style = 'PLANE'
            plane.scale_basis = 0.16
            cp._paint(plane, colours[axis], 0.6)
            self._handles.append((_PLANES[axis], plane))
        ring = self.gizmos.new("GIZMO_GT_move_3d")
        ring.draw_style = 'RING_2D'
        ring.draw_options = {'ALIGN_VIEW'}
        ring.scale_basis = 0.22
        cp._paint(ring, (1.0, 1.0, 1.0), 0.6)
        self._handles.append(("", ring))

    def refresh(self, context):
        from .curve_edit import ANIMATICA_GGT_curve_point as cp
        arm = _arm(context)
        hs = items(context.scene, arm)
        sel = [i for i, h in enumerate(hs) if h.select]
        if len(sel) != 1:
            return
        origin = world(arm, hs[sel[0]])
        if origin is None:
            return
        for constraint, gz in self._handles:
            gz.matrix_basis = cp._place(constraint, origin)
            props = gz.target_set_operator("animatica.handle_drag")
            props.index, props.axis = sel[0], constraint

    def draw_prepare(self, context):
        self.refresh(context)


class ANIMATICA_OT_handles_deselect(bpy.types.Operator):
    """Unpick the handles when you click away from them, as a click on empty space does in Blender"""
    bl_idname = "animatica.handles_deselect"
    bl_label = "Unpick Handles"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        arm = _arm(context)
        if arm is not None:
            changed = False
            for h in items(context.scene, arm):
                if h.select:
                    h.select = False
                    changed = True
            if changed:
                context.area.tag_redraw()
        return {'PASS_THROUGH'}          # and Blender's own click selects (or clears) the bones


class ANIMATICA_OT_handle_add(bpy.types.Operator):
    """Add this handle. It starts on its joint, and nothing moves until you drag it"""
    bl_idname = "animatica.handle_add"
    bl_label = "Add Handle"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    name: StringProperty()

    def execute(self, context):
        arm = _arm(context)
        if arm is None or add(context.scene, arm, self.name) is None:
            return {'CANCELLED'}
        for a in context.screen.areas:
            a.tag_redraw()
        return {'FINISHED'}


class ANIMATICA_MT_add_handle(bpy.types.Menu):
    bl_idname = "ANIMATICA_MT_add_handle"
    bl_label = "Add Handle"

    def draw(self, context):
        from .autoposer import poser
        arm = _arm(context)
        if arm is None:
            return
        have = {h.name for h in items(context.scene, arm)}
        left = [sp for sp in specs(arm) if sp["name"] not in have]
        if not left:
            self.layout.label(text="Every handle is on the character")
        for sp in left:
            self.layout.operator(ANIMATICA_OT_handle_add.bl_idname,
                                 text=poser.control_label(sp["joint"], sp["kind"])).name = sp["name"]


class ANIMATICA_TL_autopose(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'POSE'
    bl_idname = TOOL_ID
    bl_label = "Autopose"
    bl_description = ("Drag a hand, a foot, the hips or the head to pose the body while the other "
                      "handles hold. Shift-drag moves the whole body, and Ctrl-click switches a handle "
                      "on or off. Nothing is added to the rig")
    # a figure reaching up, posed by its handles (icons/src/make_tool_icon.py)
    bl_icon = os.path.join(os.path.dirname(__file__), "icons", "tool", "ops.animatica.autopose")
    # off the handles, the tool picks bones as Blender's Select Box does
    bl_keymap = (
        # G and R on the picked handles; with none picked, Blender's own on the bones
        ("animatica.handle_transform", {"type": 'G', "value": 'PRESS'}, {"properties": [("mode", 'MOVE')]}),
        ("animatica.handle_transform", {"type": 'R', "value": 'PRESS'}, {"properties": [("mode", 'ROTATE')]}),
        # I keys the pose, as in Blender -- the handles select no bones, so Blender's own I keyed nothing
        ("animatica.set_key_pose", {"type": 'I', "value": 'PRESS'}, None),
        ("animatica.handles_deselect", {"type": 'LEFTMOUSE', "value": 'CLICK'}, None),
        ("view3d.select", {"type": 'LEFTMOUSE', "value": 'CLICK'}, None),
        ("view3d.select", {"type": 'LEFTMOUSE', "value": 'CLICK', "shift": True},
         {"properties": [("toggle", True)]}),
        ("view3d.select_box", {"type": 'LEFTMOUSE', "value": 'CLICK_DRAG'}, None),
    )


def _ensure_gizmos():
    for gg in (ANIMATICA_GGT_handles, ANIMATICA_GGT_handle_xform):
        try:
            bpy.context.window_manager.gizmo_group_type_ensure(gg.bl_idname)
        except Exception:                                   # noqa: BLE001
            pass


def activate(context) -> str:
    """Make the Autopose tool the active one (Pose Mode on the character);
    '' when it is, else why not."""
    arm = _arm(context)
    why = problem(context, arm)
    if why:
        return why
    if context.mode != 'POSE':
        if context.view_layer.objects.active is not arm:
            for o in context.view_layer.objects.selected:
                o.select_set(False)
            arm.select_set(True)
            context.view_layer.objects.active = arm
        bpy.ops.object.mode_set(mode='POSE')
    ensure(context.scene, arm)
    bpy.ops.wm.tool_set_by_id(name=TOOL_ID)
    _ensure_gizmos()
    retire_old_rig(context, arm)
    return ""


def retire_old_rig(context, arm) -> bool:
    """Take off the control bones an older version of the Autoposer added to
    the rig: the tool draws its handles instead, and two sets on one body --
    the old ones answering Blender's G -- was a trap. Only where the rig can
    be edited (not a linked one)."""
    from .autoposer import poser
    if arm is None or not poser.has_controls(arm) or poser.edit_problem(arm):
        return False
    try:
        bpy.ops.autoposer.remove_rig('EXEC_DEFAULT')
    except RuntimeError as exc:
        print(f"[Animatica] old control bones not removed: {exc}")
        return False
    return True


def deactivate(context) -> None:
    bpy.ops.wm.tool_set_by_id(name=FALLBACK_TOOL)


_classes = (AnimaticaHandle, ANIMATICA_GT_handles, ANIMATICA_GGT_handles, ANIMATICA_OT_handle_drag,
            ANIMATICA_OT_handle_transform, ANIMATICA_GGT_handle_xform, ANIMATICA_OT_handles_deselect,
            ANIMATICA_OT_handle_add, ANIMATICA_MT_add_handle)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.animatica_handles = bpy.props.CollectionProperty(type=AnimaticaHandle)
    try:
        bpy.utils.register_tool(ANIMATICA_TL_autopose, after={"builtin.transform"}, separator=True)
    except Exception as exc:                                # noqa: BLE001
        print(f"[Animatica] Autopose tool not registered: {exc}")


def unregister():
    try:
        bpy.utils.unregister_tool(ANIMATICA_TL_autopose)
    except Exception:                                       # noqa: BLE001
        pass
    del bpy.types.Scene.animatica_handles
    _hover.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
