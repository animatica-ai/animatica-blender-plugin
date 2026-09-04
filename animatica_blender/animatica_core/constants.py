"""Shared constants for animatica_core.

Single source of truth for unit conversions and defaults used across
builder, animator, bvh_loader, and loader modules.

MotionBuilder coordinate system: Y-up, +Z forward, units in centimetres
(matches Maya). Animatica / MMCP server output is in meters and is
converted on import.
"""

M_TO_CM = 100.0       # meters -> centimetres (MotionBuilder native unit)
CM_TO_M = 0.01        # centimetres -> meters

DEFAULT_FPS = 30.0    # fallback when motion file has no fps metadata
DEFAULT_HIP_HEIGHT = 1.0  # meters; places a default skeleton on the ground plane

DEFAULT_PREFIX = "animatica"
DEFAULT_SKELETON_NAME = "soma77"
DEFAULT_SERVER_URL = "http://localhost:8000"
ANIMATICA_API_URL  = "https://api.animatica.ai"           # auth endpoints base
ANIMATICA_MMCP_URL = "https://api.animatica.ai/mmcp"      # MMCP /capabilities + /generate
ANIMATICA_DOCS_URL = "https://animatica.ai/motion-builder/docs"  # hosted user manual

# NumPy pip spec depends on the MoBu *Python* version, not the MoBu year:
#   * MoBu 2024-2026 run Python <= 3.12 and ship PySide6/shiboken6 wheels
#     compiled against NumPy 1.x -- NumPy 2.x crashes shiboken6 binary
#     import, so we pin "<2" (the 1.26.4 wheel is available there).
#   * MoBu 2027+ run Python 3.13, where NumPy 1.x has no wheel at all
#     (first cp313 wheels ship with NumPy 2.1). Their PySide6 (6.8+)
#     supports NumPy 2.x, so we require ">=2".
# Shared by scripts/install.py (which mirrors this logic standalone) and
# the runtime bootstrap in animatica_core/_bootstrap.py.
NUMPY_SPEC_LEGACY = "numpy<2"    # Python <= 3.12  (MoBu 2024-2026)
NUMPY_SPEC_MODERN = "numpy>=2"   # Python >= 3.13  (MoBu 2027+)


def numpy_spec(py_version=None):
    """pip spec for the running (or given) interpreter's Python version.

    *py_version* is an optional ``(major, minor)`` tuple; defaults to the
    current interpreter via ``sys.version_info``.
    """
    import sys
    v = tuple(py_version) if py_version else tuple(sys.version_info[:2])
    return NUMPY_SPEC_MODERN if v >= (3, 13) else NUMPY_SPEC_LEGACY


def numpy_version_ok(numpy_version, py_version=None):
    """Return True if *numpy_version* (e.g. "1.26.4") is acceptable for the
    interpreter's Python version: NumPy 1.x on Python <=3.12, NumPy >=2 on
    Python >=3.13."""
    import sys
    v = tuple(py_version) if py_version else tuple(sys.version_info[:2])
    try:
        major = int(str(numpy_version).split(".", 1)[0])
    except (ValueError, IndexError):
        return False
    return major >= 2 if v >= (3, 13) else major == 1
