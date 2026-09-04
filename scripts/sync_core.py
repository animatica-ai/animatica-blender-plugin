"""Vendor the SDK into this addon. The mechanism lives in the SDK itself.

One implementation serves every host — ``animatica_core.vendoring``, the
MotionBuilder git-archive-at-a-pin mechanism promoted to core after the two
hand-written copies disagreed exactly as the audit predicted; every host
keeps a shim this size.

    python scripts/sync_core.py                  # check against the pin
    python scripts/sync_core.py --write          # re-vendor at the pin
    python scripts/sync_core.py --write --ref X  # move the pin to X

Blender ships the addon as ONE package in the zip (``make zip`` archives
``animatica_blender``), so the copy and its stamp live inside the package:
``animatica_blender/animatica_core/`` and ``animatica_blender/CORE-VERSION``.
That is what ``plugin_root`` names here.

The vendored copy judging the tree it is part of is sound: the judgement
compares bytes against a NAMED COMMIT of the SDK, so a stale checker still
compares correctly, and a checker whose own file drifted is reported by its
own comparison. Hence the import order below — the copy, when there is one,
must be the thing that judges itself.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_ROOT = os.path.join(ROOT, "animatica_blender")

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

try:
    from animatica_core.vendoring import main
except ImportError:
    # First run only: there is no vendored copy yet to run the mechanism
    # from, so borrow it from the SDK checkout. Same knob the mechanism
    # itself reads (``vendoring.DEFAULT_CORE_REPO``), spelled out because
    # importing it is precisely what failed.
    CORE_REPO = os.environ.get("ANIMATICA_CORE",
                               r"C:\_CODE\motionmcp-client-sdk")
    print(f"note: no vendored core yet -- running the mechanism from "
          f"{CORE_REPO}", file=sys.stderr)
    sys.path.insert(0, CORE_REPO)
    from animatica_core.vendoring import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(plugin_root=PLUGIN_ROOT))
