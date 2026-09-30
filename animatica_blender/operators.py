"""Blender operators for the Animatica addon (MMCP client side).

Operators:
    animatica.connect           — fetch /capabilities, populate model picker
    animatica.generate          — build request, POST /generate, bake response
    animatica.generate_pose     — single-frame variant (defined in step 8)
    animatica.accept            — keep the generated motion, release source
    animatica.reject            — strip generated samples from the preview action
    animatica.cancel            — request cancellation of an in-flight gen

The generate operator is modal: a worker thread does the blocking POST while
a 0.1 s event-timer keeps the UI responsive. There is no SSE in MMCP v1, so
the modal shows an indeterminate progress bar (we can't know how far along
the server is).
"""

from __future__ import annotations

import random
import threading
import time

import bpy
from mathutils import Vector
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator

from . import (
    blender_compat,
    constraints_ui,
    gltf_to_blender,
    mmcp_client,
    properties,
    request_builder,
)


def _live_target_armature_or_clear(settings):
    """Return the scene's target armature if it still exists; else ``None``.

    When the pointer is dangling (rig deleted), routes through
    :func:`properties.reset_target_armature_state` so every piece of scene
    state tied to the rig — picker, prompt blocks, preview flags — is
    wiped in one pass.
    """
    arm = properties._live_armature(settings.target_armature)
    if arm is not None:
        return arm
    if settings.target_armature is not None:
        properties.reset_target_armature_state(settings)
    return None


def _is_motion_bake_action(action) -> bool:
    """True for Animatica motion-bake actions (preview or committed)."""
    return (
        action is not None
        and action.name.startswith(request_builder._GENERATED_ACTION_PREFIXES)
    )


def _stash_source_action_name(settings, arm) -> None:
    """Remember the user's pre-generation action for Accept / Reject.

    Never stash a motion-bake preview as the source — that makes Regenerate
    treat generated output as the action to restore and can merge dense
    samples back onto the wrong datablock.
    """
    if settings.source_action_name:
        return
    if arm.animation_data is None or arm.animation_data.action is None:
        return
    act = arm.animation_data.action
    if _is_motion_bake_action(act):
        return
    settings.source_action_name = act.name


def _action_has_keys_outside(action, lo: int, hi: int) -> bool:
    """True when ``action`` holds keys beyond ``[lo, hi]``.

    That is what makes a generation a splice rather than a fresh bake: there
    is motion either side of the window which must survive untouched.
    """
    if action is None:
        return False
    for fc in constraints_ui.iter_action_fcurves(action):
        for kp in fc.keyframe_points:
            f = int(round(kp.co.x))
            if f < lo or f > hi:
                return True
    return False


def _build_regen_request_action(
    src: bpy.types.Action,
    preview: bpy.types.Action,
    states: list | None = None,
) -> tuple[bpy.types.Action | None, list[bpy.types.Action]]:
    """Scratch action merging ``preview`` edits into ``src`` for request build.

    Only the artist's edits come over (see preview_session.edits), not the
    take's own keys: its KEYFRAME-typed keys on the user's key frames hold the
    model's pose, and merging them overwrote the user's keys with it.

    Does not mutate ``src`` or ``preview``. Caller removes the returned
    temporaries (including the scratch) when done.
    """
    from . import preview_session
    scratch = src.copy()
    preview_session.apply_edits(
        preview_session.edits(preview) if states is None else states, scratch)
    return scratch, []


MOTION_ACTION_PREFIX = "Animatica_Motion"
POSE_ACTION_NAME     = "Animatica_Pose"

# Kept for back-compat with action references in older scenes — older bakes
# wrote to ``Proscenium_Generated``, and pre-rename bakes to
# ``Proscenium_Motion``; the prefix tuple in request_builder catches every
# generation of the naming.
GENERATED_ACTION_NAME = MOTION_ACTION_PREFIX


_NLA_TRACK_PREFIX = "Animatica: "
# Tracks written before the Proscenium -> Animatica rename carry the old
# prefix. Match on the tuple to find the kept takes in files saved by earlier
# versions; new tracks are always written with the new one.
_NLA_TRACK_PREFIXES = (_NLA_TRACK_PREFIX, "Proscenium: ")


def _action_name_for_prompt(prompt: str) -> str:
    """Build an action name from a single block's prompt, clamped to fit
    Blender's 63-char action-name limit (with room left for ``.001`` auto
    suffixing).
    """
    label = (prompt or "").strip()
    name = f"{MOTION_ACTION_PREFIX}: {label}" if label else MOTION_ACTION_PREFIX
    MAX = 56
    if len(name) > MAX:
        name = name[:MAX - 1] + "…"
    return name


def _build_motion_action_name(prompt_blocks) -> str:
    """Descriptive Blender action name for a single-action motion bake.

    Picks the first enabled prompt block's text as the descriptive suffix.
    Used for the single-block / single-action fallback path; multi-block
    bakes call ``_action_name_for_prompt`` per block instead.
    """
    for b in prompt_blocks or ():
        if not getattr(b, "enabled", True):
            continue
        text = (b.prompt or "").strip()
        if text:
            return _action_name_for_prompt(text)
    return MOTION_ACTION_PREFIX


def _block_ranges_for_split(prompt_blocks, gen_start: int, gen_end: int):
    """Compute per-block ``(frame_start, frame_end, action_name)`` triples
    with half-gap expansion so NLA strips abut perfectly.

    Each enabled block claims half of the gap on each side (the rest going
    to its neighbor). The first block expands left to ``gen_start``, the
    last expands right to ``gen_end``, so no model output is discarded and
    no scene frame is uncovered.

    Returns ``[]`` if fewer than 2 enabled blocks are present (caller falls
    back to single-action bake in that case).
    """
    enabled = [b for b in prompt_blocks or () if getattr(b, "enabled", True)]
    # As the request has it (request_builder.build_segments): with any block
    # prompted, an empty one is not part of the take -- the one a new
    # character's timeline starts with, left there. Counted here it split
    # the take into two strips, one of them an empty-prompt "Animatica_Motion".
    if any((b.prompt or "").strip() for b in enabled):
        enabled = [b for b in enabled if (b.prompt or "").strip()]
    enabled.sort(key=lambda b: int(b.frame_start))
    if len(enabled) < 2:
        return []

    ranges: list[tuple[int, int, str]] = []
    for i, b in enumerate(enabled):
        fs = int(b.frame_start)
        fe = int(b.frame_end)
        if i == 0:
            fs = min(fs, gen_start)
        else:
            prev_fe = int(enabled[i - 1].frame_end)
            # Midpoint between this block's start and previous block's end —
            # the +1 keeps strips non-overlapping when a gap has odd length.
            fs = (prev_fe + fs) // 2 + 1
        if i == len(enabled) - 1:
            fe = max(fe, gen_end)
        else:
            next_fs = int(enabled[i + 1].frame_start)
            fe = (fe + next_fs) // 2

        ranges.append((fs, fe, _action_name_for_prompt(b.prompt)))
    return ranges


#: Blender's own name for the muted track an action is stashed on (the Action
#: editor's Stash, and what it does when an action is swapped out).
_STASH_TRACK_NAME = "[Action Stash]"
#: on an action Accept put on the NLA: a take the artist kept
_KEPT_KEY = "animatica_kept"
_TWEAK_MODE_MESSAGE = "Exit NLA tweak mode (Tab in the NLA editor) first"


def _in_tweak_mode(arm) -> bool:
    ad = getattr(arm, "animation_data", None) if arm is not None else None
    return bool(ad is not None and getattr(ad, "use_tweak_mode", False))


def _refuse_in_tweak_mode(op, arms) -> bool:
    """True (and says so) when any of *arms* is in NLA tweak mode.

    In tweak mode the rig's action is the strip being tweaked, and Blender
    refuses to have it replaced: a take baked, kept or thrown away then either
    failed half way or went into an accepted take's action instead."""
    for arm in arms:
        if _in_tweak_mode(arm):
            op.report({'ERROR'}, f"{arm.name}: {_TWEAK_MODE_MESSAGE}"
                      if len(arms) > 1 else _TWEAK_MODE_MESSAGE)
            return True
    return False


def _clip_label(action) -> str:
    """What a take is called on the NLA: its prompt (the action's name
    without the addon's prefix or Blender's ``.001``)."""
    import re as _re
    name = action.name
    for prefix in request_builder._GENERATED_ACTION_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip(":").strip()
            break
    name = _re.sub(r"\.\d{3}$", "", name).strip()
    return name or "Motion"


def _unique_track_name(nla, base: str) -> str:
    base = base[:59]
    taken = {t.name for t in nla}
    if base not in taken:
        return base
    n = 1
    while f"{base}.{n:03d}" in taken:
        n += 1
    return f"{base}.{n:03d}"


def _new_strip(track, name, action, arm, start: float):
    """One strip of *action* on *track*, starting at frame *start* exactly
    (``strips.new`` takes a whole frame), playing the rig's slot."""
    strip = track.strips.new(name=name[:63], start=int(start // 1), action=action)
    if abs(strip.frame_start - start) > 1e-6:
        strip.frame_start_ui = float(start)
    slot = constraints_ui.rig_slot(action, arm)
    if slot is not None and hasattr(strip, "action_slot"):
        try:
            strip.action_slot = slot
        except (TypeError, RuntimeError, AttributeError):
            pass
    return strip


def _stash_action(armature_obj, action):
    """Keep *action* on the rig's NLA the way Blender keeps an action it takes
    off a rig: a strip on a muted ``[Action Stash]`` track, stashes kept
    together below the rest. It plays nothing, and a save keeps it: Accept
    takes the artist's own action off the rig, and with no user of its own it
    was gone the next time the file was saved. Nothing is added for an action
    already on the NLA. Returns the track, or None."""
    if action is None:
        return None
    ad = armature_obj.animation_data or armature_obj.animation_data_create()
    nla = ad.nla_tracks
    for t in nla:
        if any(st.action == action for st in t.strips):
            return None
    stashes = [t for t in nla if t.name.startswith(_STASH_TRACK_NAME)]
    # Python can only add a track above another one: with no stash yet, the
    # first stash is at the bottom only when the NLA is empty. Muted, where
    # it sits changes nothing that plays.
    track = nla.new(prev=stashes[-1]) if stashes else nla.new()
    track.name = _unique_track_name(nla, _STASH_TRACK_NAME)
    try:
        start = float(action.frame_range[0])
        _new_strip(track, action.name, action, armature_obj, start)
    except (RuntimeError, TypeError, ValueError) as exc:
        # an action Blender will not make a strip of (nothing in it): kept by
        # a fake user instead
        nla.remove(track)
        action.use_fake_user = True
        print(f"[animatica] kept {action.name!r} with a fake user: no strip of it ({exc})")
        return None
    track.mute = True
    return track


def _push_actions_to_nla(armature_obj, actions, *, fill_end=None) -> list:
    """Put an accepted take on the NLA. Returns the new tracks.

    Each action gets a track of its own, named after its clip
    (``Animatica: <prompt>``), stacked above what is there: a take kept
    earlier stays where it is, and plays where the new one does not. A take
    split into blocks is one track per block, in timeline order, so each
    block is a clip of its own: glTF's Actions export skips a track with more
    than one strip, and an FBX export names each strip.

    Each strip sits at the frames its action was made for (the action's own
    frame range, manual for a block or a loop), replaces what is under it at
    full influence, and holds its last pose forward but not its first pose
    back: HOLD, Blender's default for a track's first strip, played the take's
    first pose over every frame before it and hid the take below.

    A loop plays on to *fill_end*: the strip runs the action past its cycle,
    and the curves' Cycles modifiers repeat it -- with offset on the root, so
    a walk keeps walking forward (a strip's own Repeat starts the action over
    each time, and the root with it).
    """
    if armature_obj.animation_data is None:
        armature_obj.animation_data_create()
    nla = armature_obj.animation_data.nla_tracks
    if not actions:
        return []

    def _sort_key(a):
        try:
            return float(a.frame_range[0])
        except Exception:
            return 0.0

    tracks = []
    for action in sorted(actions, key=_sort_key):
        label = _clip_label(action)
        track = nla.new(prev=tracks[-1]) if tracks else nla.new()
        track.name = _unique_track_name(nla, f"{_NLA_TRACK_PREFIX}{label}")
        loop = action.get("animatica_loop")
        if loop is not None and "start" in loop and "cut" in loop:
            action.use_frame_range = True
            action.frame_start = float(loop["start"])
            action.frame_end = float(loop["cut"])
            action.use_cyclic = True
        try:
            start = float(action.frame_range[0])
        except Exception:
            start = 1.0
        strip = _new_strip(track, label, action, armature_obj, start)
        # Blender 5.x ``strips.new`` returns a strip with ``influence=0`` by
        # default in some configurations — that's "the strip exists but
        # contributes nothing to the pose", which silently kills playback.
        # Force full influence so the strip drives the pose at 100%.
        strip.influence = 1.0
        strip.blend_type = 'REPLACE'
        strip.extrapolation = 'HOLD_FORWARD'
        # Disable blend-in/out ramps too — blocks abut, no soft fade needed.
        strip.blend_in = 0.0
        strip.blend_out = 0.0
        if loop is not None and fill_end is not None and fill_end > strip.action_frame_end:
            # Past the cycle: kept when the action is next edited in tweak mode.
            strip.use_sync_length = False
            strip.action_frame_end = float(fill_end) - strip.frame_start + strip.action_frame_start
        action.use_fake_user = True
        action[_KEPT_KEY] = True
        tracks.append(track)
    return tracks


def _root_location_data_path(armature_obj) -> str | None:
    """The fcurve data_path that drives the armature's root-bone location."""
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return None
    root_bone = next(
        (pb for pb in armature_obj.pose.bones if pb.parent is None),
        None,
    )
    if root_bone is None:
        return None
    return f'pose.bones["{root_bone.name}"].location'


def _animatica_motion_actions() -> list:
    """Every motion-bake action the addon owns (current and legacy names)."""
    return [
        a for a in bpy.data.actions
        if a.name.startswith(request_builder._GENERATED_ACTION_PREFIXES)
    ]


_INPLACE_CONSTRAINT_NAME = "Animatica_InPlace"


def _drop_legacy_inplace_constraint(armature_obj) -> None:
    """Remove the Limit Location constraint In place used to add (files saved
    with an older version may still carry one)."""
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return
    for pb in armature_obj.pose.bones:
        con = pb.constraints.get(_INPLACE_CONSTRAINT_NAME)
        if con is not None:
            pb.constraints.remove(con)


def _inplace_spans(armature_obj, action) -> list:
    """What In place reads, per stretch: ``(first, last, loop)``. One per prompt
    block of the preview, else the loop's cycle, else the whole action."""
    import json as _json
    raw = armature_obj.get("animatica_pending_block_ranges")
    try:
        blocks = [(int(r[0]), int(r[1])) for r in _json.loads(raw)] if raw else []
    except (TypeError, ValueError):
        blocks = []
    if len(blocks) >= 2:
        return [(fs, fe, False) for fs, fe in blocks]
    looped = action.get("animatica_loop")
    if looped is not None and "start" in looped and "cut" in looped:
        return [(int(looped["start"]), int(looped["cut"]), True)]
    lo, hi = action.frame_range
    return [(int(round(lo)), int(round(hi)), False)]


def _apply_inplace_constraint(armature_obj, enabled: bool) -> None:
    """In place on the take showing: its travel taken out, or put back.

    The travel is the path the character moves along, found from its centre of
    mass with the gait taken out and fitted with the simplest trajectory a game
    can re-apply (a line, an arc, or one of those eased); see inplace.py. Only
    that is removed. The pelvis keeps its sway and surge, heights are
    untouched, and an arc's turn comes out with it. The root used to be pinned
    on the ground plane instead, which removed the sway and surge too: a game
    moving the character at the walk's speed then saw the feet slide twice as
    much.

    Non-destructive: the original keys stay on the action, so switching off
    puts the travel back exactly. At Accept the result is kept (see
    :func:`_keep_inplace`). Only a generated take is touched, never an action
    the artist made.
    """
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return
    if _in_tweak_mode(armature_obj):
        # the action showing is a kept take's, being tweaked: not the take's
        return
    from . import inplace
    _drop_legacy_inplace_constraint(armature_obj)
    ad = armature_obj.animation_data
    action = ad.action if ad is not None else None
    if action is None:
        return
    from . import preview_session, root_edit
    edited = root_edit.find(action) is not None
    # The root keys this rewrites are the take's, not edits of the artist's.
    with preview_session.keeping_edits(action):
        if not enabled:
            if inplace.applied_mode(action) == "in_place":
                inplace.restore(armature_obj, action)
            if edited and inplace.applied_mode(action) is None and _is_motion_bake_action(action):
                # off, with an edited path: the take goes along it
                inplace.apply(armature_obj, action, bpy.context.scene,
                              _inplace_spans(armature_obj, action), mode="repath")
            return
        if inplace.applied_mode(action) == "in_place" or not _is_motion_bake_action(action):
            return
        inplace.apply(armature_obj, action, bpy.context.scene, _inplace_spans(armature_obj, action))


def _keep_inplace(armature_obj, actions) -> None:
    """Accept: the take stays in place; the original keys kept for switching back go,
    and so does an edited root trajectory's curve (the edit is in the keys now)."""
    from . import inplace, root_edit
    for a in actions:
        inplace.forget(a)
    root_edit.discard_all()
    _drop_legacy_inplace_constraint(armature_obj)


class ANIMATICA_OT_edit_root_trajectory(Operator):
    """Send the take along a new path: its root trajectory as a curve to edit"""
    bl_idname = "animatica.edit_root_trajectory"
    bl_label = "Edit Root Trajectory"
    bl_description = (
        "Turn the take's root trajectory into a Bezier curve on the floor. "
        "Editing it re-paths the take: with In place off the character moves "
        "along the curve, live as you edit (the timing along it stays the "
        "take's); with In place on the pose is unchanged and the curve is the "
        "root motion a game export gives it. Sharp new bends slide the feet"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "animatica", None)
        arm = getattr(s, "target_armature", None)
        return bool(arm is not None and arm.animation_data and arm.animation_data.action)

    def execute(self, context):
        from . import root_edit
        s = context.scene.animatica
        arm = s.target_armature
        action = arm.animation_data.action
        obj = root_edit.find(action)
        created = obj is None
        if created:
            obj = root_edit.create(arm, action, context.scene)
        if obj is None:
            self.report({'INFO'}, "This take stays on the spot: there is no root trajectory to edit")
            return {'CANCELLED'}
        s.key_pose_overlay = True
        s.key_pose_root_path = True
        if context.mode != 'OBJECT' and context.view_layer.objects.active is not None:
            bpy.ops.object.mode_set(mode='OBJECT')
        for o in context.view_layer.objects.selected:
            o.select_set(False)
        obj.hide_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        if created:
            root_edit.refresh(context.scene)
        self.report({'INFO'}, "Editing the root trajectory: move its points and handles to re-path the take; "
                              "Tab to finish")
        return {'FINISHED'}


class ANIMATICA_OT_reset_root_trajectory(Operator):
    """Go back to the fitted root trajectory"""
    bl_idname = "animatica.reset_root_trajectory"
    bl_label = "Reset Root Trajectory"
    bl_description = "Drop the edited root trajectory and go back to the one fitted from the take"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        from . import root_edit
        s = getattr(context.scene, "animatica", None)
        arm = getattr(s, "target_armature", None)
        ad = arm.animation_data if arm is not None else None
        return bool(ad and ad.action and root_edit.find(ad.action) is not None)

    def execute(self, context):
        from . import root_edit
        arm = context.scene.animatica.target_armature
        if context.mode == 'EDIT_CURVE':
            bpy.ops.object.mode_set(mode='OBJECT')
        root_edit.discard(arm.animation_data.action)
        if context.view_layer.objects.active is None or context.view_layer.objects.active == arm:
            context.view_layer.objects.active = arm
            arm.select_set(True)
        root_edit.refresh(context.scene, reuse=False)
        return {'FINISHED'}


#: per-key properties the split carries over besides the frame and value
_KEY_FLOATS = (("co", 2), ("handle_left", 2), ("handle_right", 2),
               ("back", 1), ("amplitude", 1), ("period", 1))
_KEY_ENUMS = ("interpolation", "easing", "handle_left_type", "handle_right_type", "type")


def _read_keys(fc) -> dict:
    import numpy as np
    kps = fc.keyframe_points
    n = len(kps)
    out = {}
    for name, width in _KEY_FLOATS:
        a = np.empty(n * width, dtype=np.float32)
        kps.foreach_get(name, a)
        out[name] = a.reshape(n, width) if width > 1 else a
    for name in _KEY_ENUMS:
        out[name] = [getattr(k, name) for k in kps]
    return out


def _write_keys(fc, keys: dict, index) -> None:
    import numpy as np
    kps = fc.keyframe_points
    n = len(index)
    kps.add(n)
    # the handle types first: setting a handle's type moves it
    for name in ("handle_left_type", "handle_right_type", "interpolation", "easing", "type"):
        vals = keys[name]
        for k, i in zip(kps, index):
            setattr(k, name, vals[i])
    for name, width in _KEY_FLOATS:
        kps.foreach_set(name, np.ascontiguousarray(keys[name][index]).ravel())


def _copy_fcurve_modifiers(src_fc, dst_fc) -> None:
    for m in src_fc.modifiers:
        try:
            d = dst_fc.modifiers.new(m.type)
        except (RuntimeError, TypeError):
            continue
        for _ in range(2):   # twice: a range clamps against the other end
            for prop in m.bl_rna.properties:
                ident = prop.identifier
                if prop.is_readonly or ident in ("rna_type", "type", "active"):
                    continue
                try:
                    setattr(d, ident, getattr(m, ident))
                except (AttributeError, TypeError, ValueError, RuntimeError):
                    pass


def _split_action_into_blocks(
    source_action,
    armature_obj,
    blocks,
):
    """Slice ``source_action``'s keyframes into N per-block actions.

    For each ``(frame_start, frame_end, action_name)`` in ``blocks``, builds
    a fresh action via the layered-Action API (mirrors the structure of the
    multi-block bake helper) holding the source's keys from that block. The
    keys go over whole -- handles and their types, interpolation, easing, key
    type -- with each curve's modifiers, extrapolation, mute flag and group.

    Each block plays the stretch from its start to the next block's; the first
    also everything before (the artist's keys before the take, carried into
    it), the last everything after. A block's action also holds, for every
    curve, the two keys either side of that stretch, and its manual frame
    range is the stretch: inside it the curves evaluate exactly as the take
    did as one action -- a curve running on across a block's edge, a bone
    only the artist keyed -- and its strip covers only its own frames.

    Layered-API construction (instead of ``source_action.copy()`` + trim) is
    deliberate: it sidesteps the NLA-state corruption Blender 5.x exhibits
    when an action is touched-then-detached during keyframe writes. Fresh
    actions always evaluate cleanly on NLA strips.

    Returns the list of new actions in input order.
    """
    import numpy as np
    new_actions = []
    curves = []
    for src_fc in constraints_ui.iter_action_fcurves(source_action, armature_obj):
        if not len(src_fc.keyframe_points):
            continue
        keys = _read_keys(src_fc)
        grp = src_fc.group.name if getattr(src_fc, "group", None) is not None else ""
        curves.append((src_fc, keys, np.round(keys["co"][:, 0].astype(np.float64), 4), grp))
    if not curves:
        return new_actions
    first = min(float(fr[0]) for _fc, _k, fr, _g in curves)
    last = max(float(fr[-1]) for _fc, _k, fr, _g in curves)
    starts = [float(b[0]) for b in blocks]

    for n, (fs, fe, action_name) in enumerate(blocks):
        lo = min(first, float(fs)) if n == 0 else float(fs)
        hi = max(last, float(fe)) if n == len(blocks) - 1 else starts[n + 1]
        new_a = bpy.data.actions.new(name=action_name)
        layer = new_a.layers.new(name="Layer")
        strip = layer.strips.new(type='KEYFRAME')
        slot = new_a.slots.new(id_type='OBJECT', name=armature_obj.name)
        cb = strip.channelbag(slot, ensure=True)

        for src_fc, keys, fr, grp in curves:
            # keys in [lo, hi), and two either side: what a key's automatic
            # handles are worked out from
            a = int(np.searchsorted(fr, lo, side="left"))
            b = int(np.searchsorted(fr, hi, side="left" if n < len(blocks) - 1 else "right"))
            index = np.arange(max(0, a - 2), min(len(fr), b + 2))
            if not len(index):
                continue
            new_fc = cb.fcurves.new(data_path=src_fc.data_path, index=src_fc.array_index)
            if grp:
                g = cb.groups.get(grp) or cb.groups.new(grp)
                new_fc.group = g
            # Carry the mute flag — without this the In-place toggle's live
            # effect is lost the moment the user clicks Accept (the split
            # would create fresh fcurves with mute=False, defeating the
            # user's intent).
            new_fc.mute = src_fc.mute
            new_fc.extrapolation = src_fc.extrapolation
            _write_keys(new_fc, keys, index)
            _copy_fcurve_modifiers(src_fc, new_fc)
            new_fc.update()

        new_a.use_frame_range = True
        new_a.frame_start = lo
        new_a.frame_end = max(hi, lo + 1.0)
        new_actions.append(new_a)

    return new_actions


def _stash_quota_state(settings, exc) -> None:
    """If ``exc`` is a quota-exceeded MmcpError, mirror its message and
    upgrade URL onto the scene settings so the panel can render a
    persistent banner with an "Upgrade" action. No-op for other errors."""
    if not isinstance(exc, mmcp_client.MmcpError):
        return
    if exc.code != "quota_exceeded":
        return
    settings.quota_exceeded_message = exc.message or str(exc)
    settings.quota_upgrade_url = (exc.details or {}).get("upgrade_url", "")


def _clear_quota_state(settings) -> None:
    settings.quota_exceeded_message = ""
    settings.quota_upgrade_url = ""


def _tick_generation_elapsed(context, start_time: float) -> None:
    """Advance the scene's ``generation_elapsed`` counter and nudge the
    sidebar to repaint.

    MMCP v1 has no server-side progress signal, so the panel shows elapsed
    wall-clock seconds instead of a (necessarily fake) progress bar. Called
    from each generating modal's TIMER branch. Writes only when the whole
    second changes, and tags just the 3D-View UI regions, so the overhead is
    one redraw per second rather than one per 0.1 s timer tick.
    """
    s = getattr(context.scene, "animatica", None)
    if s is None:
        return
    elapsed = int(time.time() - start_time)
    if elapsed == s.generation_elapsed:
        return
    s.generation_elapsed = elapsed
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


# ═══════════════════════════════════════════════════════════════════════════
# Connect
# ═══════════════════════════════════════════════════════════════════════════

class ANIMATICA_OT_connect(Operator):
    bl_idname = "animatica.connect"
    bl_label = "Connect"
    bl_description = (
        "Fetch GET /capabilities from the configured MMCP server. "
        "Populates the model dropdown with what the server hosts"
    )

    def execute(self, context):
        url = mmcp_client.get_mmcp_url()
        try:
            client = mmcp_client.MmcpClient(url, timeout=30)
            caps = client.capabilities(refresh=True)
        except mmcp_client.MmcpError as exc:
            mmcp_client.clear_capabilities(error=str(exc))
            self.report({'ERROR'}, f"Cannot connect to {url}: {exc}")
            return {'CANCELLED'}
        except Exception as exc:                         # noqa: BLE001 — defensive
            mmcp_client.clear_capabilities(error=str(exc))
            self.report({'ERROR'}, f"Cannot connect to {url}: {exc}")
            return {'CANCELLED'}

        mmcp_client.store_capabilities(caps)
        models = [m.get("id") for m in caps.get("models", [])]

        settings = context.scene.animatica
        if settings.model_id not in models and models:
            try:
                settings.model_id = models[0]
            except TypeError:
                pass

        for area in context.screen.areas:
            area.tag_redraw()

        self.report({'INFO'}, f"Connected. {len(models)} model(s): {', '.join(models) or '(none)'}")
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════════════════
# Generate
# ═══════════════════════════════════════════════════════════════════════════

def anchor_frames_for(req: dict, src_action, gen_start: int, gen_end: int):
    """The frames (and bones) the artist keyed, which the bake keeps typed as theirs.

    Every frame from the generated timeline is tagged ``GENERATED`` except
    these, collapsed to scene-frame space:

    1. ``pose_keyframe`` constraints — frame index is timeline-relative
       (0 = request's first frame), so shifted by *gen_start*.
    2. Every keyframe on the source action's fcurves, regardless of channel.
       This catches location-only keys (e.g. root-bone path animation) and
       scale keys that the pose_keyframe sampler filters out (it only emits
       constraints from rotation fcurves), so a hand-authored Hips path stays
       distinguishable from the generated motion afterwards.

    Not effector_target frames, and not root_path ones: a pin or a waypoint
    lives on its own object, and the rig's pose at that frame is generated.
    Tagging it KEYFRAME made Reject keep it and the next generation send it
    back as a full-body key pose — the previous take's pose, root and all,
    pinned at every pin frame.

    Only motion-bake output is excluded from 2 — pose-generator output is the
    user's authored content. The BONE each key sits on is kept, not just its
    frame: tagging by frame alone marked every bone as authored wherever any
    one was keyed, so a hips-only keyframe came back looking like a full-body
    one. A dict iterates as its frames, so frame-only consumers are unaffected.
    """
    anchor_frames: set[int] = set()
    for c in req.get("constraints", []):
        if c.get("type") == "pose_keyframe":
            anchor_frames.add(int(c["frame"]) + gen_start)
    anchor_bones: dict[int, set[str]] = {}
    if src_action is not None and not _is_motion_bake_action(src_action):
        for fc in constraints_ui.iter_action_fcurves(src_action):
            bone = constraints_ui._bone_name_from_data_path(fc.data_path)
            for kp in fc.keyframe_points:
                f = int(round(kp.co.x))
                if gen_start <= f <= gen_end:
                    anchor_frames.add(f)
                    if bone is not None:
                        anchor_bones.setdefault(f, set()).add(bone)
    return anchor_bones if anchor_bones else anchor_frames


def bake_take(context, settings, arm, result, **bake) -> tuple:
    """Bake one generation's result onto *arm* as a take. Returns (action, skipped joints).

    The first take on a character opens its preview session (see
    preview_session): what Reject puts back. If that first bake fails, the
    rig goes back to where it was -- its own action active, not an empty
    take with nothing to Reject.
    """
    from . import preview_session
    src_name = bake.get("source_action_name") or ""
    opened = preview_session.begin(
        context, arm, bpy.data.actions.get(src_name) if src_name else None)
    try:
        action, skipped = _bake_take(context, settings, arm, result, **bake)
        # What the take baked, so Reject can tell the artist's edits from it.
        preview_session.record_baseline(action)
    except Exception:
        if opened:
            preview_session.abort(context, arm)
        raise
    return action, skipped


def _bake_take(context, settings, arm, result, *, prompt_blocks, gen_start: int, gen_end: int,
               anchor_frames=None, splice_target=None, server_looped: bool = False,
               source_action_name: str = "", sample_index: int = 0) -> tuple:
    """The bake itself, for :func:`bake_take`.

    Everything a take gets between the server's answer and the preview, for one
    character: the bake (or an in-place splice), the authored keys either side
    of the window carried in, pins put back on target, the fingers, the cycle,
    In place, and the block ranges Accept splits by. Shared by Generate and by
    the batch, so a character's take is the same whichever made it. Raises on
    failure; the caller reports it.
    """
    block_ranges = (
        _block_ranges_for_split(prompt_blocks, gen_start, gen_end)
        if not request_builder.is_control_rig(arm)
        else []
    )
    # Always bake as a single action for preview — it scrubs cleanly via the
    # active-action / dopesheet display, no NLA stack required. Multi-block
    # scenes still get split into per-block actions on Accept; the block
    # ranges are stashed on the armature so the Accept handler knows what to do.
    preview_name = (
        f"{MOTION_ACTION_PREFIX}: Preview"
        if block_ranges
        else _build_motion_action_name(prompt_blocks)
    )
    # Splice in place when the rig already carries motion either side of the
    # window. Generating over a gap is an edit to an existing animation, not a
    # new take: the frames belong in the action the user is working in, so the
    # walk and the sit stay exactly where they are and nothing has to be
    # reassembled afterwards. A fresh bake is still right when there is
    # nothing to preserve.
    #
    # Not routed through here for control rigs: splice_gltf_into_action has no
    # control-rig hand-off, only bake_gltf_to_armature does.
    spliced = (
        splice_target is not None
        and not block_ranges
        and not request_builder.is_control_rig(arm)
        and _action_has_keys_outside(splice_target, gen_start, gen_end)
    )
    if spliced:
        # What Reject puts back: the action as it was, keys in the window and all.
        from . import preview_session
        preview_session.backup_before_splice(arm, splice_target)
        # A take spliced over one (a regenerate) is recorded afresh after the
        # bake: the old record would read every new key as the artist's.
        preview_session.forget_baseline(splice_target)
        # keyframe writes go through animation_data.action, so the target has
        # to be the active one.
        arm.animation_data.action = splice_target
        gltf_to_blender.splice_gltf_into_action(
            result,
            arm,
            splice_target,
            sample_index=sample_index,
            request_start_frame=gen_start,
            target_range=(gen_start, gen_end),
            anchor_frames=anchor_frames,
        )
        action = splice_target
        # Accept and Reject both need to know this was an in-place edit: there
        # is nothing to push, and Reject restores the copy kept above.
        arm["animatica_spliced_in_place"] = True
        # A cycle the action was once made into is not one now a stretch of it
        # was rewritten; the marker would still label it a seamless loop and
        # make In place read it as one. (Reject's copy keeps it.)
        if "animatica_loop" in action:
            del action["animatica_loop"]
    else:
        action = gltf_to_blender.bake_gltf_to_armature(
            result,
            arm,
            sample_index=sample_index,
            action_name=preview_name,
            start_frame=gen_start,
            anchor_frames=anchor_frames,
        )
    skipped = list(action.get("animatica_skipped_joints") or [])
    # The server's travel trajectory for this sample, if it sent one (MMCP
    # supports_trajectory): In place uses it (see inplace.py).
    samples = gltf_to_blender.read_extension_metadata(result).get("samples") or []
    traj = samples[sample_index].get("trajectory") if 0 <= sample_index < len(samples) else None
    if isinstance(traj, dict) and traj.get("position") and not spliced:
        import json as _json
        action["animatica_server_trajectory"] = _json.dumps({**traj, "frame_start": int(gen_start)})
    elif "animatica_server_trajectory" in action:
        del action["animatica_server_trajectory"]

    # Splice, don't replace. The bake covers only the window the prompt blocks
    # asked for, so on its own the preview would be that window and nothing
    # else — every pose authored before or after it gone from view. Carry those
    # back in so Generate reads as "this stretch changed" rather than
    # "everything else was thrown away". Reject is unaffected: these keep their
    # original type, and it strips only GENERATED ones.
    from . import preview_session
    src_action = (preview_session.source_of(arm) if preview_session.get(arm) is not None
                  else bpy.data.actions.get(source_action_name) if source_action_name else None)
    if not spliced and src_action is not None and src_action is not action:
        carried = constraints_ui.carry_keyframes_outside_range(
            src_action, action, (gen_start, gen_end),
        )
        if carried:
            print(f"[animatica] carried {carried} authored key(s) from "
                  f"outside {gen_start}..{gen_end} into the preview")

    # Pins land where they were put, on this rig, whatever the server's
    # retarget did to them (see pin_fix).
    from . import pin_fix
    fixed = pin_fix.apply(arm, action, context.scene, (gen_start, gen_end))
    if fixed:
        print("[animatica] pins put back on target: "
              + ", ".join(f"{j.split(':')[-1]}@{f} was {cm} cm off" for j, f, cm in fixed))
    # Fingers: the model has none, so each hand gets its pose laid on.
    from . import hand_pose
    hand_pose.apply(arm, action, settings, (gen_start, gen_end))
    # A cycle, if the model sampled one: last, so it repeats the motion as it
    # will play.
    # Not on a splice: there the take is a stretch of the user's own action,
    # and making a cycle would rewrite their keys and the scene range with no
    # Reject to take it back.
    if server_looped and spliced:
        print("[animatica] loop: generated into a gap in the action, left as it is (not made a cycle)")
    elif server_looped:
        from . import loop
        done = loop.apply(arm, action, (gen_start, gen_end))
        if done:
            # Play the cycle: its last frame is its first again, so the range
            # stops one short of it and playback wraps onto it.
            cycle = done["frames"]
            context.scene.frame_start = gen_start
            context.scene.frame_end = gen_start + cycle - 1
            # Reject puts the scene's own range back.
            from . import preview_session
            preview_session.update(arm, loop_range=[int(context.scene.frame_start),
                                                    int(context.scene.frame_end)])
            print(f"[animatica] loop: {cycle} frames, seam {done['seam_deg']:.1f} deg, "
                  f"turned {done['turned_deg']:.1f} deg straight")

    if block_ranges:
        # Stash split metadata for Accept. Blender's ID-property arrays are
        # homogeneous numerics-only, which rules out a list-of-(int, int, str)
        # shape: JSON-encode into a string custom prop instead — survives save/
        # reload, decodes cheaply on Accept.
        import json as _json
        arm["animatica_pending_block_ranges"] = _json.dumps([
            [int(fs), int(fe), str(name)] for fs, fe, name in block_ranges
        ])
    elif "animatica_pending_block_ranges" in arm:
        # Single-block / control-rig path: drop any stale stash from a prior
        # multi-block preview that is being overwritten in place.
        del arm["animatica_pending_block_ranges"]

    # In place, if it was on when the bake completed: the travel comes out now,
    # block by block (hence after the stash above). Toggling it afterwards does
    # the same live via its update callback.
    _apply_inplace_constraint(arm, enabled=bool(getattr(settings, "inplace", False)))
    return action, skipped


class ANIMATICA_OT_generate(Operator):
    bl_idname = "animatica.generate"
    bl_label = "Generate Motion"
    bl_description = (
        "Make motion from the prompts on the Timeline and the poses you keyed. "
        "Each run is a new take unless you lock a seed in Settings"
    )
    # One undo step for the take, pushed when the bake finishes: Ctrl+Z after
    # a generation goes back to before it, not one step further.
    bl_options = {'REGISTER', 'UNDO'}

    _timer = None
    _thread: threading.Thread | None = None
    _result: dict | None = None
    _error: Exception | None = None
    _anchor_frames: set[int] | None = None
    _regen_scratch_actions: list | None = None
    _regen_src = None
    _regen_preview = None
    _start_time: float = 0.0

    def execute(self, context):
        settings = context.scene.animatica
        # The action on the rig when Generate was pressed. If it already holds
        # motion either side of the window, the generation is a splice and the
        # frames go back into THIS action rather than a new one — captured
        # before any regen bookkeeping swaps the active action out.
        _arm0 = settings.target_armature
        self._splice_target = (
            _arm0.animation_data.action
            if _arm0 is not None and _arm0.animation_data and _arm0.animation_data.action
            else None
        )

        if settings.is_generating:
            self.report({'WARNING'}, "Already generating — wait or click Cancel")
            return {'CANCELLED'}

        arm = _live_target_armature_or_clear(settings)
        if arm is None:
            self.report(
                {'ERROR'},
                "Set a target armature first (or the previous rig was deleted)",
            )
            return {'CANCELLED'}
        if _refuse_in_tweak_mode(self, [arm]):
            return {'CANCELLED'}

        model_caps = mmcp_client.cached_model(settings.model_id)
        if model_caps is None:
            self.report({'ERROR'}, "Connect to the server first (Animatica panel → Connect)")
            return {'CANCELLED'}

        # Regenerate path: build the request from a scratch merge of preview
        # edits into the source action. The real merge runs only after a
        # successful bake — merging here used to corrupt the source when the
        # POST or bake failed, and left no preview to fall back to.
        self._regen_scratch_actions = []
        self._regen_src = None
        self._regen_preview = None
        self._regen_edits = []

        # The user's action, as the take's preview session keeps it (by
        # reference: renamed meanwhile, it is still the one); by name for a
        # take from an older version. A session that no longer holds is
        # dropped first (see preview_session.stale).
        from . import preview_session
        if preview_session.get(arm) is not None:
            why = preview_session.stale(context, arm)
            if why:
                preview_session.drop(arm, why)
        src_name = settings.source_action_name
        if preview_session.get(arm) is not None:
            src = preview_session.source_of(arm)
            src_name = src.name if src is not None else ""
            settings.source_action_name = src_name
        else:
            src = bpy.data.actions.get(src_name) if src_name else None
        if src_name:
            if src is not None and _is_motion_bake_action(src):
                settings.source_action_name = ""
                src = None
            if src is not None:
                if arm.animation_data is None:
                    arm.animation_data_create()
                preview = (
                    arm.animation_data.action
                    if arm.animation_data and arm.animation_data.action
                    else None
                )
                # Keys the artist added or changed on the take showing are
                # theirs: onto what Reject restores, and typed as theirs so the
                # request follows them and a splice keeps them. (Typed over a
                # generated key they stayed GENERATED, and were thrown away.)
                # With the travel in: the user's action travels (see
                # preview_session.travel_edits).
                from . import preview_session
                self._regen_edits = preview_session.travel_edits(arm, preview)
                preview_session.fold_edits(arm, preview, self._regen_edits)
                if (
                    preview is not None
                    and preview is not src
                    and _is_motion_bake_action(preview)
                ):
                    scratch, temps = _build_regen_request_action(src, preview, self._regen_edits)
                    self._regen_scratch_actions = temps + [scratch]
                    self._regen_src = src
                    self._regen_preview = preview
                    arm.animation_data.action = scratch
                    # The new take is made against the user's action, as the
                    # first was: splicing it into the preview (which carries
                    # the user's keys either side) marked the take an in-place
                    # edit, and Reject then never gave the user's action back.
                    self._splice_target = src
                else:
                    arm.animation_data.action = src

        try:
            req = request_builder.build_request(
                model_id=settings.model_id,
                model_caps=model_caps,
                armature_obj=arm,
                prompt_blocks=settings.prompt_blocks,
                settings=settings,
                scene=context.scene,
                constraint_objects=constraints_ui.walk_scene_constraints(context.scene),
            )
        except request_builder.BuildError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        # Whether the server was asked to sample a cycle: the take then comes
        # back closed, and the bake only straightens and repeats it. Loop is
        # the model's alone; a take it did not sample as a cycle is not looped.
        self._server_looped = bool((req.get("options") or {}).get("loop"))
        if (getattr(settings, "loop", False) and not self._server_looped
                and (model_caps or {}).get("supports_loop")):
            gen_range = request_builder.compute_frame_range(settings.prompt_blocks, arm, context.scene)
            if request_builder.will_splice(arm, gen_range):
                self.report({'WARNING'}, "Loop is off for a generation into a gap between "
                                         "your keys; generating without it")
            else:
                self.report({'WARNING'}, "Loop needs a single prompt block; generating without it")

        # Save the source action for Accept / Reject (no-op if already saved).
        _stash_source_action_name(settings, arm)

        # The frames the artist keyed stay theirs after the bake (see anchor_frames_for).
        gen_start, gen_end = request_builder.compute_frame_range(
            settings.prompt_blocks, arm, context.scene
        )
        self._gen_start_frame = gen_start
        # And its end, as sent: the window depends on the keys either side of
        # the blocks (see compute_frame_range), which are not what they were
        # once the bake has swapped the rig's action.
        self._gen_end_frame = gen_end

        src_action = (
            arm.animation_data.action
            if arm.animation_data and arm.animation_data.action
            else None
        )
        self._anchor_frames = anchor_frames_for(req, src_action, gen_start, gen_end)

        # Reset state and kick worker.
        settings.is_generating = True
        settings.cancel_requested = False
        settings.generation_elapsed = 0
        self._start_time = time.time()
        self._result = None
        self._error = None
        self._thread = threading.Thread(
            target=self._worker,
            args=(mmcp_client.get_mmcp_url(), req),
            daemon=True,
        )
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # ----- thread body -----------------------------------------------------
    def _worker(self, server_url: str, req: dict) -> None:
        try:
            client = mmcp_client.MmcpClient(server_url)
            self._result = client.generate(req)
        except Exception as exc:                         # noqa: BLE001 — surfaced to UI
            self._error = exc

    # ----- modal -----------------------------------------------------------
    def modal(self, context, event):
        settings = context.scene.animatica

        if event.type == 'ESC' or settings.cancel_requested:
            self._cleanup(context)
            self.report({'INFO'}, "Generation cancelled (request still runs server-side)")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if self._thread is not None and self._thread.is_alive():
            _tick_generation_elapsed(context, self._start_time)
            return {'RUNNING_MODAL'}

        # Worker finished.
        if self._error is not None:
            self._cleanup(context)
            _stash_quota_state(context.scene.animatica, self._error)
            self.report({'ERROR'}, f"Generation failed: {self._error}")
            return {'CANCELLED'}

        if self._result is None:
            self._cleanup(context)
            self.report({'ERROR'}, "Worker exited with no result")
            return {'CANCELLED'}

        # Successful run — clear any stale quota banner.
        _clear_quota_state(context.scene.animatica)

        arm = _live_target_armature_or_clear(settings)
        if arm is None:
            self._cleanup(context)
            self.report(
                {'ERROR'},
                "Target armature is missing or was deleted before the bake finished",
            )
            return {'CANCELLED'}

        # Bake. Two paths:
        #   • 2+ enabled prompt blocks → split the response into one action
        #     per block, push to NLA. Lets the user iterate on individual
        #     blocks later (and matches Blender's "strip per beat" mental
        #     model). Control-rig handling is intentionally bypassed here;
        #     fall back to the single-action path for control rigs.
        #   • Otherwise → existing single-action bake (handles control rigs
        #     via the Mixamo operator hand-off).
        gen_start = getattr(self, "_gen_start_frame", context.scene.frame_start)
        gen_end_settings_scene_frame = context.scene.frame_end
        gen_end = getattr(self, "_gen_end_frame", None)
        if gen_end is None:
            _, gen_end = request_builder.compute_frame_range(
                settings.prompt_blocks, arm, context.scene
            )

        self._remove_regen_scratch_actions()

        n_actions = 0
        skipped: list[str] = []
        try:
            action, skipped = bake_take(
                context, settings, arm, self._result,
                prompt_blocks=settings.prompt_blocks,
                gen_start=gen_start, gen_end=gen_end,
                anchor_frames=getattr(self, "_anchor_frames", None),
                splice_target=getattr(self, "_splice_target", None),
                server_looped=getattr(self, "_server_looped", False),
                source_action_name=settings.source_action_name,
            )
            n_actions = 1
            # Variations: keep the answer, so the others can be shown in turn.
            import json as _json
            from types import SimpleNamespace
            from . import variations
            variations.remember(
                arm, self._result, action,
                prompt_blocks=[SimpleNamespace(**d) for d in _json.loads(
                    properties._serialize_blocks(settings.prompt_blocks))],
                gen_start=gen_start, gen_end=gen_end,
                anchor_frames=getattr(self, "_anchor_frames", None),
                splice_target_name=(getattr(self, "_splice_target", None) or SimpleNamespace(name="")).name,
                server_looped=getattr(self, "_server_looped", False),
                source_action_name=settings.source_action_name,
            )

            # The artist's edits on the take replaced are on the source already
            # (execute); again here for a take from an older version, which
            # has no session to fold them into. The replaced take goes.
            if self._regen_src is not None and self._regen_preview is not None:
                from . import preview_session
                preview_session.apply_edits(getattr(self, "_regen_edits", []), self._regen_src)
                old = self._regen_preview
                if old is not action and old.users == 0 and not old.use_fake_user:
                    bpy.data.actions.remove(old)
                self._clear_regen_state()
        except Exception as exc:                         # noqa: BLE001 — surfaced to UI
            self._cleanup(context)
            self.report({'ERROR'}, f"Bake failed: {exc}")
            return {'CANCELLED'}

        # Bake succeeded — keep the source-action reference so the
        # Accept / Reject preview UI knows what to fall back to.
        self._cleanup(context, preview=True)
        msg_suffix = f" ({n_actions} actions)" if n_actions > 1 else ""
        if skipped:
            self.report(
                {'WARNING'},
                f"Done — skipped {len(skipped)} unmatched joint(s){msg_suffix}",
            )
        else:
            self.report({'INFO'}, f"Generation complete{msg_suffix}")
        return {'FINISHED'}

    def _remove_regen_scratch_actions(self) -> None:
        for ac in self._regen_scratch_actions or ():
            try:
                if ac is not None and ac.name in bpy.data.actions:
                    bpy.data.actions.remove(ac)
            except Exception:
                pass
        self._regen_scratch_actions = []

    def _clear_regen_state(self) -> None:
        self._remove_regen_scratch_actions()
        self._regen_src = None
        self._regen_preview = None

    def _cleanup(self, context, *, preview: bool = False) -> None:
        # ``preview=True`` is set only by the success path so the Accept /
        # Reject UI can show. Every CANCELLED path leaves ``preview`` at
        # its default ``False`` and we drop the source reference here —
        # without this, a failed worker (e.g. a quota-exceeded 429) would
        # surface the preview UI even though no motion was baked.
        s = context.scene.animatica
        arm = _live_target_armature_or_clear(s)

        regen_preview = self._regen_preview

        # Restore the armature to the live preview if a failed regen left
        # it on a scratch action.
        if (
            not preview
            and arm is not None
            and regen_preview is not None
            and arm.animation_data is not None
        ):
            arm.animation_data.action = regen_preview

        self._clear_regen_state()

        if not preview:
            from . import preview_session
            still_previewing = arm is not None and (
                # a take spliced into the user's action is not a motion bake
                preview_session.get(arm) is not None
                or (
                    arm.animation_data is not None
                    and arm.animation_data.action is not None
                    and _is_motion_bake_action(arm.animation_data.action)
                )
            )
            if not still_previewing:
                s.source_action_name = ""
                s.is_previewing = False
        else:
            # Source-action name may be empty for free-form generations
            # (no prior action to fall back to); ``is_previewing`` tracks
            # the preview UI independently so the panel still surfaces.
            s.is_previewing = True
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        s.is_generating = False
        s.cancel_requested = False


# ═══════════════════════════════════════════════════════════════════════════
# Cancel
# ═══════════════════════════════════════════════════════════════════════════

class ANIMATICA_OT_cancel_generation(Operator):
    bl_idname = "animatica.cancel"
    bl_label = "Cancel"
    bl_description = (
        "Stop waiting on the in-flight generation. The HTTP request continues "
        "server-side; the addon discards whatever comes back"
    )

    def execute(self, context):
        s = context.scene.animatica
        if not s.is_generating:
            return {'CANCELLED'}
        s.cancel_requested = True
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════════════════
# Preview: Accept / Reject
# ═══════════════════════════════════════════════════════════════════════════

def _remove_orphan_takes(*, past_fake_user: bool = False) -> int:
    """Drop the takes nothing uses: one thrown away, a version not chosen, a
    take regenerated over. Left, they went into an FBX export of all actions.
    A take the artist kept (Accept marks it) stays even with nothing using it
    -- its NLA track deleted, say -- and so does a pose-generator action,
    which is the artist's. ``past_fake_user``: a fake user alone does not
    keep one either (Reject: older versions gave takes on show one). Returns
    how many went."""
    def _is_orphan(a):
        real_users = a.users - (1 if a.use_fake_user and past_fake_user else 0)
        return real_users <= 0

    gone = [a for a in bpy.data.actions
            if a.name.startswith(request_builder._GENERATED_ACTION_PREFIXES)
            and a.library is None and not a.get(_KEPT_KEY) and _is_orphan(a)]
    for a in gone:
        bpy.data.actions.remove(a)
    return len(gone)


class ANIMATICA_OT_accept(Operator):
    bl_idname = "animatica.accept"
    bl_label = "Accept"
    bl_description = (
        "Keep this take. It goes onto the NLA as a track of its own, above "
        "the takes kept before, which stay; your own action is stashed there too"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.animatica
        return s.is_previewing and not s.is_generating

    def execute(self, context):
        s = context.scene.animatica
        arm = _live_target_armature_or_clear(s)
        if arm is not None and _refuse_in_tweak_mode(self, [arm]):
            return {'CANCELLED'}
        from . import preview_session, variations
        variations.forget(arm)
        tracks = []

        if arm is not None:
            import json as _json
            if arm.get("animatica_spliced_in_place"):
                # The frames went straight into the user's own action, so
                # there is nothing to assemble: accepting just means keeping
                # it. Pushing to NLA here would detach the action they are
                # working in and hand back a strip instead.
                del arm["animatica_spliced_in_place"]
                _drop_legacy_inplace_constraint(arm)
                if "animatica_pending_block_ranges" in arm:
                    del arm["animatica_pending_block_ranges"]
                # The take's frames stay typed GENERATED, so the next request
                # does not read them as key poses; a later Reject restores its
                # own copy of the action, which has them, instead of stripping
                # GENERATED keys. Keys the artist typed over them are theirs.
                preview_session.promote_edits(arm.animation_data.action)
                # What the take shows is kept, as a normal Accept keeps it: the
                # travel re-pathed onto an edited trajectory stays in the keys,
                # the original keys kept for switching back go, and so does the
                # curve. Left, the curve went on rewriting the accepted action,
                # and the next take's In place put those old keys back over it.
                _keep_inplace(arm, [arm.animation_data.action])
                preview_session.finish(context, arm, accepted=True)
                s.source_action_name = ""
                s.is_previewing = False
                _clear_quota_state(s)
                self.report({'INFO'}, "Kept the generated frames in "
                                      f"'{arm.animation_data.action.name}'")
                return {'FINISHED'}

            pending_raw = arm.get("animatica_pending_block_ranges")
            try:
                pending = _json.loads(pending_raw) if pending_raw else []
            except (TypeError, ValueError):
                pending = []
            block_ranges = [
                (int(r[0]), int(r[1]), str(r[2]))
                for r in pending
                if len(r) >= 3
            ]

            preview_action = (
                arm.animation_data.action
                if arm.animation_data and arm.animation_data.action
                else None
            )

            actions_to_push: list = []
            # The artist's own action, which the take showed in place of.
            source = preview_session.source_of(arm) or (
                bpy.data.actions.get(s.source_action_name) if s.source_action_name else None)
            if source is not None and (source == preview_action or _is_motion_bake_action(source)):
                source = None
            session = preview_session.get(arm) or {}
            fill_end = max([int(context.scene.frame_end)]
                           + [int(f) for f in (session.get("frame_range") or [])[1:2]])
            preview_session.promote_edits(preview_action)
            preview_session.forget_baseline(preview_action)

            # In place: the take is already in place if the toggle was on
            # while it showed; make sure of it before it is split into blocks.
            if bool(getattr(s, "inplace", False)) and preview_action is not None:
                _apply_inplace_constraint(arm, enabled=True)

            if len(block_ranges) >= 2 and preview_action is not None:
                # Multi-block: build the per-block actions from the preview's
                # fcurves, drop the preview, push the splits.
                actions_to_push = _split_action_into_blocks(
                    preview_action, arm, block_ranges,
                )
                # each block keeps its share of the root motion (a game export reads it)
                from . import inplace
                inplace.carry(preview_action, actions_to_push, block_ranges)
                arm.animation_data.action = None
                bpy.data.actions.remove(preview_action)
            elif (
                preview_action is not None
                and preview_action.name.startswith(
                    request_builder._GENERATED_ACTION_PREFIXES
                )
            ):
                # Single-block: push the preview as-is (it's already the
                # final, prompt-named motion action). Detach from the
                # armature so NLA evaluation isn't shadowed by an active
                # action of the same content.
                actions_to_push = [preview_action]
                arm.animation_data.action = None

            if actions_to_push:
                # Taken off the rig, the artist's action is kept where Blender
                # keeps one: stashed on the NLA (it had no user left, and the
                # next save dropped it). The keys it had outside the take play
                # on in the take, which carries them.
                _stash_action(arm, source)
                tracks = _push_actions_to_nla(arm, actions_to_push, fill_end=fill_end)

            # The accepted actions keep what they show: in place or travelling.
            # The original keys kept for switching back are dropped.
            _keep_inplace(arm, actions_to_push)

            if "animatica_pending_block_ranges" in arm:
                del arm["animatica_pending_block_ranges"]
            preview_session.finish(context, arm, accepted=True)

        s.source_action_name = ""
        s.is_previewing = False
        _remove_orphan_takes()
        names = ", ".join(f"'{t.name}'" for t in tracks)
        self.report({'INFO'}, f"Take kept: it plays from the NLA ({names})" if tracks
                    else "Take kept")
        return {'FINISHED'}


class ANIMATICA_OT_reject(Operator):
    bl_idname = "animatica.reject"
    bl_label = "Reject"
    bl_description = (
        "Throw this take away and go back to what you had. Your own keys, "
        "including any you added while previewing, stay"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Nothing previewing, nothing to throw away: run anyway (F3, a
        # script) it detached the user's own action.
        s = context.scene.animatica
        return s.is_previewing and not s.is_generating

    def execute(self, context):
        s   = context.scene.animatica
        arm = _live_target_armature_or_clear(s)
        if arm is not None and _refuse_in_tweak_mode(self, [arm]):
            return {'CANCELLED'}
        from . import preview_session, root_edit, variations
        variations.forget(arm)
        root_edit.discard_all()
        if arm is None:
            self.report(
                {'ERROR'},
                "Target armature is missing or was deleted — choose an armature again",
            )
            return {'CANCELLED'}

        if arm.animation_data is None:
            arm.animation_data_create()

        # ``source`` is the pre-generation action stashed by Generate (kept by
        # the preview session, which survives a change of rig). It goes back
        # on the rig as it was, with the artist's preview edits added.
        source = preview_session.source_of(arm) or (
            bpy.data.actions.get(s.source_action_name) if s.source_action_name else None)
        preview = (
            arm.animation_data.action
            if arm.animation_data and arm.animation_data.action
            else None
        )
        is_motion_preview = (
            preview is not None
            and preview.name.startswith(request_builder._GENERATED_ACTION_PREFIXES)
        )

        # The NLA is left alone: what is on it is takes kept earlier, and
        # throwing this one away must not throw those away with it. (It
        # cleared every Animatica track: accept a wave, reject a walk, and
        # the wave was gone too.)

        # The take goes, and its in-place state with it; only a constraint
        # left by an older version needs removing.
        _drop_legacy_inplace_constraint(arm)

        # Pending-block-ranges stash is only meaningful for Accept.
        if "animatica_pending_block_ranges" in arm:
            del arm["animatica_pending_block_ranges"]

        # The keys the artist added or changed while the take showed: they
        # stay, whatever else goes (see preview_session.edits) -- with the
        # take's travel in, as the user's action has it.
        kept = preview_session.travel_edits(arm, preview)

        if arm.get("animatica_spliced_in_place"):
            # The generated frames were written into the user's action. The
            # copy kept before the splice goes back in its place: the window's
            # old keys, the handles either side, no curves the take added.
            # (Stripping GENERATED keys instead took accepted takes with it,
            # and could not bring back what the splice had replaced.)
            del arm["animatica_spliced_in_place"]
            restored = preview_session.restore_backup(arm)
            if restored is not None:
                preview_session.apply_edits(kept, restored)
                removed = None
            else:   # spliced by an older version: no copy
                removed = constraints_ui.strip_generated_keyframe_points(
                    preview, promote_unauthored=False,
                )
            name = arm.animation_data.action.name if arm.animation_data.action else ""
            preview_session.finish(context, arm, accepted=False)
            s.source_action_name = ""
            s.is_previewing = False
            _clear_quota_state(s)
            self.report({'INFO'},
                        (f"Removed {removed} generated sample(s); " if removed is not None else "")
                        + f"'{name}' is back as it was")
            return {'FINISHED'}

        n_rm = 0
        if is_motion_preview:
            if source is not None:
                # The user's action as it was, plus their edits. Nothing else
                # of the preview comes over: its KEYFRAME-typed keys on the
                # user's key frames hold the model's values, and copying them
                # back overwrote the user's keys (and added channels to bones
                # they never keyed).
                n_rm = sum(len(fc.keyframe_points)
                           for fc in constraints_ui.iter_action_fcurves(preview)) - len(kept)
                preview_session.apply_edits(kept, source)
                arm.animation_data.action = source
            else:
                # Widen authored frames to the whole body only when the preview
                # is about to become the action outright.
                n_rm = constraints_ui.strip_generated_keyframe_points(
                    preview, promote_unauthored=True,
                )
                # With nothing of the artist's left in it, it is not theirs to
                # keep: the rig goes back to having no action, as it had.
                has_keys = any(len(fc.keyframe_points)
                               for fc in constraints_ui.iter_action_fcurves(preview, arm))
                arm.animation_data.action = preview if has_keys else None
        else:
            # Unexpected: preview isn't a motion bake. Fall back to assigning
            # whatever ``source`` we have (or detach if none).
            arm.animation_data.action = source

        # The pose of every bone no key drives, the scene's frame range, the
        # source's own fake-user flag: back as they were before Generate.
        preview_session.finish(context, arm, accepted=False)

        _remove_orphan_takes(past_fake_user=True)

        s.source_action_name = ""
        s.is_previewing = False

        if is_motion_preview and source is not None:
            self.report(
                {'INFO'},
                f"Restored {source.name!r}; kept {len(kept)} key(s) you edited, "
                f"removed {n_rm} generated sample(s)",
            )
        elif is_motion_preview:
            self.report({'INFO'}, f"Removed {n_rm} generated sample(s)")
        elif source is not None:
            self.report({'INFO'}, f"Restored source action {source.name!r}")
        else:
            self.report({'INFO'}, "Discarded preview")
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════════════════
# Pose generator (single keyframe at current frame, additive)
# ═══════════════════════════════════════════════════════════════════════════

#: The same ladder as the motion examples, for shapes rather than motion:
#: symmetric and upright first, then a change of level, then asymmetry, then
#: a pose that has to hold its own balance, then contact with the floor. Each
#: is a shape — this generates one frame.
EXAMPLE_POSES = (
    ("a person stands with both arms raised overhead", "Arms overhead"),
    ("a person crouches low", "Crouch"),
    ("a person reaches up with their right hand", "Reach up with one arm"),
    ("a person leans forward with hands on knees", "Hands on knees"),
    ("a person in a boxing guard, fists up", "Boxing guard"),
    ("a person sits cross-legged on the floor", "Sit cross-legged"),
)


def _pose_example_items(self, context):
    return [(prompt, label, prompt) for prompt, label in EXAMPLE_POSES]


class ANIMATICA_OT_use_example_pose(Operator):
    bl_idname = "animatica.use_example_pose"
    bl_label = "Use Example Pose"
    bl_description = "Put this example in the prompt field"
    bl_options = {"REGISTER", "INTERNAL"}

    example: StringProperty()

    def execute(self, context):
        # The dialog reads its pre-fill from last_pose_prompt on open, so
        # writing there and reopening is how an example lands in the field.
        context.scene.animatica.last_pose_prompt = self.example
        return bpy.ops.animatica.generate_pose('INVOKE_DEFAULT')


class ANIMATICA_MT_example_poses(bpy.types.Menu):
    bl_idname = "ANIMATICA_MT_example_poses"
    bl_label = "Try an Example"

    def draw(self, context):
        layout = self.layout
        for prompt, label in EXAMPLE_POSES:
            layout.operator("animatica.use_example_pose",
                            text=label).example = prompt


class ANIMATICA_OT_generate_pose(Operator):
    bl_idname = "animatica.generate_pose"
    bl_label = "Generate Pose at Current Frame"
    bl_description = (
        "Generate a single pose from text and insert it as a keyframe at the "
        "current scene frame. Requires a server that advertises the 'pose' "
        "segment type (Animatica Cloud). Non-destructive — undo with Ctrl+Z"
    )

    prompt: StringProperty(
        name="Prompt",
        description="Text describing the pose to generate",
        # Empty on purpose. A pre-filled "a person stands in a neutral pose"
        # is a prompt nobody chose: press Generate on it and the character
        # goes to a pose indistinguishable from doing nothing, which reads as
        # the feature being broken. The examples below are the way in.
        default="",
    )
    seed: IntProperty(name="Seed", default=42, min=0, max=999999)
    preserve_height: BoolProperty(
        name="Preserve height",
        description=(
            "Keep the root's current world height. When unchecked (default), "
            "the root's world Z is overridden by the generated pose's height "
            "(XY stays put), so a 'crouching' pose actually drops the character "
            "toward the floor and a 'jumping' pose lifts them"
        ),
        default=False,
    )
    pose_apply_scope: EnumProperty(
        name="Apply pose to",
        description="Which bones receive keyframes from the generated pose",
        items=(
            (
                "ALL",
                "All bones",
                "Keyframe every joint in the server response",
            ),
            (
                "SELECTED",
                "Selected bones",
                "Only keyframe bones that are selected on the target armature in "
                "Pose mode (IK / control handles on a Mixamo rig expand to their "
                "driving deform bones)",
            ),
        ),
        default="ALL",
    )

    # NOTE: keep these as plain class attributes (no type hints). Blender's
    # operator-registration walks ``__annotations__`` to resolve the class's
    # ``StringProperty``/``IntProperty``/``BoolProperty`` declarations into
    # RNA properties; under ``from __future__ import annotations`` every
    # annotation is a string and Blender's resolver chokes on the non-Property
    # ones (e.g. ``threading.Thread | None``), dropping ALL annotations for
    # the class — which makes the whole dialog go blank.
    _timer = None
    _thread = None
    _result = None
    _error = None
    _target_frame = 1
    _pose_joint_filter = None
    _start_time = 0.0
    # Side channel for the in-dialog Randomize button. The child operator
    # can't reach this instance's ``self.seed`` directly, so it writes the
    # new value here and ``draw`` picks it up on the next redraw.
    _pending_seed = None

    @classmethod
    def poll(cls, context):
        # Hide the operator entirely when the connected model doesn't claim
        # 'pose' segment support — text-to-pose is a cloud-only capability.
        s = context.scene.animatica
        model_caps = mmcp_client.cached_model(s.model_id) if s.model_id else None
        if model_caps is None:
            return False
        return "pose" in (model_caps.get("supported_segments") or [])

    # ----- UI --------------------------------------------------------------
    def invoke(self, context, event):
        s = context.scene.animatica
        # Pre-fill from the most recent pose prompt the user submitted in
        # this scene; falls back to the property's default on first use.
        last = getattr(s, "last_pose_prompt", "")
        if last:
            self.prompt = last
        self.seed = int(s.seed)
        type(self)._pending_seed = None
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        # Pick up any seed the in-dialog Randomize button stashed since the
        # last redraw. Cleared after consumption so a stale value can't leak
        # into a future dialog open.
        cls = type(self)
        if cls._pending_seed is not None:
            self.seed = cls._pending_seed
            cls._pending_seed = None
        layout = self.layout
        layout.prop(self, "prompt")
        if not (self.prompt or "").strip():
            layout.menu("ANIMATICA_MT_example_poses",
                        text="Try an example", icon='PRESET')
        row = layout.row(align=True)
        row.prop(self, "seed")
        row.operator("animatica.randomize_pose_seed", text="", icon='FILE_REFRESH')
        layout.prop(self, "preserve_height")
        layout.prop(self, "pose_apply_scope")
        layout.label(text=f"Insert keyframe at frame {context.scene.frame_current}")

    # ----- entry -----------------------------------------------------------
    def execute(self, context):
        s = context.scene.animatica
        if s.is_generating:
            self.report({'WARNING'}, "Already generating — wait or click Cancel")
            return {'CANCELLED'}

        arm = _live_target_armature_or_clear(s)
        if arm is None:
            self.report(
                {'ERROR'},
                "Set a target armature first (or the previous rig was deleted)",
            )
            return {'CANCELLED'}

        model_caps = mmcp_client.cached_model(s.model_id)
        if model_caps is None:
            self.report({'ERROR'}, "Connect to the server first")
            return {'CANCELLED'}

        if "pose" not in (model_caps.get("supported_segments") or []):
            self.report({'ERROR'},
                        "Server does not advertise the 'pose' segment type. "
                        "Pose generation is an Animatica Cloud feature.")
            return {'CANCELLED'}

        # Persist the prompt to the scene so the next dialog open pre-fills
        # with what the user just submitted — kept here (after capability
        # checks pass) so a typo + cancel doesn't overwrite the previous
        # known-good prompt.
        s.last_pose_prompt = self.prompt

        # Send the user's own armature skeleton — the server retargets it to
        # the canonical on the way in and back again on the way out.
        request_skeleton = request_builder.armature_to_skeleton(arm)

        if not model_caps.get("supports_retargeting", True):
            canonical_joints = {j["name"] for j in model_caps["canonical_skeleton"]["joints"]}
            missing = canonical_joints - {pb.name for pb in arm.pose.bones}
            if missing:
                self.report({'ERROR'},
                            f"Server does not support retargeting and the armature is "
                            f"missing {len(missing)} canonical joint(s). Re-import via "
                            f"'Import canonical skeleton'")
                return {'CANCELLED'}
            request_skeleton = model_caps["canonical_skeleton"]

        # Single PoseSegment — server's specialized text-to-pose model returns
        # a 1-frame glTF directly. No client-side middle-frame extraction.
        req = {
            "protocol_version": request_builder.PROTOCOL_VERSION,
            "model":            s.model_id,
            "skeleton":         request_skeleton,
            "segments": [{
                "type":   "pose",
                "prompt": self.prompt,
            }],
            "options": {
                "diffusion_steps": request_builder.QUALITY_PRESETS.get(
                    s.quality_preset, int(s.custom_steps)
                ),
                "num_samples":     1,
                "seed":            int(self.seed) if int(self.seed) > 0 else None,
                "post_processing": bool(s.post_processing),
            },
        }

        self._target_frame = int(context.scene.frame_current)

        self._pose_joint_filter = None
        if self.pose_apply_scope == "SELECTED":
            selected = {
                pb.name for pb in arm.pose.bones if blender_compat.pose_bone_is_selected(pb)
            }
            if not selected:
                self.report(
                    {"ERROR"},
                    'Pose scope is "Selected bones" but no bones are selected on '
                    "the target armature — open Pose mode and select one or more bones",
                )
                return {"CANCELLED"}
            self._pose_joint_filter = frozenset(
                gltf_to_blender.resolve_pose_bake_joint_names(arm, selected)
            )

        s.is_generating = True
        s.cancel_requested = False
        s.generation_elapsed = 0
        self._start_time = time.time()
        self._result = None
        self._error = None
        self._thread = threading.Thread(
            target=self._worker,
            args=(mmcp_client.get_mmcp_url(), req),
            daemon=True,
        )
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # ----- thread body -----------------------------------------------------
    def _worker(self, server_url: str, req: dict) -> None:
        try:
            client = mmcp_client.MmcpClient(server_url)
            self._result = client.generate(req)
        except Exception as exc:                         # noqa: BLE001
            self._error = exc

    # ----- modal -----------------------------------------------------------
    def modal(self, context, event):
        s = context.scene.animatica

        if event.type == 'ESC' or s.cancel_requested:
            self._cleanup(context)
            self.report({'INFO'}, "Pose generation cancelled")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if self._thread is not None and self._thread.is_alive():
            _tick_generation_elapsed(context, self._start_time)
            return {'RUNNING_MODAL'}

        if self._error is not None:
            self._cleanup(context)
            _stash_quota_state(context.scene.animatica, self._error)
            self.report({'ERROR'}, f"Pose generation failed: {self._error}")
            return {'CANCELLED'}

        if self._result is None:
            self._cleanup(context)
            self.report({'ERROR'}, "Worker exited with no result")
            return {'CANCELLED'}

        # Successful run — clear any stale quota banner.
        _clear_quota_state(context.scene.animatica)

        arm = _live_target_armature_or_clear(s)
        if arm is None:
            self._cleanup(context)
            self.report(
                {'ERROR'},
                "Target armature is missing or was deleted before the pose bake finished",
            )
            return {'CANCELLED'}

        n_frames = gltf_to_blender.sample_frame_count(self._result, sample_index=0)
        if n_frames < 1:
            self._cleanup(context)
            self.report({'ERROR'}, "Response had no frames")
            return {'CANCELLED'}

        # PoseSegment yields exactly 1 frame from the server — no
        # middle-frame extraction needed; bake source_frame=0.
        try:
            written = gltf_to_blender.bake_single_pose(
                self._result,
                arm,
                source_frame=0,
                target_frame=self._target_frame,
                sample_index=0,
                root_translation="skip" if self.preserve_height else "height_only",
                joint_name_filter=self._pose_joint_filter,
            )
        except Exception as exc:                         # noqa: BLE001
            self._cleanup(context)
            self.report({'ERROR'}, f"Bake failed: {exc}")
            return {'CANCELLED'}

        if written == 0 and self._pose_joint_filter is not None:
            self._cleanup(context)
            self.report(
                {"WARNING"},
                "No pose channels matched the selected bones (names must match "
                "skeleton joints in the response) — nothing keyframed",
            )
            return {"CANCELLED"}

        # Snap viewport to the freshly-keyframed pose.
        context.scene.frame_set(self._target_frame)

        self._cleanup(context)
        self.report({'INFO'}, f"Inserted pose: {written} channels @ frame {self._target_frame}")
        return {'FINISHED'}

    def _cleanup(self, context) -> None:
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        s = context.scene.animatica
        s.is_generating = False
        s.cancel_requested = False


# ═══════════════════════════════════════════════════════════════════════════
# Auth — Animatica Cloud sign-in / sign-out
# ═══════════════════════════════════════════════════════════════════════════
#
# Auth is NOT part of the MMCP protocol. The cloud's proxy in front of
# /generate consumes the Bearer token; self-hosted servers ignore it.
# These operators only matter when the user is pointing at the cloud.

class ANIMATICA_OT_signin(Operator):
    bl_idname = "animatica.signin"
    bl_label = "Sign in to Animatica"
    bl_description = (
        "Exchange email + password for an Animatica session token. Only "
        "needed when pointing at Animatica Cloud — self-hosted servers "
        "don't require sign-in"
    )

    email: StringProperty(name="Email", default="")
    # SKIP_SAVE: an operator property otherwise remembers its last value and
    # pre-fills it next time — the password must not outlive the dialog.
    password: StringProperty(name="Password", default="", subtype='PASSWORD',
                             options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.prop(self, "email")
        col.prop(self, "password")

    def execute(self, context):
        if not self.email or not self.password:
            self.report({'ERROR'}, "Email and password required")
            return {'CANCELLED'}
        try:
            data = mmcp_client.sign_in(self.email, self.password)
        except Exception as exc:                          # noqa: BLE001
            self.report({'ERROR'}, f"Sign-in failed: {exc}")
            return {'CANCELLED'}
        tier = data.get("tier", "")
        msg = f"Signed in as {data.get('email', self.email)}"
        if tier:
            msg += f" ({tier})"
        # Signing in changes what the server will tell us, so ask it again
        # rather than leaving the artist on a stale "cannot reach" or an empty
        # model list.
        mmcp_client.connect_async(force=True)
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class ANIMATICA_OT_signout(Operator):
    bl_idname = "animatica.signout"
    bl_label = "Sign out"
    bl_description = "Forget the cached Animatica session tokens"

    def execute(self, context):
        mmcp_client.sign_out()
        # Signing out on purpose is not an expired session; drop any notice
        # left by one, or the sign-in prompt keeps explaining itself.
        mmcp_client.clear_session_expired()
        self.report({'INFO'}, "Signed out")
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════════════════
# Quota / upgrade
# ═══════════════════════════════════════════════════════════════════════════

class ANIMATICA_OT_open_upgrade(Operator):
    bl_idname = "animatica.open_upgrade"
    bl_label = "Upgrade"
    bl_description = "Open the upgrade URL in your browser"

    def execute(self, context):
        url = (context.scene.animatica.quota_upgrade_url or "").strip()
        if not url:
            self.report({'ERROR'}, "No upgrade URL available")
            return {'CANCELLED'}
        bpy.ops.wm.url_open(url=url)
        return {'FINISHED'}


DISCORD_HELP_URL = "https://discord.com/invite/A8CrURBewz"


class ANIMATICA_OT_open_discord_help(Operator):
    bl_idname = "animatica.open_discord_help"
    bl_label = "Need help?"
    bl_description = "Ask for help on the Animatica Discord (opens your browser)"

    def execute(self, context):
        bpy.ops.wm.url_open(url=DISCORD_HELP_URL)
        return {'FINISHED'}


class ANIMATICA_OT_dismiss_quota(Operator):
    bl_idname = "animatica.dismiss_quota"
    bl_label = "Dismiss"
    bl_description = "Hide the quota-exceeded banner"

    def execute(self, context):
        _clear_quota_state(context.scene.animatica)
        return {'FINISHED'}


class ANIMATICA_OT_randomize_seed(Operator):
    bl_idname = "animatica.randomize_seed"
    bl_label = "Randomize Seed"
    bl_description = "Pick a new random seed for the next generation"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Avoid 0 — that's the "let the server pick" sentinel, which would
        # defeat the point of choosing a specific seed to reproduce later.
        context.scene.animatica.seed = random.randint(1, 999999)
        return {'FINISHED'}


class ANIMATICA_OT_lock_global_seed(Operator):
    bl_idname = "animatica.lock_global_seed"
    bl_label = "Lock Seed"
    bl_description = (
        "Copy the seed the last generation actually ran with into Seed, so the "
        "next Generate reproduces it. Useful after generating with Seed = 0"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.animatica
        last = int(getattr(s, "last_used_seed", 0) or 0)
        if last <= 0:
            self.report({'WARNING'}, "No recorded seed yet — generate once first")
            return {'CANCELLED'}
        s.seed = last
        self.report({'INFO'}, f"Locked seed {last}")
        return {'FINISHED'}


class ANIMATICA_OT_randomize_pose_seed(Operator):
    bl_idname = "animatica.randomize_pose_seed"
    bl_label = "Randomize Seed"
    bl_description = "Pick a new random seed for this pose"

    def execute(self, context):
        # Stash on the class — the running generate-pose dialog reads this
        # in its next draw and copies it into its own operator-instance seed.
        ANIMATICA_OT_generate_pose._pending_seed = random.randint(1, 999999)
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════════

_classes = (
    ANIMATICA_OT_connect,
    ANIMATICA_OT_generate,
    ANIMATICA_OT_generate_pose,
    ANIMATICA_OT_use_example_pose,
    ANIMATICA_MT_example_poses,
    ANIMATICA_OT_accept,
    ANIMATICA_OT_reject,
    ANIMATICA_OT_edit_root_trajectory,
    ANIMATICA_OT_reset_root_trajectory,
    ANIMATICA_OT_cancel_generation,
    ANIMATICA_OT_signin,
    ANIMATICA_OT_signout,
    ANIMATICA_OT_open_upgrade,
    ANIMATICA_OT_open_discord_help,
    ANIMATICA_OT_dismiss_quota,
    ANIMATICA_OT_randomize_seed,
    ANIMATICA_OT_lock_global_seed,
    ANIMATICA_OT_randomize_pose_seed,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
