# SPDX-License-Identifier: GPL-3.0-or-later
"""Forward-migration of .blend data written before the Proscenium → Animatica rename.

Everything the addon stamps into a .blend was prefixed ``proscenium``. The
rename moved those to ``animatica``, which orphans data in files saved by
0.4.0 and earlier: the scene settings, the per-object constraint markers, the
prompt blocks cached on the armature.

Two different problems, handled two different ways:

* Things looked up by an **exact key** — custom properties, and the
  ``Scene`` settings pointer — cannot be read at all under the old name, so
  they are rewritten here, once, on file load.

* Things matched by **name prefix** — motion-bake actions, NLA tracks — are
  not touched. ``request_builder._GENERATED_ACTION_PREFIXES`` and
  ``operators._NLA_TRACK_PREFIXES`` carry the old spellings alongside the new
  ones, so old datablocks keep resolving without rewriting anything the user
  can see in the outliner. Renaming them would also invalidate
  ``settings.source_action_name``, which refers to an action by name.

This runs on every load and is a no-op once migrated, so it costs one pass
over the ID collections on files that have nothing to do.
"""
from __future__ import annotations

import bpy

_OLD = "proscenium"
_NEW = "animatica"
_OLD_PROP_PREFIX = f"{_OLD}_"
_NEW_PROP_PREFIX = f"{_NEW}_"

# The ID collections the addon stamps custom properties onto. Objects carry
# the constraint markers and the block cache, actions the skipped-joint list,
# scenes the settings pointer.
_ID_COLLECTIONS = ("objects", "actions", "scenes", "armatures", "curves")

# Pre-rename name of the in-place limit constraint. Looked up with
# ``constraints.get()``, so a stale name means a second constraint gets added
# on top of the old one instead of reusing it.
_OLD_INPLACE_CONSTRAINT = "Proscenium_InPlace"
_NEW_INPLACE_CONSTRAINT = "Animatica_InPlace"


def _plain(value):
    """An IDProperty value as something assignable back into an IDProperty.

    Blender hands back wrapper types (``IDPropertyArray``,
    ``IDPropertyGroup``) that cannot be re-assigned directly.
    """
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "to_list"):
        return value.to_list()
    return value


def _migrate_custom_props(datablock) -> int:
    """Rename ``proscenium_*`` custom props to ``animatica_*`` on one ID."""
    moved = 0
    for key in [k for k in datablock.keys() if k.startswith(_OLD_PROP_PREFIX)]:
        new_key = _NEW_PROP_PREFIX + key[len(_OLD_PROP_PREFIX):]
        if new_key in datablock.keys():
            # Already migrated (or written fresh by this version) — the new
            # value wins; just drop the stale one.
            del datablock[key]
            continue
        try:
            datablock[new_key] = _plain(datablock[key])
        except (TypeError, ValueError):
            continue
        del datablock[key]
        moved += 1
    return moved


def _migrate_scene_settings(scene) -> bool:
    """Copy the old ``scene["proscenium"]`` settings onto ``scene.animatica``.

    Only scalars are copied. ``prompt_blocks`` is a collection property and
    is not stored here in a form worth reconstructing — it is separately
    cached on the target armature as a custom property (migrated above) and
    rehydrated by the addon's own ``load_post`` handler, which runs after
    this one.
    """
    old = scene.get(_OLD)
    if old is None:
        return False
    new = getattr(scene, _NEW, None)
    if new is None:
        return False
    try:
        items = old.to_dict().items()
    except AttributeError:
        del scene[_OLD]
        return False

    for key, value in items:
        if not hasattr(new, key):
            continue
        if isinstance(value, (dict, list)):
            continue
        try:
            setattr(new, key, value)
        except (TypeError, ValueError, AttributeError):
            # Enum values and pointer targets that no longer exist land here;
            # the field keeps its default, which is the right fallback.
            continue

    del scene[_OLD]
    return True


def _migrate_inplace_constraints() -> int:
    """Rename the in-place limit constraint on every armature root bone."""
    renamed = 0
    for obj in bpy.data.objects:
        if obj.type != 'ARMATURE' or obj.pose is None:
            continue
        for pb in obj.pose.bones:
            con = pb.constraints.get(_OLD_INPLACE_CONSTRAINT)
            if con is None:
                continue
            if pb.constraints.get(_NEW_INPLACE_CONSTRAINT) is not None:
                pb.constraints.remove(con)   # both present: keep the new one
            else:
                con.name = _NEW_INPLACE_CONSTRAINT
            renamed += 1
    return renamed


def run() -> None:
    """Migrate the current file. Safe to call on every load; never raises."""
    props = 0
    scenes = 0
    constraints = 0
    try:
        for coll_name in _ID_COLLECTIONS:
            coll = getattr(bpy.data, coll_name, None)
            if coll is None:
                continue
            for datablock in coll:
                props += _migrate_custom_props(datablock)

        for scene in bpy.data.scenes:
            if _migrate_scene_settings(scene):
                scenes += 1

        constraints += _migrate_inplace_constraints()
    except Exception as exc:                                  # noqa: BLE001
        # A partially migrated file still loads; better that than an
        # exception escaping a load_post handler.
        print(f"[animatica] migration from Proscenium data stopped early: {exc}")
        return

    if props or scenes or constraints:
        print(
            f"[animatica] migrated Proscenium data: {props} custom propert"
            f"{'y' if props == 1 else 'ies'}, {scenes} scene setting"
            f"{'' if scenes == 1 else 's'}, {constraints} constraint"
            f"{'' if constraints == 1 else 's'}"
        )
