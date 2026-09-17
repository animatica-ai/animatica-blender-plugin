# SPDX-License-Identifier: Apache-2.0
"""The Autoposer, ported into Animatica — a neural control rig that runs here.

Pose a character by dragging a handful of controls: the model fills in
everything you did not pin — spine, elbows, weight shift — and a Gauss-Newton
IK pass puts the pinned joints exactly under their handles. Both run in
Blender's own process through onnxruntime, so a drag costs a few milliseconds
and nothing leaves the machine.

Ported from ``projects/autoposer/blender_addon``, which shipped it as an addon
of its own. Three things changed and nothing else:

* **Preferences.** An addon gets exactly one ``AddonPreferences``, and
  Animatica already has it. The Autoposer's settings are merged into it as
  fields (``prefs.PROPERTIES``) and drawn in a section of its own
  (``prefs.draw``). The names and defaults are untouched, and so is the data
  directory — a machine that already downloaded the model keeps using it.
* **Where its panels live.** Under the Animatica tab rather than a second
  sidebar tab, because this is now one addon.
* **Registration.** Driven from Animatica's ``register()``.

The inference package is vendored under ``vendor/autoposer_runtime`` exactly
as the standalone addon vendored it, and ``rig.json`` is the control taxonomy
generated from ``atelier.rig.control_rig``. Neither carries weights: the model
is fetched once, from Hugging Face or a folder, and cached.
"""

import bpy

from . import engine, poser, prefs  # noqa: F401


#: Addons that register the same ``autoposer.*`` operators and ``ap_*`` scene
#: properties. Two copies fight over both, and the last one registered wins —
#: which is the kind of thing that looks like the addon misbehaving.
_SUPERSEDED = ("animatica_autoposer", "autoposer_blender")


def superseded_addons() -> list[str]:
    """Any standalone Autoposer still enabled alongside this one."""
    try:
        enabled = {a.module for a in bpy.context.preferences.addons}
    except AttributeError:
        return []
    return [name for name in _SUPERSEDED if name in enabled]


def register() -> None:
    for cls in prefs.CLASSES:
        bpy.utils.register_class(cls)
    poser.register()
    clash = superseded_addons()
    if clash:
        print("[Animatica] the Autoposer is built in now — disable the standalone "
              f"addon{'s' if len(clash) > 1 else ''}: {', '.join(clash)}")


def unregister() -> None:
    poser.unregister()
    engine.unload()
    for cls in reversed(prefs.CLASSES):
        bpy.utils.unregister_class(cls)
