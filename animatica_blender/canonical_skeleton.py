"""Build a Blender Armature from an MMCP skeleton.

Two sources, same builder:

* **The Animatica rig (default)** — the 30-joint SOMA skeleton bundled at
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


def default_rig_available() -> bool:
    return DEFAULT_RIG_PATH.exists()


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
            ('DEFAULT', "Animatica rig (SOMA30)",
             "The rig bundled with the addon. Works with every model; the server retargets"),
            ('CANONICAL', "Model's canonical",
             "The skeleton the selected model generates on. No retargeting, but it differs per model"),
        ],
        default='DEFAULT',
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

    def execute(self, context):
        settings = context.scene.animatica

        rig_name, joints = None, None
        if self.source == 'DEFAULT':
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
