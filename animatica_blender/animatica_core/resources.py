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

_MANUAL_DIR = "manual"
_MANUAL_BY_HOST = {
    "mobu": "Animatica_MotionBuilder_User_Manual.html",
    "max": "Animatica_3dsMax_User_Manual.html",
}
_MANUAL_FALLBACK = _MANUAL_BY_HOST["mobu"]


def example_prompts_dir() -> str:
    """Absolute path of the example prompt-file folder (``example/prompts``)."""
    return os.path.join(_PKG_DIR, "example", "prompts")


def manual_html_path() -> str:
    """Absolute path of the user-manual HTML file **for the registered host**.

    One core, one ``manual/`` folder, but one manual per host: until this was
    keyed on ``host.key()``, the 3ds Max plugin's "User Manual..." menu opened
    the MotionBuilder documentation -- screenshots, menu names and all -- which
    is worse than no manual, because it reads as authoritative.

    The MotionBuilder HTML is the fallback for every case that cannot be
    answered: no host registered (menu code built before ``host.register()``
    runs, and the resource tests, which call every helper bare), a host with no
    entry here, or an entry whose file this vendored copy does not carry yet.
    A manual in the wrong host's dialect beats a dialog pointed at nothing.
    """
    from . import host                      # function-local: see settings.py
    name = _MANUAL_BY_HOST.get(host.key()) if host.is_registered() else None
    if name:
        path = os.path.join(_PKG_DIR, _MANUAL_DIR, name)
        if os.path.exists(path):
            return path
    return os.path.join(_PKG_DIR, _MANUAL_DIR, _MANUAL_FALLBACK)
