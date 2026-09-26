# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate for several characters at once: a scene of actors, or a crowd.

Select the characters and Generate them together. One request per character,
sent in parallel, each baked onto its character as its answer arrives — with
the same bake a single Generate uses (``operators.bake_take``), so a take is
the same whichever made it. The takes then wait for review together: Accept
All / Reject All run the ordinary Accept / Reject once per character.

Two ways to direct them:

- **Each their own** — every character's own prompt blocks, key poses,
  waypoints and pins (waypoints and pins belong to the character they were
  made for). A scene with distinct actors.
- **Shared** — the active character's prompt blocks for everyone, each with
  its own seed and starting from where it stands and faces. Variations on one
  action, not copies of it: a crowd. Waypoints and pins are left out, since
  they would pull everyone to the same spot; each character's own key poses
  still go.

Every character is one generation against the quota, and the panel says how
many before the button is pressed.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace

import bpy
from bpy.types import Operator

from . import constraints_ui, mmcp_client, properties, request_builder

#: requests in flight at once; the rest queue behind them
MAX_PARALLEL = 6

#: on each character with a take waiting: the action to restore on Reject
_PENDING_KEY = "animatica_batch_take"


def selected_characters(context) -> list:
    """The characters among the selected objects: armatures, or the rigs of
    selected meshes, each once, by name."""
    found = {}
    for obj in getattr(context, "selected_objects", ()) or ():
        arm = properties.armature_of(obj)
        if arm is not None and properties._is_live_armature(arm):
            found[arm.name] = arm
    return [found[n] for n in sorted(found)]


def pending(settings) -> list:
    """Names of the characters whose batch take waits for Accept / Reject."""
    try:
        return list(json.loads(settings.batch_pending or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


def failures(settings) -> list:
    try:
        return list(json.loads(settings.batch_failed or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


def _snapshot(blocks) -> list:
    """Prompt blocks as plain objects, so a request can be built for a character
    without making it the active one (which swaps the panel's blocks)."""
    return [SimpleNamespace(**d) for d in json.loads(properties._serialize_blocks(blocks))]


def _blocks_of(arm, settings) -> list:
    if arm == settings.target_armature:
        return _snapshot(settings.prompt_blocks)
    raw = arm.get(properties._BLOCKS_KEY)
    try:
        return [SimpleNamespace(**d) for d in json.loads(raw)] if raw else []
    except (json.JSONDecodeError, TypeError):
        return []


@dataclass
class _Job:
    name: str
    req: dict
    blocks: list
    gen_start: int
    gen_end: int
    anchors: object
    splice_target_name: str
    source_name: str
    looped: bool
    future: object = None
    done: bool = False
    error: str = ""
    extra: dict = field(default_factory=dict)


class ANIMATICA_OT_generate_batch(Operator):
    bl_idname = "animatica.generate_batch"
    bl_label = "Generate Selected Characters"
    bl_description = (
        "Generate every selected character at once, one generation each. Each "
        "their own direction, or the active character's shared by all with a "
        "different seed each. The takes wait for Accept All / Reject All"
    )

    _timer = None
    _pool: ThreadPoolExecutor | None = None
    _jobs: list | None = None

    @classmethod
    def poll(cls, context):
        s = context.scene.animatica
        return (not s.is_generating and not s.is_previewing and not pending(s)
                and len(selected_characters(context)) >= 2)

    def execute(self, context):
        from . import operators

        scene = context.scene
        settings = scene.animatica
        chars = selected_characters(context)
        model_caps = mmcp_client.cached_model(settings.model_id)
        if model_caps is None:
            self.report({'ERROR'}, "Connect to the server first")
            return {'CANCELLED'}

        shared = settings.batch_direction == 'SHARED'
        active = properties._live_armature(settings.target_armature)
        if shared and active is None:
            self.report({'ERROR'}, "Shared direction takes the active character's prompts: set one")
            return {'CANCELLED'}
        # The active character's blocks as they stand in the panel right now.
        if active is not None:
            properties.save_blocks_to_armature(active, settings)
        shared_blocks = _snapshot(settings.prompt_blocks) if shared else None

        base_seed = int(settings.seed)
        jobs, skipped = [], []
        for i, arm in enumerate(chars):
            blocks = shared_blocks if shared else _blocks_of(arm, settings)
            if not any((getattr(b, "prompt", "") or "").strip() for b in blocks):
                skipped.append(f"{arm.name}: no prompt")
                continue
            # Shared: waypoints and pins would pull everyone to one spot.
            found = ({"root_paths": [], "effector_targets": [], "waypoints": []} if shared
                     else constraints_ui.walk_scene_constraints(scene, owner=arm))
            # A locked seed would give a shared crowd one motion N times; vary it.
            if shared and base_seed > 0:
                settings.seed = base_seed + i
            try:
                req = request_builder.build_request(
                    model_id=settings.model_id, model_caps=model_caps, armature_obj=arm,
                    prompt_blocks=blocks, settings=settings, scene=scene,
                    constraint_objects=found,
                )
            except request_builder.BuildError as exc:
                skipped.append(f"{arm.name}: {exc}")
                continue
            finally:
                settings.seed = base_seed
            gen_start, gen_end = request_builder.compute_frame_range(blocks, arm, scene)
            act = arm.animation_data.action if arm.animation_data and arm.animation_data.action else None
            source = act if act is not None and not operators._is_motion_bake_action(act) else None
            jobs.append(_Job(
                name=arm.name, req=req, blocks=blocks, gen_start=gen_start, gen_end=gen_end,
                anchors=operators.anchor_frames_for(req, source, gen_start, gen_end),
                splice_target_name=act.name if act is not None else "",
                source_name=source.name if source is not None else "",
                looped=bool((req.get("options") or {}).get("loop")),
            ))

        if not jobs:
            self.report({'ERROR'}, "Nothing to generate — " + "; ".join(skipped))
            return {'CANCELLED'}

        url = mmcp_client.get_mmcp_url()
        self._pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(jobs)))
        for job in jobs:
            job.future = self._pool.submit(
                lambda r=job.req: mmcp_client.MmcpClient(url).generate(r))
        self._jobs = jobs
        self._skipped = skipped

        settings.is_generating = True
        settings.cancel_requested = False
        settings.generation_elapsed = 0
        settings.batch_total = len(jobs)
        settings.batch_done = 0
        settings.batch_failed = json.dumps(skipped)
        import time
        self._start = time.time()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        from . import operators

        settings = context.scene.animatica
        if event.type == 'ESC' or settings.cancel_requested:
            self._finish(context, cancelled=True)
            self.report({'INFO'}, "Batch cancelled; takes already made wait for review")
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        operators._tick_generation_elapsed(context, self._start)

        for job in self._jobs:
            if job.done or not job.future.done():
                continue
            job.done = True
            try:
                result = job.future.result()
                arm = properties._live_armature(bpy.data.objects.get(job.name))
                if arm is None:
                    raise RuntimeError("the character was deleted")
                if arm.animation_data is None:
                    arm.animation_data_create()
                operators.bake_take(
                    context, settings, arm, result,
                    prompt_blocks=job.blocks, gen_start=job.gen_start, gen_end=job.gen_end,
                    anchor_frames=job.anchors,
                    splice_target=bpy.data.actions.get(job.splice_target_name),
                    server_looped=job.looped, source_action_name=job.source_name,
                )
                source = bpy.data.actions.get(job.source_name)
                if source is not None:
                    # Kept alive while the take waits: nothing else uses it,
                    # and a save in between would drop it.
                    source.use_fake_user = True
                arm[_PENDING_KEY] = json.dumps({"source": job.source_name})
                settings.batch_pending = json.dumps(pending(settings) + [job.name])
                settings.batch_done += 1
            except Exception as exc:  # noqa: BLE001 — one character's failure is not the batch's
                operators._stash_quota_state(settings, exc)
                job.error = str(exc)
                settings.batch_failed = json.dumps(failures(settings) + [f"{job.name}: {exc}"])

        if all(j.done for j in self._jobs):
            self._finish(context)
            ok, bad = settings.batch_done, len(self._jobs) - settings.batch_done
            self.report({'WARNING'} if bad else {'INFO'},
                        f"{ok} take(s) ready for review" + (f", {bad} failed" if bad else ""))
            return {'FINISHED'}
        return {'RUNNING_MODAL'}

    def _finish(self, context, *, cancelled: bool = False) -> None:
        settings = context.scene.animatica
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:  # noqa: BLE001
                pass
            self._timer = None
        settings.is_generating = False
        settings.cancel_requested = False
        settings.batch_total = 0


def _review(context, op_name: str) -> int:
    """Run Accept or Reject once for each character with a batch take waiting.

    The ordinary operator, set up as it expects: that character active, its
    source action named, the preview flag up. Then back to the character that
    was active before.
    """
    settings = context.scene.animatica
    before = properties._live_armature(settings.target_armature)
    handled = 0
    for name in pending(settings):
        arm = properties._live_armature(bpy.data.objects.get(name))
        if arm is None:
            continue
        info = json.loads(arm.get(_PENDING_KEY) or "{}")
        settings.target_armature = arm          # swaps in its blocks, clears the flags
        settings.source_action_name = info.get("source", "")
        settings.is_previewing = True
        getattr(bpy.ops.animatica, op_name)()
        source = bpy.data.actions.get(info.get("source", ""))
        if source is not None:
            source.use_fake_user = False
        if _PENDING_KEY in arm:
            del arm[_PENDING_KEY]
        handled += 1
    if before is not None and properties._is_live_armature(before):
        settings.target_armature = before
    settings.batch_pending = ""
    settings.batch_failed = ""
    settings.is_previewing = False
    return handled


class ANIMATICA_OT_accept_batch(Operator):
    bl_idname = "animatica.accept_batch"
    bl_label = "Accept All"
    bl_description = "Keep every character's take: each moves to its own NLA track"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(pending(context.scene.animatica))

    def execute(self, context):
        n = _review(context, "accept")
        self.report({'INFO'}, f"Kept {n} take(s)")
        return {'FINISHED'}


class ANIMATICA_OT_reject_batch(Operator):
    bl_idname = "animatica.reject_batch"
    bl_label = "Reject All"
    bl_description = "Throw every character's take away and go back to what each had"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(pending(context.scene.animatica))

    def execute(self, context):
        n = _review(context, "reject")
        self.report({'INFO'}, f"Threw away {n} take(s)")
        return {'FINISHED'}


_classes = (
    ANIMATICA_OT_generate_batch,
    ANIMATICA_OT_accept_batch,
    ANIMATICA_OT_reject_batch,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
