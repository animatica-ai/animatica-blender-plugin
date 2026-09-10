"""Get a rig into the scene to animate on.

Three sources. The first is imported from a downloaded FBX; the other two are
built by the builder at the bottom of this module from a list of MMCP joints:

* **The Animatic character (default)** — the rigged, textured hero body, with
  its skinned mesh and embedded textures. It is fetched from the asset
  repository on first use rather than shipped in the addon (see
  ``remote_asset``) and normalised on import. This is what users animate on:
  a character rather than a stick figure, and a 77-bone superset of SOMA30,
  so every joint the server generates lands on it and the extra bones (finger
  segments, end bones, eyes, jaw) simply stay unanimated.
* **The Animatica rig** — the 30-joint SOMA skeleton bundled at
  ``assets/soma30_rig.json``. One rig for every backbone: users animate on
  it whatever model they pick, and the server retargets between it and its
  own skeleton. Keeping it local also means the rig does not change under
  the user when a backbone is redeployed.
* **The server's canonical** — ``/capabilities.models[].canonical_skeleton``,
  the skeleton the chosen model actually generates on. Fewer moving parts
  (no retarget hop), but it differs per backbone: ARDY publishes a 27-joint
  Core rig, Kimodo a 30-joint SOMA one.

Either way each joint has a name, a parent name, a local-space
``rest_translation`` (metres, MMCP frame) and a ``rest_rotation``
quaternion (identity for both rigs above).

Bone construction strategy:
  * head = accumulated rest_translation from root to this joint
  * tail = the **first listed child**'s head — matches the model's
    convention of which direction is "up the chain" for that joint. Using
    the centroid of children for branching joints (Hips → spine + legs)
    makes the Hips bone point straight down, which then misinterprets
    every local rotation the model produces as a 180° tilt. Picking the
    first child puts the bone's local +Y along the spine for Hips, along
    the neck for Chest, along the head-end for Head — matching how the
    model authored its rest pose.
  * leaf joints: tail = head + (+Z * 0.05)  (just for visibility)
  * roll = 0   (rest_rotation is baked into the pose, not the bone roll —
    Blender bone rolls don't round-trip cleanly through quaternion data)

All positions are converted MMCP → Blender at import time via ``coords``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import bpy
from bpy.props import BoolProperty, EnumProperty
from mathutils import Vector

from . import body_mesh, coords, mmcp_client


LEAF_TAIL_LENGTH = 0.05   # metres, +Y in MMCP frame (= +Z in Blender)

# The rig we ship. Generated from the SOMA30 skeleton in MMCP wire form; see
# the file's own ``_comment``.
DEFAULT_RIG_PATH = Path(__file__).parent / "assets" / "soma30_rig.json"
DEFAULT_RIG_NAME = "SOMA30"

# The Animatic character: a rigged, textured hero body. It is not shipped in
# the addon — ``remote_asset`` fetches the FBX from the asset repository on
# first use and caches it per user; see that module for why, and for how the
# pinned hash serves as both version and integrity check.
#
# Its bones carry the ``animatica:`` namespace from the Maya/MotionBuilder
# pipeline it comes out of. That is kept, so the asset still round-trips to
# that pipeline; the bake meets it halfway instead, resolving bare MMCP joint
# names through the namespace (``gltf_to_blender.resolve_joint_bone``).
# Renaming the bones here would also mean renaming all 77 vertex groups to
# keep the skinning attached.
#
# Everything else about the Maya export is normalised on the way in, because
# Blender's FBX importer does not do it for you and the raw result is not an
# armature anyone can work with. ``docs/rebuilding-the-character.md`` explains
# the two halves: ``automatic_bone_orientation`` aims each bone at its child,
# and the importer's unit/axis transform is baked into the armature and mesh
# data so the object sits at identity with bone lengths and root motion in
# metres.
CHARACTER_NAME = "Animatic"

#: The importer settings that make this a usable armature rather than a bundle
#: of +Y sticks. ``ignore_leaf_bones`` stays off: the ``*End`` bones carry
#: vertex groups, and dropping them detaches part of the skin.
_FBX_IMPORT = {
    "automatic_bone_orientation": True,
    "ignore_leaf_bones": False,
    "use_anim": False,
}


def default_rig_available() -> bool:
    return DEFAULT_RIG_PATH.exists()


def character_available() -> bool:
    """True when the character is already downloaded and ready to import."""
    from . import remote_asset
    return remote_asset.is_cached()


def load_character(context) -> tuple[bpy.types.Object, bpy.types.Object | None]:
    """Import the cached Animatic character. Returns ``(armature, body_mesh)``.

    The download is the caller's job — this needs the file already in the
    cache and raises ``ValueError`` if it is not, so the operator can offer to
    fetch it rather than stalling the UI from in here.
    """
    from . import remote_asset

    path = remote_asset.cache_path()
    if not remote_asset.is_cached():
        raise ValueError("the Animatic character has not been downloaded yet")

    before = set(bpy.data.objects)
    try:
        bpy.ops.import_scene.fbx(filepath=str(path), **_FBX_IMPORT)
    except Exception as exc:                                         # noqa: BLE001
        raise ValueError(f"cannot import {path.name}: {exc}") from exc
    imported = [o for o in bpy.data.objects if o not in before]

    arm_obj = next((o for o in imported if o.type == 'ARMATURE'), None)
    if arm_obj is None:
        for obj in imported:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise ValueError(f"{path.name} contains no armature")
    mesh_obj = next((o for o in imported if o.type == 'MESH'), None)

    # The published FBX carries only the character; anything else that turned
    # up (a camera, a control rig) is not part of it and is dropped rather
    # than left in the user's scene.
    for obj in imported:
        if obj not in (arm_obj, mesh_obj):
            bpy.data.objects.remove(obj, do_unlink=True)

    _normalise_transform(arm_obj, mesh_obj)
    clear_pose(arm_obj)

    arm_obj.name = CHARACTER_NAME
    arm_obj.data.name = CHARACTER_NAME
    if mesh_obj is not None:
        mesh_obj.name = f"{CHARACTER_NAME}_body"

    coll = context.collection or context.scene.collection
    for obj in (arm_obj, mesh_obj):
        if obj is None:
            continue
        for other in list(obj.users_collection):
            if other is not coll:
                other.objects.unlink(obj)
        if obj.name not in coll.objects:
            coll.objects.link(obj)

    return arm_obj, mesh_obj


# ---------------------------------------------------------------------------
# First-run download progress
#
# The fetch cannot run inline in ``execute()``. Blocking the main thread in
# Python starves Blender's event loop, so nothing repaints and operator
# reports are not surfaced until the operator returns: the user would get a
# frozen window with no indication, then "Downloading…" and "Imported…"
# arriving together once it was already over. It runs on a worker thread with
# a modal timer instead, and this dict is what the timer and the panel read.
# ---------------------------------------------------------------------------

_DOWNLOAD: dict = {
    "active": False, "got": 0, "total": 0, "speed": 0.0,
    "error": None, "done": False,
}

#: Weight of the newest sample in the transfer-rate average. Chunks arrive
#: unevenly, so the raw per-chunk rate swings wildly; smoothing keeps the
#: number readable without making it lag the truth.
_RATE_SMOOTHING = 0.3

#: ``(monotonic time, bytes)`` of the previous progress callback, for the rate.
_RATE_SAMPLE: list = [0.0, 0]


def download_state() -> dict:
    """Snapshot of an in-flight character download, for the panel to draw.

    ``percent`` and ``eta`` are derived here rather than stored, so the worker
    thread only ever writes the two raw numbers it actually knows.
    """
    state = dict(_DOWNLOAD)
    got, total, speed = state["got"], state["total"], state["speed"]
    state["percent"] = int(100 * got / total) if total else 0
    state["eta"] = (
        (total - got) / speed
        if total and speed > 0 and total > got
        else None
    )
    return state


def _asset_size() -> str:
    from . import remote_asset
    return remote_asset.human_size()


def _download_blocking(context, operator) -> None:
    """Fetch the character on the calling thread. Raises ``ValueError``.

    Only for ``EXEC_DEFAULT`` callers — scripts, the agent skill, tests —
    which have no event loop to keep alive anyway. Interactive clicks go
    through the operator's modal path.
    """
    from . import remote_asset

    wm = context.window_manager
    wm.progress_begin(0, 100)
    try:
        remote_asset.download(
            progress=lambda got, total: wm.progress_update(
                int(100 * got / total) if total else 0
            ),
        )
    except remote_asset.DownloadError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        wm.progress_end()


def _normalise_transform(arm_obj, mesh_obj) -> None:
    """Bake the FBX importer's unit/axis conversion into the datablocks.

    The importer leaves it on the *object* — 0.01 scale and +90 degrees about
    X — which puts bone lengths and root motion in Maya centimetres. Both
    datablocks take the same matrix because the mesh's vertices are in
    armature space, and it is the DATA that is transformed, not the objects:
    ``transform_apply`` on a parented, skinned mesh misaligns it from its
    armature.
    """
    from mathutils import Matrix

    M = arm_obj.matrix_world.copy()
    if M == Matrix.Identity(4):
        return
    arm_obj.data.transform(M)
    arm_obj.matrix_world = Matrix.Identity(4)
    if mesh_obj is not None:
        mesh_obj.data.transform(M)
        mesh_obj.matrix_world = Matrix.Identity(4)
        mesh_obj.parent = arm_obj
        mesh_obj.matrix_parent_inverse = Matrix.Identity(4)
        for mod in mesh_obj.modifiers:
            if mod.type == 'ARMATURE':
                mod.object = arm_obj


def clear_pose(arm_obj: bpy.types.Object) -> None:
    """Put ``arm_obj`` back on its rest pose — for this character, the T-pose.

    Pose-bone transforms are saved in a .blend independently of any action, so
    clearing an armature's ``animation_data`` does not unpose it: whatever
    frame the rig was evaluated on when the asset was authored gets baked in
    and the character imports mid-stride. The shipped asset is written on rest
    for exactly that reason; this makes the guarantee hold whatever the asset
    happens to contain, and costs one pass over 77 bones.
    """
    if arm_obj.pose is None:
        return
    for pb in arm_obj.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pb.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)


def load_default_rig() -> tuple[str, list[dict[str, Any]]]:
    """``(rig_name, joints)`` for the bundled Animatica rig.

    Raises ``ValueError`` if the asset is missing or malformed — the caller
    falls back to the server's canonical.
    """
    try:
        doc = json.loads(DEFAULT_RIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:                                         # noqa: BLE001
        raise ValueError(f"cannot read bundled rig {DEFAULT_RIG_PATH.name}: {exc}") from exc
    joints = doc.get("joints") or []
    if not joints:
        raise ValueError(f"bundled rig {DEFAULT_RIG_PATH.name} has no joints")
    return doc.get("name") or DEFAULT_RIG_NAME, joints


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class ANIMATICA_OT_import_canonical_skeleton(bpy.types.Operator):
    bl_idname = "animatica.import_canonical_skeleton"
    bl_label = "Import Rig"
    bl_description = (
        "Build a Blender armature to animate on. Defaults to the Animatica "
        "rig (SOMA30) bundled with the addon — the same rig for every model, "
        "retargeted server-side. Switch the source to use the selected "
        "model's own canonical skeleton instead"
    )
    bl_options = {'REGISTER', 'UNDO'}

    source: EnumProperty(
        name="Rig",
        description="Which skeleton to build",
        items=[
            ('CHARACTER', "Animatic character",
             "The rigged, textured Animatic body bundled with the addon. Animate on this"),
            ('DEFAULT', "Animatica rig (SOMA30)",
             "The bare 30-joint rig bundled with the addon. Works with every model; the server retargets"),
            ('CANONICAL', "Model's canonical",
             "The skeleton the selected model generates on. No retargeting, but it differs per model"),
        ],
        default='CHARACTER',
    )

    with_body: BoolProperty(
        name="Include body mesh",
        description=(
            "Also import the SOMA77 reference body mesh, skinned to the "
            "imported armature. Weights for joints not present on the "
            "armature (fingers, jaw) get redistributed to their nearest "
            "ancestor — fingers don't curl, but the body shape is preserved"
        ),
        default=True,
    )

    # ----- first-run download (interactive) --------------------------------

    _timer = None
    _thread = None
    _download_attempted = False

    def invoke(self, context, event):
        """Fetch the character first, with a visible loader, then import.

        Only the interactive path goes modal. ``EXEC_DEFAULT`` callers land in
        ``execute`` and take the blocking fetch, which is what a script wants.
        """
        from . import remote_asset

        if self.source != 'CHARACTER' or remote_asset.is_cached():
            return self.execute(context)
        return self._start_download(context)

    def _start_download(self, context):
        import threading
        import time

        from . import remote_asset

        _DOWNLOAD.update(active=True, got=0, total=remote_asset.ASSET_BYTES,
                         speed=0.0, error=None, done=False)
        _RATE_SAMPLE[0], _RATE_SAMPLE[1] = 0.0, 0

        def _progress(got, total):
            # Worker thread. Writes raw counters only; the panel derives the
            # percentage and the ETA from them on the main thread.
            now = time.monotonic()
            prev_time, prev_got = _RATE_SAMPLE
            if prev_time and now > prev_time:
                instant = (got - prev_got) / (now - prev_time)
                previous = _DOWNLOAD["speed"]
                _DOWNLOAD["speed"] = (
                    instant if previous <= 0
                    else previous + _RATE_SMOOTHING * (instant - previous)
                )
            _RATE_SAMPLE[0], _RATE_SAMPLE[1] = now, got
            _DOWNLOAD["got"] = got
            _DOWNLOAD["total"] = total or remote_asset.ASSET_BYTES

        def _worker():
            try:
                remote_asset.download(progress=_progress)
            except Exception as exc:                             # noqa: BLE001
                _DOWNLOAD["error"] = str(exc)
            finally:
                _DOWNLOAD["done"] = True

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        wm.progress_begin(0, 100)
        self._draw_progress(context)
        return {'RUNNING_MODAL'}

    def _draw_progress(self, context) -> None:
        """Push the current figures everywhere the user might be looking."""
        from . import remote_asset

        state = download_state()
        text = (
            f"Downloading the {CHARACTER_NAME} character — "
            f"{remote_asset.format_bytes(state['got'])} of "
            f"{remote_asset.format_bytes(state['total'])} "
            f"({state['percent']}%) · {remote_asset.format_rate(state['speed'])} · "
            f"{remote_asset.format_eta(state['eta'])}"
        )
        try:
            context.window_manager.progress_update(state["percent"])
            context.workspace.status_text_set(text)
        except (AttributeError, TypeError):
            pass
        # The sidebar draws the same state; without an explicit tag it would
        # not repaint until the user moved the mouse over it.
        for area in getattr(context.screen, "areas", ()):
            if area.type == 'VIEW_3D':
                area.tag_redraw()

    def _end_download(self, context) -> None:
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()
        try:
            context.workspace.status_text_set(None)
        except (AttributeError, TypeError):
            pass
        _DOWNLOAD.update(active=False, got=0, total=0, speed=0.0)
        for area in getattr(context.screen, "areas", ()):
            if area.type == 'VIEW_3D':
                area.tag_redraw()

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if not _DOWNLOAD["done"]:
            self._draw_progress(context)
            return {'RUNNING_MODAL'}

        error = _DOWNLOAD["error"]
        self._end_download(context)
        # Either way we now go through execute(), which must not start a
        # second fetch: on success the cache is warm, on failure this flag
        # sends it down the SOMA30 fallback instead of retrying.
        self._download_attempted = True
        if error:
            self.report({'WARNING'}, error)
        else:
            self.report(
                {'INFO'},
                f"Downloaded the {CHARACTER_NAME} character ({_asset_size()})",
            )
        return self.execute(context)

    def cancel(self, context):
        # The worker is a daemon thread on a socket read; it cannot be
        # interrupted from here, so it is left to finish into the cache (the
        # download is atomic — a partial file is never left behind).
        self._end_download(context)

    def execute(self, context):
        settings = context.scene.animatica

        # The character is not built from joints — it is appended whole, so it
        # short-circuits the builder (and the separate body-mesh import, since
        # it brings its own skinned mesh).
        if self.source == 'CHARACTER':
            from . import remote_asset
            try:
                if not remote_asset.is_cached() and not self._download_attempted:
                    # EXEC_DEFAULT only — an interactive click has already
                    # been through the modal fetch by the time it gets here.
                    self._download_attempted = True
                    _download_blocking(context, self)
                if not remote_asset.is_cached():
                    raise ValueError("the Animatic character is not available")
                arm_obj, mesh_obj = load_character(context)
            except ValueError as exc:
                self.report({'WARNING'}, f"{exc}; falling back to the SOMA30 rig")
            else:
                settings.target_armature = arm_obj
                try:
                    context.view_layer.objects.active = arm_obj
                    bpy.ops.object.mode_set(mode='POSE')
                except Exception:                                    # noqa: BLE001
                    # No 3D View context (headless / script) — import still fine.
                    pass
                self.report(
                    {'INFO'},
                    f"Imported the {CHARACTER_NAME} character "
                    f"({len(arm_obj.data.bones)} bones"
                    + (" with body mesh)" if mesh_obj is not None else ")"),
                )
                return {'FINISHED'}

        rig_name, joints = None, None
        if self.source in {'DEFAULT', 'CHARACTER'}:
            try:
                rig_name, joints = load_default_rig()
            except ValueError as exc:
                self.report({'WARNING'}, f"{exc}; falling back to the model's canonical")

        if joints is None:
            model_id = settings.model_id
            if not model_id:
                self.report({'ERROR'}, "Pick a model first (Animatica panel → Connect, then choose a Model)")
                return {'CANCELLED'}

            # Re-fetch /capabilities before building. The process-wide cache
            # is only refreshed by Connect, so after a backbone redeploy that
            # changes the published canonical an import would silently
            # rebuild the stale rig. Fall back to the cache when the server
            # cannot be reached right now.
            url = mmcp_client.get_mmcp_url()
            try:
                caps = mmcp_client.MmcpClient(url, timeout=30).capabilities(refresh=True)
                mmcp_client.store_capabilities(caps)
            except Exception as exc:                                 # noqa: BLE001
                self.report({'WARNING'}, f"Could not refresh capabilities from {url} ({exc}); using cached")

            model = mmcp_client.cached_model(model_id)
            if model is None:
                self.report({'ERROR'}, f"Model {model_id!r} not in the cached capabilities; reconnect first")
                return {'CANCELLED'}
            joints = (model.get("canonical_skeleton") or {}).get("joints") or []
            if not joints:
                self.report({'ERROR'}, f"Model {model_id!r} has no canonical_skeleton.joints")
                return {'CANCELLED'}
            rig_name = model_id

        try:
            arm_obj, floor_lift = build_armature_from_canonical(rig_name, joints, context)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        # Wire it as the generation target.
        settings.target_armature = arm_obj

        body_loaded = False
        if (
            self.with_body
            and body_mesh.asset_available()
            and body_mesh.looks_like_kimodo_skeleton(arm_obj)
        ):
            try:
                body_obj = body_mesh.load_body_mesh(
                    arm_obj, context,
                    canonical_joints=joints,
                    floor_lift=floor_lift,
                )
                body_loaded = body_obj is not None
            except Exception as exc:                                 # noqa: BLE001
                # Mesh is a nice-to-have; never fail the armature import on it.
                self.report({'WARNING'}, f"Imported armature but body mesh failed: {exc}")

        msg = f"Imported {rig_name} ({len(joints)} joints)"
        if body_loaded:
            msg += " with body mesh"
            try:
                context.view_layer.objects.active = arm_obj
                bpy.ops.object.mode_set(mode='POSE')
            except Exception:
                # No 3D View context (e.g. headless / script) — armature import
                # still succeeded.
                pass
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_armature_from_canonical(
    model_id: str,
    joints: list[dict[str, Any]],
    context,
) -> tuple[bpy.types.Object, float]:
    """Create + link a Blender Armature object whose bones mirror ``joints``.

    Returns ``(armature_object, floor_lift)``. The lift is the +Z offset
    applied to every bone head/tail so the lowest joint sits on Blender's
    z=0 plane; downstream code that wants to align other geometry with the
    rest pose (e.g. the body mesh) needs the same value.

    Always creates a new object — re-importing the same model produces
    ``model_id.001``, ``.002``, etc.
    """
    name_to_local: dict[str, tuple[float, float, float]] = {}
    name_to_parent: dict[str, str | None] = {}
    children_of: dict[str, list[str]] = {}
    order: list[str] = []

    for j in joints:
        name = j.get("name")
        if not name:
            raise ValueError("joint missing 'name'")
        parent = j.get("parent")
        rt = j.get("rest_translation") or [0.0, 0.0, 0.0]
        if len(rt) != 3:
            raise ValueError(f"joint {name!r}: rest_translation must be length 3")
        name_to_local[name]  = (float(rt[0]), float(rt[1]), float(rt[2]))
        name_to_parent[name] = parent
        children_of.setdefault(parent, []).append(name)
        order.append(name)

    # Resolve global rest positions in MMCP frame. ``joints`` is guaranteed
    # parents-before-children by the spec, so a single forward pass is enough.
    name_to_global_mmcp: dict[str, tuple[float, float, float]] = {}
    for name in order:
        local = name_to_local[name]
        parent = name_to_parent[name]
        if parent is None:
            name_to_global_mmcp[name] = local
        else:
            px, py, pz = name_to_global_mmcp[parent]
            lx, ly, lz = local
            name_to_global_mmcp[name] = (px + lx, py + ly, pz + lz)

    # Lift the whole skeleton so the lowest joint sits on the floor (Blender
    # z = 0). The model's root_positions stream encodes absolute root height
    # (~1 m for SOMA), so once an animation is baked the character pops to
    # the right place — but the *rest* pose without that offset would put
    # feet below the floor. Adding the lift to every joint's head/tail keeps
    # them aligned during baking too (the bake's `delta = world - rest_head`
    # accounts for it automatically).
    lowest_blender_z = min(
        coords.mmcp_pos_to_blender(p)[2] for p in name_to_global_mmcp.values()
    )
    floor_lift = max(0.0, -lowest_blender_z)

    # Build the armature.
    arm_data = bpy.data.armatures.new(f"{model_id}_data")
    arm_obj  = bpy.data.objects.new(model_id, arm_data)
    context.scene.collection.objects.link(arm_obj)

    # Edit mode is the only place EditBones can be created.
    prev_active = context.view_layer.objects.active
    context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        edit_bones = arm_data.edit_bones

        for name in order:
            head_mmcp = name_to_global_mmcp[name]
            head = Vector(coords.mmcp_pos_to_blender(head_mmcp))
            head.z += floor_lift

            children = children_of.get(name, [])
            if children:
                # Take the first listed child as the bone's "primary"
                # direction. The joints[] array is in the model's intended
                # order (parents-before-children, primary chain first), so
                # for Hips this picks Spine1 (not LeftLeg/RightLeg), which
                # keeps the bone's local +Y aligned with what the model
                # treats as the rest direction.
                primary = children[0]
                tail = Vector(coords.mmcp_pos_to_blender(name_to_global_mmcp[primary]))
                tail.z += floor_lift
                if (tail - head).length < 1e-4:
                    tail = head + Vector((0, 0, LEAF_TAIL_LENGTH))
            else:
                tail = head + Vector((0, 0, LEAF_TAIL_LENGTH))

            bone = edit_bones.new(name)
            bone.head = head
            bone.tail = tail
            bone.roll = 0.0

        # Wire parent links in a second pass so child bones exist.
        for name in order:
            parent = name_to_parent[name]
            if parent is not None:
                edit_bones[name].parent = edit_bones[parent]
                # Optional connect: only if parent's tail coincides with this
                # bone's head (otherwise we'd snap the bone). Loose-link
                # everything for v1 — preserves rest_translation faithfully.
                edit_bones[name].use_connect = False
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.objects.active = prev_active

    # Stash the model id on the armature so the rest of the addon can verify
    # the rig matches the server's canonical skeleton without name-matching.
    arm_obj["animatica_canonical_model"] = model_id

    return arm_obj, floor_lift
