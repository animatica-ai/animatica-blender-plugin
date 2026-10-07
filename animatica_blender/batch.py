# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate for several characters at once: a scene of actors, or a crowd.

Select the characters and Generate them together: one batched request when
the model takes batches (``supports_batch``), otherwise one request per
character in parallel. Each is baked onto its character as its answer arrives — with
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

Variations apply per character: with Variations at 3 each character comes
back with three versions, flipped through in the review one character at a
time or all together, and Accept All keeps the version each one shows.

Every character is one generation against the quota, and the panel says how
many before the button is pressed.
"""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from . import constraints_ui, mmcp_client, properties, request_builder, variations
from .operators import ends_cleanly

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


def _submit(pool, jobs, url, model_caps) -> None:
    """Send the jobs, giving each a future for its own answer.

    A model that takes batches gets them in as few requests as its batch size
    allows, and a server that has several characters in one request can run
    them in one pass of the model. Otherwise one request per character, in
    parallel.
    """
    if not model_caps.get("supports_batch"):
        for job in jobs:
            job.future = pool.submit(lambda r=job.req: mmcp_client.MmcpClient(url).generate(r))
        return
    size = max(1, int((model_caps.get("limits") or {}).get("max_batch_size") or 16))
    for i in range(0, len(jobs), size):
        chunk = jobs[i:i + size]
        for job in chunk:
            job.future = Future()
        whole = pool.submit(lambda reqs=[j.req for j in chunk]:
                            mmcp_client.MmcpClient(url).generate_batch(reqs))
        whole.add_done_callback(lambda f, chunk=chunk: _fan_out(f, chunk))


def _fan_out(whole, chunk) -> None:
    """One batch's answer, handed to each character's future."""
    if whole.cancelled():
        return
    exc = whole.exception()
    for job, result in zip(chunk, [exc] * len(chunk) if exc else whole.result(), strict=True):
        if isinstance(result, BaseException):
            job.future.set_exception(result)
        else:
            job.future.set_result(result)


class ANIMATICA_OT_generate_batch(Operator):
    bl_idname = "animatica.generate_batch"
    bl_label = "Generate Selected Characters"
    bl_description = (
        "Generate every selected character at once, one generation each. Each "
        "character uses its own prompts, or all share the active character's "
        "prompts with a different seed each. The takes wait for Accept All or "
        "Reject All"
    )

    characters: StringProperty(
        default="",
        options={'SKIP_SAVE'},
        description="Characters to generate, as a JSON list of names, used instead of "
                    "the selection. Any take of theirs still waiting for review is "
                    "thrown away first. Used by Regenerate in the batch review",
    )

    _timer = None
    _pool: ThreadPoolExecutor | None = None
    _jobs: list | None = None

    @classmethod
    def description(cls, context, props):
        if not props.characters:
            return cls.bl_description
        names = json.loads(props.characters)
        who = names[0] if len(names) == 1 else f"these {len(names)} characters"
        return (f"Generate {who} again: the take waiting is thrown away and a new "
                "one made, the others stay as they are")

    @classmethod
    def poll(cls, context):
        s = context.scene.animatica
        return not s.is_generating and not s.is_previewing

    def _chars(self, context) -> list:
        if not self.characters:
            return selected_characters(context)
        arms = [properties._live_armature(bpy.data.objects.get(n))
                for n in json.loads(self.characters)]
        return [a for a in arms if a is not None]

    def execute(self, context):
        from . import operators

        scene = context.scene
        settings = scene.animatica
        chars = self._chars(context)
        if not self.characters and (pending(settings) or len(chars) < 2):
            self.report({'ERROR'}, "Select two or more characters, with no batch waiting for review")
            return {'CANCELLED'}
        if not chars:
            self.report({'ERROR'}, "Those characters are gone")
            return {'CANCELLED'}
        if operators._refuse_in_tweak_mode(self, chars):
            return {'CANCELLED'}
        model_caps = mmcp_client.cached_model(settings.model_id)
        if model_caps is None:
            self.report({'ERROR'}, "Connect to the server first")
            return {'CANCELLED'}
        # Regenerate: their takes go first, so the request is built from what
        # each had, not from the take being replaced.
        redo = [a.name for a in chars if a.name in pending(settings)]
        if redo:
            kept_failed = failures(settings)
            _review(context, "reject", redo)
            settings.batch_failed = json.dumps(kept_failed)

        shared = settings.batch_direction == 'SHARED'
        active = properties._live_armature(settings.target_armature)
        if shared and active is None:
            self.report({'ERROR'}, "Shared direction uses the active character's prompts. Set an active character first")
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
            self.report({'ERROR'}, "Nothing to generate: " + "; ".join(skipped))
            return {'CANCELLED'}

        self._pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(jobs)))
        _submit(self._pool, jobs, mmcp_client.get_mmcp_url(), model_caps)
        self._jobs = jobs
        # Whether any character was refused for the quota. The dialog opens
        # once, when the batch ends, not once per refused character.
        self._quota_hit = False
        self._skipped = skipped

        settings.is_generating = True
        settings.cancel_requested = False
        settings.generation_elapsed = 0
        settings.batch_total = len(jobs)
        settings.batch_done = 0
        # Failures of characters being tried again are old news.
        again = {j.name for j in jobs}
        settings.batch_failed = json.dumps(
            [f for f in failures(settings) if f.split(":")[0] not in again] + skipped)
        import time
        self._start = time.time()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._finish(context, cancelled=True)

    @ends_cleanly
    def modal(self, context, event):
        from . import operators

        settings = context.scene.animatica
        if operators.esc_cancels(self, event) or settings.cancel_requested:
            self._finish(context, cancelled=True)
            self.report({'INFO'}, "Batch cancelled. Takes already made are waiting for review")
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
                bake = dict(
                    prompt_blocks=job.blocks, gen_start=job.gen_start, gen_end=job.gen_end,
                    anchor_frames=job.anchors, server_looped=job.looped,
                    source_action_name=job.source_name,
                )
                action, _ = operators.bake_take(
                    context, settings, arm, result,
                    splice_target=bpy.data.actions.get(job.splice_target_name), **bake,
                )
                # Its variations, to flip through in the review.
                variations.remember(arm, result, action,
                                    splice_target_name=job.splice_target_name, **bake)
                # The source is kept alive while the take waits (a fake user,
                # given by the take's preview session, and taken back by the
                # Accept / Reject that ends it).
                arm[_PENDING_KEY] = json.dumps({"source": job.source_name})
                settings.batch_pending = json.dumps(
                    [n for n in pending(settings) if n != job.name] + [job.name])
                settings.batch_done += 1
            except Exception as exc:  # noqa: BLE001 — one character's failure is not the batch's
                operators._stash_quota_state(settings, exc)
                self._quota_hit = self._quota_hit or getattr(exc, "code", None) == "quota_exceeded"
                job.error = str(exc)
                settings.batch_failed = json.dumps(failures(settings) + [f"{job.name}: {exc}"])

        if all(j.done for j in self._jobs):
            self._finish(context)
            if self._quota_hit:
                operators._redraw_sidebars(context)
                operators._open_quota_dialog(context)
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


def _review(context, op_name: str, names: list | None = None) -> int:
    """Run Accept or Reject once for each character named (every character
    with a batch take waiting, by default).

    The ordinary operator, set up as it expects: that character active, its
    source action named, the preview flag up. Then back to the character that
    was active before. The review ends with its last take.
    """
    settings = context.scene.animatica
    before = properties._live_armature(settings.target_armature)
    waiting = pending(settings)
    names = waiting if names is None else [n for n in names if n in waiting]
    handled = 0
    for name in names:
        arm = properties._live_armature(bpy.data.objects.get(name))
        if arm is None:
            continue
        info = json.loads(arm.get(_PENDING_KEY) or "{}")
        settings.target_armature = arm          # swaps in its blocks, clears the flags
        from . import preview_session
        kept = preview_session.source_of(arm)
        settings.source_action_name = kept.name if kept is not None else info.get("source", "")
        settings.is_previewing = True
        legacy = preview_session.get(arm) is None
        getattr(bpy.ops.animatica, op_name)()
        source = bpy.data.actions.get(info.get("source", ""))
        if legacy and source is not None:
            # A take from an older version, which set the fake user itself.
            source.use_fake_user = False
        if _PENDING_KEY in arm:
            del arm[_PENDING_KEY]
        handled += 1
    if before is not None and properties._is_live_armature(before):
        settings.target_armature = before
    left = [n for n in waiting if n not in names
            and properties._live_armature(bpy.data.objects.get(n)) is not None]
    settings.batch_pending = json.dumps(left) if left else ""
    if not left:
        settings.batch_failed = ""
    settings.is_previewing = False
    return handled


def _refuse_in_tweak_mode(op, context, names: list | None = None) -> bool:
    """Before a review: no character it would touch is in NLA tweak mode
    (see operators._refuse_in_tweak_mode). Each character's Accept or Reject
    refusing on its own left the review counting it done."""
    from . import operators
    waiting = pending(context.scene.animatica)
    names = waiting if names is None else [n for n in names if n in waiting]
    arms = [properties._live_armature(bpy.data.objects.get(n)) for n in names]
    return operators._refuse_in_tweak_mode(op, [a for a in arms if a is not None])


class ANIMATICA_OT_accept_batch(Operator):
    bl_idname = "animatica.accept_batch"
    bl_label = "Accept All"
    bl_description = "Keep every character's take. Each one moves to its own NLA track"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return not context.scene.animatica.is_generating and bool(pending(context.scene.animatica))

    def execute(self, context):
        if _refuse_in_tweak_mode(self, context):
            return {'CANCELLED'}
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
        s = context.scene.animatica
        # With only failures left, this is how they are dismissed.
        return not s.is_generating and bool(pending(s) or failures(s))

    def execute(self, context):
        if _refuse_in_tweak_mode(self, context):
            return {'CANCELLED'}
        n = _review(context, "reject")
        self.report({'INFO'}, f"Threw away {n} take(s)")
        return {'FINISHED'}


class ANIMATICA_OT_review_one(Operator):
    bl_idname = "animatica.review_batch_one"
    bl_label = "Keep or Throw Away"
    bl_options = {'REGISTER', 'UNDO'}

    character: StringProperty()
    keep: BoolProperty(default=True)

    @classmethod
    def description(cls, context, props):
        return (f"Keep {props.character}'s take: it moves to its NLA track" if props.keep
                else f"Throw {props.character}'s take away and go back to what it had")

    @classmethod
    def poll(cls, context):
        return not context.scene.animatica.is_generating and bool(pending(context.scene.animatica))

    def execute(self, context):
        if _refuse_in_tweak_mode(self, context, [self.character]):
            return {'CANCELLED'}
        n = _review(context, "accept" if self.keep else "reject", [self.character])
        if not n:
            self.report({'WARNING'}, f"No take waiting on {self.character}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"{'Kept' if self.keep else 'Threw away'} {self.character}'s take")
        return {'FINISHED'}


def apply_inplace(settings) -> None:
    """In place, live on every take waiting in the batch review: the travel
    muted or back, as it is for a single take."""
    from . import operators
    for name in pending(settings):
        arm = properties._live_armature(bpy.data.objects.get(name))
        if arm is not None:
            operators._apply_inplace_constraint(arm, enabled=bool(settings.inplace))
            arm.update_tag()


_classes = (
    ANIMATICA_OT_generate_batch,
    ANIMATICA_OT_accept_batch,
    ANIMATICA_OT_reject_batch,
    ANIMATICA_OT_review_one,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
