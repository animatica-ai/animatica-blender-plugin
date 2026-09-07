"""The scene seam — core's only route to a DCC.

`gui/` and parts of `core/` have to talk to the host's scene: seek the playhead,
read a joint's world transform, key a curve. Those calls are per-DCC, and this
package cannot name `mobu_bridge`, `max_bridge` or `blender_bridge`.

So the DCC plugin registers its bridge package once at startup::

    # animatica_to_max/_startup.py
    from animatica_to_max import max_bridge
    from animatica_core import bridge
    bridge.register(max_bridge)

and every call site in core reads it as if it were a normal module::

    from animatica_core.bridge import time_bridge as _tb
    _tb.goto_frame(42)

PEP 562 module ``__getattr__`` does the forwarding. That the ``from X import Y``
form resolves through it is **verified, not assumed** — ``from`` imports take a
different path (``_handle_fromlist`` tries a submodule import first), so it was
proven on both CPython 3.13 and 3ds Max's own interpreter before this design was
committed to. `tests/test_bridge.py` keeps that proof running.

Two rules make this safe, and both are enforced by tests rather than trusted:

* **Every bridge import in core must be function-local.** Registration happens at
  startup, so a module-scope ``from animatica_core.bridge import …`` would run
  before any bridge exists and fail at import time. `tests/test_core_purity.py`
  scans the whole package for that.
* **An unregistered bridge fails loudly**, naming the fix, rather than surfacing
  as an ``AttributeError`` several frames deep inside a Qt slot.
"""

from __future__ import annotations

_impl = None

_UNREGISTERED = (
    "no DCC bridge is registered — animatica_core.bridge.register(<package>) "
    "must run at plugin startup, before any GUI is built. This usually means "
    "the plugin's _startup module did not run, or it raised before registering."
)


def register(package) -> None:
    """Install the host's bridge package. Called once, at plugin startup.

    Re-registering is allowed and replaces the previous bridge — a plugin reload
    goes through this path, and refusing it would make development painful for
    no safety gain.
    """
    global _impl
    _impl = package


def unregister() -> None:
    """Drop the bridge. For tests, and for a clean plugin teardown."""
    global _impl
    _impl = None


def is_registered() -> bool:
    return _impl is not None


def get():
    """The registered bridge package itself.

    For code that needs the package rather than one of its modules — capability
    probes, diagnostics, ``hasattr`` checks against optional bridge modules.
    """
    if _impl is None:
        raise RuntimeError(_UNREGISTERED)
    return _impl


def __getattr__(name):                      # PEP 562
    if _impl is None:
        raise RuntimeError(_UNREGISTERED)
    try:
        return getattr(_impl, name)
    except AttributeError:
        raise AttributeError(
            f"the registered bridge ({getattr(_impl, '__name__', _impl)!r}) has "
            f"no module {name!r}. Either this host does not implement it — check "
            f"animatica_core.host.has() before calling — or the bridge package's "
            f"__init__ does not import it."
        ) from None


def __dir__():
    return sorted(set(globals()) | set(dir(_impl) if _impl is not None else ()))
