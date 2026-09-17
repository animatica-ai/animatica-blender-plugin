"""The autoposer, running where the artist is — inference code with no weights in it.

This package is what SHIPS: a Blender addon (or any DCC host) vendors it as a subdirectory and
gets a neural autoposer that runs on the machine, through onnxruntime, with no torch, no
server and no round trip. It carries no checkpoint. `poser.onnx` is account-gated and arrives
on first use (`bundle`), and onnxruntime itself arrives the same way (`ortsetup`).

    from autoposer_runtime import Engine, ensure_ready

    ok, why = ensure_ready(cache_dir=my_addon_data_dir, token=my_animatica_token)
    eng = Engine.open(cache_dir=my_addon_data_dir)
    out = eng.pose(effectors, bone_lengths=rig_bone_lengths)

Everything about the poser's contract — the effector schema, the coordinate frame, what the
tolerance means, why `err_before_cm` must be read — is in `PROTOCOL.md` next to this package.
The one thing to repeat here: send RAW bone lengths in metres and let `Skeleton.body_cond`
sanitise them. Pre-normalising a real rig against the training stats is the failure mode
every external-rig integration hits first, and the IK hides it.
"""
from .bundle import Bundle, BundleError, default_cache_dir, download, resolve  # noqa: F401
from .engine import Engine  # noqa: F401
from .geometry import Skeleton, mat_to_sixd, sixd_to_mat  # noqa: F401
from .ortsetup import ORT_REQUIREMENT, activate, ensure, install  # noqa: F401

__version__ = "0.1.0"


def ensure_ready(cache_dir=None, *, token=None, api_base=None, allow_install: bool = True,
                 allow_download: bool = True, on_progress=None):
    """Get everything in place, and say plainly what is missing if it cannot.

    Returns `(ready, message)`. `message` is None when ready, and otherwise a sentence a UI
    can show as-is — the two failures a user actually hits are "no runtime yet" and "not
    signed in", and both are recoverable without a support ticket.
    """
    try:
        if not ensure(cache_dir=cache_dir, allow_install=allow_install):
            return False, ("the local inference runtime is not installed yet "
                           f"({ORT_REQUIREMENT})")
    except Exception as e:                              # noqa: BLE001
        return False, f"could not install the local inference runtime: {e}"
    try:
        resolve(cache_dir=cache_dir, token=token, api_base=api_base,
                allow_download=allow_download, on_progress=on_progress)
    except BundleError as e:
        return False, str(e)
    except Exception as e:                              # noqa: BLE001
        return False, f"could not get the autoposer model: {e}"
    return True, None
