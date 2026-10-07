"""Copy a character's motion and paste it onto another: retargeting in two clicks.

**Copy Motion** copies the keys selected in the Timeline (or Dope Sheet, or Graph Editor) --
one key for a pose, many for a stretch of motion; with none selected, every key of the action.
It samples the character's pose across them into an MMCP clip, the skeleton and rotation
conventions a generate request uses, and keeps it on a clipboard. The sample is taken there and
then: editing or deleting the source afterwards does not change what is pasted.

**Paste Motion** sends that clip and the selected character's skeleton to the server's
``POST /retarget`` and puts the answer into the character's own animation, the first copied key
on the playhead: a key on each copied key's frame, the keys already in that stretch replaced and
the rest of its animation kept. Joints the retarget did not reach (no counterpart on the source)
keep their own animation too.

Nothing here maps bones. The server characterises both rigs (hips, spine, legs, arms, whatever
the names), scales for proportions, reconciles A- and T-pose rests, lands the clip where the
target stands facing its own way, and answers with the mapping it used.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import bpy
import numpy as np
from bpy.props import StringProperty
from bpy.types import Operator

from . import constraints_ui, coords, mmcp_client, properties, request_builder
from .request_builder import PROTOCOL_VERSION

#: what Copy Motion took, or None: ``clip`` (what is sent), ``sampled`` (the source frame of each
#: clip frame), ``keys`` (copied frame -> its key type), ``source``, ``action``, ``whole`` (nothing
#: was selected: every key), ``stamp``, ``announce`` (the bar still says it was copied)
_CLIPBOARD: dict[str, Any] | None = None

#: a copy spanning more frames than this is sampled at its keys only, not every frame across them
DENSE_SPAN_MAX = 2400

#: the key types, strongest first: a frame keyed as several takes the strongest
_KEY_RANK = {"KEYFRAME": 0, "BREAKDOWN": 1, "EXTREME": 2, "MOVING_HOLD": 3, "JITTER": 4, "GENERATED": 5}


# ═══════════════════════════════════════════════════════════════════════════
# Sampling
# ═══════════════════════════════════════════════════════════════════════════

def action_frame_range(action: bpy.types.Action) -> tuple[int, int]:
    """The action's keyed range as whole frames, first and last inclusive."""
    start, end = action.frame_range
    return int(round(start)), max(int(round(end)), int(round(start)))


def scene_fps(scene: bpy.types.Scene) -> float:
    return float(scene.render.fps) / float(scene.render.fps_base or 1.0)


def sample_motion(armature_obj, action, frames) -> dict[str, Any]:
    """Sample ``action`` on ``armature_obj`` into an MMCP clip, one clip frame per frame in
    ``frames`` (a ``(first, last)`` pair is every frame between, inclusive).

    Returns ``{"skeleton", "fps", "rotations", "root_translations", "joint_positions"}``: the
    rig's rest as :func:`request_builder.armature_to_skeleton` sends it, one ``[x, y, z, w]``
    local rotation per skeleton joint per frame, the root joint's world position per frame, and
    every joint's.

    MMCP moves only the root; the per-joint positions let the server keep translation keyed
    further down, on the pelvis of a rig whose root is a static bone on the floor (UE5 and most
    engine exports).

    The pose is read from the evaluated rig, so a control rig (Rigify, Mixamo, ARP) reports
    what its constraints drive the deform bones to, not the raw keys on its controls. The NLA is
    switched off for the duration so the clip is this action and only this action; the rig's
    own action, NLA state and the scene frame are restored afterwards.
    """
    if isinstance(frames, tuple) and len(frames) == 2:
        frames = range(frames[0], frames[1] + 1)
    skeleton = request_builder.armature_to_skeleton(armature_obj)
    joint_names = [j["name"] for j in skeleton["joints"]]
    root_name = next(j["name"] for j in skeleton["joints"] if j["parent"] is None)
    pose_bones = [armature_obj.pose.bones[n] for n in joint_names]
    root_pb = armature_obj.pose.bones[root_name]

    if armature_obj.animation_data is None:
        armature_obj.animation_data_create()
    ad = armature_obj.animation_data
    saved_action, saved_use_nla = ad.action, ad.use_nla
    ad.action = action
    ad.use_nla = False
    scene = bpy.context.scene
    saved_frame = scene.frame_current
    # ``matrix_world`` is only as fresh as the last depsgraph update
    bpy.context.view_layer.update()
    mw = armature_obj.matrix_world
    mw_rot = mw.to_quaternion().to_matrix()
    mw_rot_t = mw_rot.transposed()

    rotations, root_translations, joint_positions = [], [], []
    try:
        for f in frames:
            scene.frame_set(f)
            # ``frame_set`` alone does not always carry through drivers and constraint stacks
            bpy.context.view_layer.update()
            rotations.append([constraints_ui.mmcp_joint_rotation(pb, mw_rot, mw_rot_t) for pb in pose_bones])
            root_translations.append(list(coords.blender_pos_to_mmcp((mw @ root_pb.matrix).translation)))
            joint_positions.append([list(coords.blender_pos_to_mmcp(mw @ pb.head)) for pb in pose_bones])
    finally:
        ad.action = saved_action
        ad.use_nla = saved_use_nla
        scene.frame_set(saved_frame)

    return {"skeleton": skeleton, "fps": scene_fps(scene), "rotations": rotations,
            "root_translations": root_translations, "joint_positions": joint_positions}


def build_retarget_request(clip: dict[str, Any], target_obj) -> dict[str, Any]:
    """The ``POST /retarget`` body moving ``clip`` onto ``target_obj``."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "source": clip,
        "target": {"skeleton": request_builder.armature_to_skeleton(target_obj)},
        "options": {"secondary_motion": False},
    }


def retarget_report(gltf: dict[str, Any]) -> dict[str, Any]:
    """The server's ``MMCP_retarget`` extension: method, mapping, unmapped joints."""
    return (gltf.get("extensions") or {}).get("MMCP_retarget") or {}


# ═══════════════════════════════════════════════════════════════════════════
# Copying: the selected keys
# ═══════════════════════════════════════════════════════════════════════════

def key_frames(ob, action, *, selected: bool, bones: set | None = None) -> dict[int, str]:
    """``{frame: key type}`` of ``action``'s keys on ``ob`` -- only the selected ones with
    ``selected``, only the curves of ``bones`` with ``bones`` -- a frame keyed as several types
    taking the strongest (a key pose over a generated frame)."""
    from ._bake_common import bone_of
    types = {item.value: item.identifier for item in bpy.types.Keyframe.bl_rna.properties["type"].enum_items}
    out: dict[int, str] = {}
    for fc in constraints_ui.iter_action_fcurves(action, ob):
        if bones is not None and bone_of(fc.data_path) not in bones:
            continue
        kps = fc.keyframe_points
        n = len(kps)
        if not n:
            continue
        co = np.empty(2 * n, dtype=np.float32)
        kps.foreach_get("co", co)
        kind = np.empty(n, dtype=np.int32)
        kps.foreach_get("type", kind)
        pick = np.ones(n, dtype=bool)
        if selected:
            kps.foreach_get("select_control_point", pick)
        for f, k in zip(np.rint(co[0::2][pick]).astype(int).tolist(), kind[pick].tolist()):
            t = types.get(k, "KEYFRAME")
            if f not in out or _KEY_RANK.get(t, 9) < _KEY_RANK.get(out[f], 9):
                out[f] = t
    return out


def _bone_selected(pb) -> bool:
    sel = getattr(pb, "select", None)           # Blender 5: on the pose bone
    return bool(sel if sel is not None else getattr(pb.bone, "select", False))


def selected_keys(ob, action) -> tuple[dict[int, str], bool]:
    """``({frame: key type}, picked)``: the keys to copy, and whether they were picked.

    The keys selected where the artist selected them. Blender keeps a key's selection on the key,
    and a click in the Timeline changes only the keys it shows -- in Pose mode, the selected
    bones' -- so every other curve keeps whatever it had: after a generation, all of its keys,
    selected when they were made. Read across every curve, a click on one frame copied the
    whole take. So the selected bones' keys are read first (what the Timeline shows); then, if
    none of those is selected, every curve's (a selection made in the Dope Sheet); and with no
    key selected anywhere, every key."""
    if ob.mode == 'POSE':
        shown = {pb.name for pb in ob.pose.bones if _bone_selected(pb)}
        if shown:
            keys = key_frames(ob, action, selected=True, bones=shown)
            if keys:
                return keys, True
    keys = key_frames(ob, action, selected=True)
    if keys:
        return keys, True
    return key_frames(ob, action, selected=False), False


def copy(ob) -> dict[str, Any]:
    """Copy ``ob``'s selected keys (all of them when none is selected) onto the clipboard:
    Copy Motion, for scripts and tests too.

    The pose is sampled on every frame across them, not only on the keys, when that is not too
    many: the server reads which foot is planted from how the feet move between frames, and
    poses far apart would read as feet that never move."""
    global _CLIPBOARD
    action = ob.animation_data.action
    keys, picked = selected_keys(ob, action)
    if not keys:
        raise ValueError(f"{action.name} has no keys")
    frames = sorted(keys)
    lo, hi = frames[0], frames[-1]
    sampled = list(range(lo, hi + 1)) if hi - lo < DENSE_SPAN_MAX else frames
    _CLIPBOARD = {"clip": sample_motion(ob, action, sampled), "sampled": sampled, "keys": keys,
                  "source": ob.name, "action": action.name, "whole": not picked,
                  "stamp": time.monotonic(), "announce": True}
    return _CLIPBOARD


# ═══════════════════════════════════════════════════════════════════════════
# Pasting: into the target's own animation
# ═══════════════════════════════════════════════════════════════════════════

#: each channel's rest value, by property: a joint the retarget did not reach comes back at rest
_REST = {"rotation_quaternion": (1.0, 0.0, 0.0, 0.0), "rotation_euler": (0.0, 0.0, 0.0),
         "rotation_axis_angle": (0.0, 0.0, 1.0, 0.0), "location": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0)}


def paste_frames(copied: dict[str, Any], at_frame: int) -> list[tuple[int, int, str]]:
    """``[(clip index, target frame, key type)]`` for each copied key, the first on ``at_frame``."""
    offset = int(at_frame) - min(copied["keys"])
    return [(i, f + offset, copied["keys"][f]) for i, f in enumerate(copied["sampled"]) if f in copied["keys"]]


def _scratch_values(action, ob, frames: list[int]) -> dict[tuple[str, int], np.ndarray]:
    """``{(data path, index): values}`` of ``action`` at ``frames`` (each a key on it)."""
    out = {}
    want = np.asarray(frames, dtype=np.float64)
    for fc in constraints_ui.iter_action_fcurves(action, ob):
        kps = fc.keyframe_points
        n = len(kps)
        if not n:
            continue
        co = np.empty(2 * n, dtype=np.float64)
        kps.foreach_get("co", co)
        at = np.clip(np.searchsorted(co[0::2], want - 0.5), 0, n - 1)
        exact = np.abs(co[0::2][at] - want) < 0.5
        vals = np.where(exact, co[1::2][at], [fc.evaluate(f) for f in frames] if not exact.all() else 0.0)
        out[(fc.data_path, fc.array_index)] = vals
    return out


def _value_before(action, ob, path: str, index: int, frame: float) -> float:
    for fc in constraints_ui.iter_action_fcurves(action, ob):
        if fc.data_path == path and fc.array_index == index:
            return fc.evaluate(frame)
    return 0.0


def _in_mode(action, ob, bone: str, quat: dict, mode: str, first_frame: int):
    """A quaternion channel set re-expressed in the bone's own rotation mode: ``(path, {index: values})``."""
    from mathutils import Euler, Quaternion
    n = len(quat[0])
    qs = [Quaternion((quat[0][k], quat[1][k], quat[2][k], quat[3][k])) for k in range(n)]
    if mode == 'AXIS_ANGLE':
        out = {i: np.empty(n) for i in range(4)}
        for k, q in enumerate(qs):
            axis, angle = q.to_axis_angle()
            out[0][k], out[1][k], out[2][k], out[3][k] = angle, axis.x, axis.y, axis.z
        return f'pose.bones["{bone}"].rotation_axis_angle', out
    path = f'pose.bones["{bone}"].rotation_euler'
    # turned the short way from the bone's own angles just before: no 360 spins at the seam
    prev = Euler([_value_before(action, ob, path, i, first_frame - 1) for i in range(3)], mode)
    out = {i: np.empty(n) for i in range(3)}
    for k, q in enumerate(qs):
        prev = q.to_euler(mode, prev)
        out[0][k], out[1][k], out[2][k] = prev
    return path, out


def _overwrite(fc, frames: list[int], values, types: list[str]) -> None:
    """Replace ``fc``'s keys from the first of ``frames`` to the last with one key on each."""
    kps = fc.keyframe_points
    lo, hi = min(frames) - 0.5, max(frames) + 0.5
    n = len(kps)
    if n:
        co = np.empty(2 * n, dtype=np.float64)
        kps.foreach_get("co", co)
        for i in reversed(np.nonzero((co[0::2] > lo) & (co[0::2] < hi))[0].tolist()):
            kps.remove(kps[i], fast=True)
    n0 = len(kps)
    kps.add(len(frames))
    co = np.empty(2 * len(kps), dtype=np.float64)
    kps.foreach_get("co", co)
    co[2 * n0::2] = frames
    co[2 * n0 + 1::2] = values
    kps.foreach_set("co", co)
    for k, kind in enumerate(types):
        kp = kps[n0 + k]
        kp.type = kind
        kp.interpolation = 'BEZIER'
        kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
    fc.update()


def bake_result(copied: dict[str, Any], gltf: dict[str, Any], target, at_frame: int) -> dict[str, Any]:
    """The server's answer, into ``target``'s own animation: a key on each copied key's frame,
    the first on ``at_frame``, the keys in that stretch replaced, the rest kept.

    The answer is baked onto a scratch action first -- the bake knows the target's bones and,
    on a control rig, its controls -- and only the copied frames come across. A joint the
    retarget did not reach, left at rest, is not written: its own animation stays. A bone keyed
    in Euler (or axis-angle) gets its keys that way, its rotation mode unchanged.

    Returns ``{"action", "frames", "joints", "kept"}``: the action written to, the frames keyed,
    how many bones were written and how many kept their own motion."""
    from . import _bake_common, gltf_to_blender
    picks = paste_frames(copied, at_frame)
    if not picks:
        raise ValueError("nothing copied to paste")
    if target.animation_data is None:
        target.animation_data_create()
    ad = target.animation_data
    own, own_slot = ad.action, getattr(ad, "action_slot", None)
    modes = {pb.name: pb.rotation_mode for pb in target.pose.bones}
    first = int(at_frame) - (min(copied["keys"]) - copied["sampled"][0])    # where clip frame 0 lands
    scratch = None
    try:
        scratch = gltf_to_blender.bake_gltf_to_armature(gltf, target, action_name="Paste Motion (scratch)",
                                                        start_frame=first)
        values = _scratch_values(scratch, target, [first + i for i, _f, _t in picks])
    finally:
        ad.action = own
        if own is not None and own_slot is not None:
            try:
                ad.action_slot = own_slot
            except Exception:                                        # noqa: BLE001
                pass
        for name, mode in modes.items():
            target.pose.bones[name].rotation_mode = mode
        if scratch is not None:
            bpy.data.actions.remove(scratch)
    if own is None:
        own = bpy.data.actions.new(f"{target.name}Action")
        ad.action = own
    frames = [f for _i, f, _t in picks]
    types = [t for _i, _f, t in picks]
    report = retarget_report(gltf)
    unreached = set(report.get("unmapped_target_joints") or ())
    # a control rig's deform bones follow its controls: keys on them do nothing
    skip = set(request_builder.detect_deform_bones(target)) if request_builder.is_control_rig(target) else set()
    by_bone: dict[tuple[str, str], dict[int, np.ndarray]] = {}
    for (path, index), vals in values.items():
        by_bone.setdefault((_bake_common.bone_of(path), path), {})[index] = vals
    written, kept = set(), set()
    for (bone, path), chans in sorted(by_bone.items()):
        if bone in skip:
            continue
        prop = path.rsplit(".", 1)[-1]
        if bone in unreached:
            kept.add(bone)
            continue
        mode = modes.get(bone, 'QUATERNION')
        if prop == "rotation_quaternion" and mode != 'QUATERNION' and len(chans) == 4:
            path, chans = _in_mode(own, target, bone, chans, mode, frames[0])
            prop = path.rsplit(".", 1)[-1]
        rest = _REST.get(prop)
        if rest is not None and all(np.allclose(v, rest[i], atol=1e-6) for i, v in chans.items() if i < len(rest)):
            kept.add(bone)                  # at rest throughout: not reached (a control rig's), keep its own
            continue
        for index, vals in sorted(chans.items()):
            fc = _bake_common.ensure_fcurve(own, target, path, index)
            if fc is not None:
                _overwrite(fc, frames, vals, types)
        written.add(bone)
    _bake_common.group_curves(own)
    return {"action": own, "frames": frames, "joints": len(written), "kept": len(kept - written)}


def paste(target, server_url: str | None = None, at_frame: int | None = None) -> dict[str, Any]:
    """Paste the clipboard onto ``target`` in one go, on this thread (scripts and tests: the
    operator does the same with the request off the main thread)."""
    if _CLIPBOARD is None:
        raise ValueError("nothing copied")
    at = bpy.context.scene.frame_current if at_frame is None else at_frame
    gltf = mmcp_client.MmcpClient(server_url or mmcp_client.get_mmcp_url()).retarget(
        build_retarget_request(_CLIPBOARD["clip"], target))
    return bake_result(_CLIPBOARD, gltf, target, at)


# ═══════════════════════════════════════════════════════════════════════════
# The clipboard
# ═══════════════════════════════════════════════════════════════════════════

def _active_armature(context):
    """The character the command is for: the active object if it is an armature, else
    Animatica's character."""
    ob = context.active_object
    if ob is not None and ob.type == 'ARMATURE':
        return ob
    s = getattr(context.scene, "animatica", None)
    return properties._live_armature(s.target_armature) if s is not None else None


def clipboard() -> dict[str, Any] | None:
    return _CLIPBOARD


def clipboard_label() -> str:
    """What is copied, in words: "the pose at frame 12 of Walk from Animatic", "3 keys
    (frames 10–35) of Walk from Animatic"."""
    c = _CLIPBOARD
    if c is None:
        return ""
    keys = sorted(c["keys"])
    what = (f"the pose at frame {keys[0]}" if len(keys) == 1
            else f"{len(keys)} keys (frames {keys[0]}\u2013{keys[-1]})")
    return f"{what} of {c['action']} from {c['source']}"


def selection_label(ob) -> str:
    """What Copy would take from ``ob`` now, in words."""
    action = ob.animation_data.action if ob.animation_data is not None else None
    if action is None:
        return ""
    found, picked = selected_keys(ob, action)
    keys = sorted(found)
    if not keys:
        return ""
    if not picked:
        return f"every key of {action.name} (nothing is selected), frames {keys[0]}\u2013{keys[-1]}"
    if len(keys) == 1:
        return f"the pose at frame {keys[0]} (the selected key)"
    return f"the {len(keys)} selected keys of {action.name}, frames {keys[0]}\u2013{keys[-1]}"


def hint(context) -> dict | None:
    """The bar's hint line just after a copy: what was copied and what to do with it."""
    c = _CLIPBOARD
    if c is None or not c.get("announce") or _busy(context):
        return None
    ob = _active_armature(context)
    if ob is not None and ob.name != c["source"]:
        return dict(id=f"copied:{c['stamp']}",
                    text=f"Copied {clipboard_label()} \u00b7 Click to paste it onto {ob.name} at frame "
                         f"{context.scene.frame_current}",
                    why="Paste puts it into this character's animation, retargeted to its skeleton, the "
                        "first copied key on the playhead. The keys already in that stretch are replaced",
                    op="animatica.paste_motion", props={})
    return dict(id=f"copied:{c['stamp']}",
                text=f"Copied {clipboard_label()} \u00b7 Now select the character to paste it onto",
                why="Select another character, put the playhead where the motion should start, and click "
                    "Paste. The rigs don't need to match",
                op=None, props={})


def _busy(context) -> bool:
    s = getattr(context.scene, "animatica", None)
    return bool(s is not None and (s.is_generating or getattr(s, "is_previewing", False)))


class ANIMATICA_OT_copy_motion(Operator):
    bl_idname = "animatica.copy_motion"
    bl_label = "Copy Motion"
    bl_description = ("Copy the selected keys of the active character (one key for a pose, many for "
                      "a stretch of motion; none selected, every key), to paste onto another "
                      "character. The rigs don't need to match: the motion is retargeted when it is pasted")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        ob = _active_armature(context)
        if ob is None:
            cls.poll_message_set("Select a character (an armature) first")
            return False
        if ob.animation_data is None or ob.animation_data.action is None:
            cls.poll_message_set(f"{ob.name} has no animation to copy")
            return False
        return not _busy(context)

    def execute(self, context):
        ob = _active_armature(context)
        action = ob.animation_data.action
        if ob.animation_data.use_tweak_mode:
            self.report({'ERROR'}, f"{ob.name} is in NLA tweak mode; leave it first")
            return {'CANCELLED'}
        try:
            copy(ob)
        except Exception as exc:                                     # noqa: BLE001
            self.report({'ERROR'}, f"Could not read {action.name} from {ob.name}: {exc}")
            return {'CANCELLED'}
        context.window_manager.animatica_copied_motion = clipboard_label()
        from . import toolbar
        toolbar._redraw_all(context)            # Copy flashes, the hint line says what was copied
        toolbar.flash_off_later()
        say = f"Copied {clipboard_label()}. Select another character and Paste"
        toolbar.note(say)                       # from the bar, a report of its own would not show
        self.report({'INFO'}, say)
        return {'FINISHED'}


class ANIMATICA_OT_paste_motion(Operator):
    bl_idname = "animatica.paste_motion"
    bl_label = "Paste Motion"
    bl_description = ("Paste the copied keys onto the active character, retargeted to its skeleton "
                      "and proportions, into its own animation with the first copied key on the "
                      "playhead. The keys already in that stretch are replaced")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if _CLIPBOARD is None:
            cls.poll_message_set("Copy Motion from a character first")
            return False
        ob = _active_armature(context)
        if ob is None:
            cls.poll_message_set("Select the character to paste onto")
            return False
        return not _busy(context)

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        target = _active_armature(context)
        if target.animation_data is not None and target.animation_data.use_tweak_mode:
            self.report({'ERROR'}, f"{target.name} is in NLA tweak mode; leave it first")
            return {'CANCELLED'}
        if mmcp_client.needs_sign_in():
            self.report({'ERROR'}, "Sign in to Animatica to paste motion (Animatica panel → Sign in)")
            return {'CANCELLED'}
        c = _CLIPBOARD
        try:
            body = build_retarget_request(c["clip"], target)
        except Exception as exc:                                     # noqa: BLE001
            self.report({'ERROR'}, f"Could not read {target.name}'s skeleton: {exc}")
            return {'CANCELLED'}
        self._copied = dict(c)
        self._target = target
        self._at = int(context.scene.frame_current)
        self._result = self._error = None
        settings = context.scene.animatica
        settings.is_generating = True
        settings.cancel_requested = False
        settings.generation_elapsed = 0
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._worker, args=(mmcp_client.get_mmcp_url(), body),
                                        daemon=True)
        self._thread.start()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        from . import toolbar
        toolbar.note(f"Retargeting {clipboard_label()} onto {target.name}\u2026")
        self.report({'INFO'}, f"Retargeting {clipboard_label()} onto {target.name}\u2026")
        return {'RUNNING_MODAL'}

    def _worker(self, server_url: str, body: dict) -> None:
        try:
            self._result = mmcp_client.MmcpClient(server_url).retarget(body)
        except Exception as exc:                                     # noqa: BLE001
            self._error = exc

    def modal(self, context, event):
        from .operators import _tick_generation_elapsed
        settings = context.scene.animatica
        if event.type == 'ESC' or settings.cancel_requested:
            self._cleanup(context)
            self.report({'INFO'}, "Paste Motion cancelled")
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._thread.is_alive():
            _tick_generation_elapsed(context, self._start_time)
            return {'RUNNING_MODAL'}
        self._cleanup(context)
        if self._error is not None:
            code = getattr(self._error, "code", "")
            if code == "retargeting_unsupported":
                self.report({'ERROR'}, "The motion server can't retarget yet: Paste Motion needs its "
                                       "POST /retarget")
            elif code == "unauthorized":
                self.report({'ERROR'}, "Sign in to Animatica to paste motion (Animatica panel → Sign in)")
            else:
                msg = getattr(self._error, "message", "") or str(self._error)
                self.report({'ERROR'}, f"Paste Motion failed: {msg}")
            return {'CANCELLED'}
        target = properties._live_armature(self._target)
        if target is None:
            self.report({'ERROR'}, "The character was deleted while its motion was being retargeted")
            return {'CANCELLED'}
        try:
            done = bake_result(self._copied, self._result, target, self._at)
        except Exception as exc:                                     # noqa: BLE001
            self.report({'ERROR'}, f"Could not bake the retargeted motion: {exc}")
            return {'CANCELLED'}
        if _CLIPBOARD is not None:
            _CLIPBOARD["announce"] = False      # the hint line has said its piece
        frames = done["frames"]
        where = f"frame {frames[0]}" if len(frames) == 1 else f"frames {frames[0]}\u2013{frames[-1]}"
        kept = f", {done['kept']} with no counterpart keep their own" if done["kept"] else ""
        self.report({'INFO'}, f"Pasted onto {target.name} at {where}: {len(frames)} "
                              f"key{'s' if len(frames) != 1 else ''}, {done['joints']} joints{kept}")
        return {'FINISHED'}

    def _cleanup(self, context) -> None:
        settings = context.scene.animatica
        settings.is_generating = False
        settings.cancel_requested = False
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


# ═══════════════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════════════

def draw_buttons(layout, context) -> None:
    """Copy Motion and Paste Motion, side by side, with what is on the clipboard."""
    row = layout.row(align=True)
    row.operator(ANIMATICA_OT_copy_motion.bl_idname, icon='COPYDOWN')
    row.operator(ANIMATICA_OT_paste_motion.bl_idname, icon='PASTEDOWN')
    label = context.window_manager.animatica_copied_motion
    if label:
        sub = layout.row()
        sub.active = False
        sub.label(text=f"Copied: {label}", icon='ARMATURE_DATA')


class ANIMATICA_PT_copy_motion(bpy.types.Panel):
    """Copy a character's motion and paste it onto another."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animatica"
    bl_label = "Copy & Paste Motion"
    bl_idname = "ANIMATICA_PT_copy_motion"

    def draw(self, context):
        layout = self.layout
        sub = layout.row()
        sub.active = False
        sub.label(text="Copy from one character, paste onto another")
        draw_buttons(layout, context)


def _context_menu(self, context):
    layout = self.layout
    layout.separator()
    layout.operator(ANIMATICA_OT_copy_motion.bl_idname, icon='COPYDOWN')
    layout.operator(ANIMATICA_OT_paste_motion.bl_idname, icon='PASTEDOWN')


_classes = (ANIMATICA_OT_copy_motion, ANIMATICA_OT_paste_motion, ANIMATICA_PT_copy_motion)
_MENUS = ("VIEW3D_MT_object_context_menu", "VIEW3D_MT_pose_context_menu")


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.animatica_copied_motion = StringProperty(default="")
    for m in _MENUS:
        getattr(bpy.types, m).append(_context_menu)


def unregister():
    for m in _MENUS:
        getattr(bpy.types, m).remove(_context_menu)
    del bpy.types.WindowManager.animatica_copied_motion
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
