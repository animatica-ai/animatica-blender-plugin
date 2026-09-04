"""Package-relative content paths for animatica_core.

Single home for locating the content core itself ships (example prompts, user
manual) so menu and dialog code never hand-builds paths. Pure CPython -- no
pyfbsdk, no Qt. All helpers return absolute paths.

Every path named here exists inside this package. That is the rule, not a
coincidence: core is *vendored* into each plugin, so a helper pointing at a
folder core does not carry would resolve to a missing directory in every host
at once, and the failure (an empty dialog listing) is silent. Host-owned demo
content -- MotionBuilder's example scenes and animatic character, 37 MB of FBX
and PNG -- therefore stays in the plugin that ships it, and is addressed from
there. Only the 32 KB of host-neutral prompt JSON travels with core, because
that is what the shared ``gui.example_prompts_dialog`` reads.
"""

import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def example_prompts_dir() -> str:
    """Absolute path of the example prompt-file folder (``example/prompts``)."""
    return os.path.join(_PKG_DIR, "example", "prompts")


def manual_html_path() -> str:
    """Absolute path of the user-manual HTML file."""
    return os.path.join(_PKG_DIR, "manual", "Animatica_MotionBuilder_User_Manual.html")
