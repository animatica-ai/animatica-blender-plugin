"""animatica-core — the DCC-agnostic half of the Animatica plugins.

Shared by the MotionBuilder, 3ds Max and (from Phase 3) Blender plugins. This
package must never import a DCC API, and never assume one is present: it has to
import cleanly under plain CPython so the test suite, linting and byte-compiling
all run headless.

Anything that needs the host goes through two seams:

* :mod:`animatica_core.bridge` — the scene. A DCC plugin calls
  ``bridge.register(its_bridge_package)`` once at startup, and every call site
  here reads ``from animatica_core.bridge import time_bridge`` and gets the
  host's implementation.
* :mod:`animatica_core.host` — identity and capabilities: where user data
  lives, what the product is called, and what this host can actually do
  (takes? a transport zoom bar? a character system?). Ask the host, never infer
  from its name.
"""

__version__ = "0.2.0"
