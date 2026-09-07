"""Skeleton-registry API.

Animatica is not restricted to a single hierarchy: the MMCP server
retargets server-side when motion is requested on a non-canonical
skeleton.  This module is the local registry the GUI and builder consult
to translate ``skeleton_name`` -> hierarchy + rest pose.

SOMA-77 data is ported verbatim from ``maya_kimodo/maya_kimodo/skeleton.py``.

No numpy dependency at import time — positions are stored as plain tuples
so the module loads cleanly inside MotionBuilder before numpy is installed.
numpy-aware callers (e.g. the animator) can pass the returned dict straight
into their existing code because ``dict[name][axis]`` indexing works on both.
"""

# ---------------------------------------------------------------------------
# SOMA-77 constants
# ---------------------------------------------------------------------------

SOMA77_HIERARCHY = [
    ("Hips", None),
    ("Spine1", "Hips"),
    ("Spine2", "Spine1"),
    ("Chest", "Spine2"),
    ("Neck1", "Chest"),
    ("Neck2", "Neck1"),
    ("Head", "Neck2"),
    ("HeadEnd", "Head"),
    ("Jaw", "Head"),
    ("LeftEye", "Head"),
    ("RightEye", "Head"),
    ("LeftShoulder", "Chest"),
    ("LeftArm", "LeftShoulder"),
    ("LeftForeArm", "LeftArm"),
    ("LeftHand", "LeftForeArm"),
    ("LeftHandThumb1", "LeftHand"),
    ("LeftHandThumb2", "LeftHandThumb1"),
    ("LeftHandThumb3", "LeftHandThumb2"),
    ("LeftHandThumbEnd", "LeftHandThumb3"),
    ("LeftHandIndex1", "LeftHand"),
    ("LeftHandIndex2", "LeftHandIndex1"),
    ("LeftHandIndex3", "LeftHandIndex2"),
    ("LeftHandIndex4", "LeftHandIndex3"),
    ("LeftHandIndexEnd", "LeftHandIndex4"),
    ("LeftHandMiddle1", "LeftHand"),
    ("LeftHandMiddle2", "LeftHandMiddle1"),
    ("LeftHandMiddle3", "LeftHandMiddle2"),
    ("LeftHandMiddle4", "LeftHandMiddle3"),
    ("LeftHandMiddleEnd", "LeftHandMiddle4"),
    ("LeftHandRing1", "LeftHand"),
    ("LeftHandRing2", "LeftHandRing1"),
    ("LeftHandRing3", "LeftHandRing2"),
    ("LeftHandRing4", "LeftHandRing3"),
    ("LeftHandRingEnd", "LeftHandRing4"),
    ("LeftHandPinky1", "LeftHand"),
    ("LeftHandPinky2", "LeftHandPinky1"),
    ("LeftHandPinky3", "LeftHandPinky2"),
    ("LeftHandPinky4", "LeftHandPinky3"),
    ("LeftHandPinkyEnd", "LeftHandPinky4"),
    ("RightShoulder", "Chest"),
    ("RightArm", "RightShoulder"),
    ("RightForeArm", "RightArm"),
    ("RightHand", "RightForeArm"),
    ("RightHandThumb1", "RightHand"),
    ("RightHandThumb2", "RightHandThumb1"),
    ("RightHandThumb3", "RightHandThumb2"),
    ("RightHandThumbEnd", "RightHandThumb3"),
    ("RightHandIndex1", "RightHand"),
    ("RightHandIndex2", "RightHandIndex1"),
    ("RightHandIndex3", "RightHandIndex2"),
    ("RightHandIndex4", "RightHandIndex3"),
    ("RightHandIndexEnd", "RightHandIndex4"),
    ("RightHandMiddle1", "RightHand"),
    ("RightHandMiddle2", "RightHandMiddle1"),
    ("RightHandMiddle3", "RightHandMiddle2"),
    ("RightHandMiddle4", "RightHandMiddle3"),
    ("RightHandMiddleEnd", "RightHandMiddle4"),
    ("RightHandRing1", "RightHand"),
    ("RightHandRing2", "RightHandRing1"),
    ("RightHandRing3", "RightHandRing2"),
    ("RightHandRing4", "RightHandRing3"),
    ("RightHandRingEnd", "RightHandRing4"),
    ("RightHandPinky1", "RightHand"),
    ("RightHandPinky2", "RightHandPinky1"),
    ("RightHandPinky3", "RightHandPinky2"),
    ("RightHandPinky4", "RightHandPinky3"),
    ("RightHandPinkyEnd", "RightHandPinky4"),
    ("LeftLeg", "Hips"),
    ("LeftShin", "LeftLeg"),
    ("LeftFoot", "LeftShin"),
    ("LeftToeBase", "LeftFoot"),
    ("LeftToeEnd", "LeftToeBase"),
    ("RightLeg", "Hips"),
    ("RightShin", "RightLeg"),
    ("RightFoot", "RightShin"),
    ("RightToeBase", "RightFoot"),
    ("RightToeEnd", "RightToeBase"),
]

# Neutral T-pose joint positions in meters, Y-up, +Z forward.
# Root (Hips) is at origin; all values are root-relative.
# Source: kimodo/assets/skeletons/somaskel77/joints.p (via maya_kimodo).
# Stored as a plain tuple-of-tuples so this module loads without numpy.
SOMA77_NEUTRAL_POSITIONS = (
    ( 0.000000,  0.000000,  0.000000),  # Hips
    (-0.000137,  0.050038, -0.000537),  # Spine1
    (-0.000137,  0.121291, -0.000836),  # Spine2
    (-0.000137,  0.196791, -0.008995),  # Chest
    (-0.001954,  0.459904, -0.014529),  # Neck1
    (-0.001954,  0.536998,  0.008497),  # Neck2
    (-0.001954,  0.598287,  0.028034),  # Head
    (-0.001918,  0.758941,  0.009680),  # HeadEnd
    (-0.001928,  0.603043,  0.058984),  # Jaw
    ( 0.030110,  0.652089,  0.103903),  # LeftEye
    (-0.034179,  0.651906,  0.103617),  # RightEye
    ( 0.016079,  0.429163,  0.042139),  # LeftShoulder
    ( 0.165278,  0.429163, -0.012884),  # LeftArm
    ( 0.452671,  0.429163, -0.012910),  # LeftForeArm
    ( 0.723611,  0.429163, -0.012884),  # LeftHand
    ( 0.746375,  0.415242,  0.019030),  # LeftHandThumb1
    ( 0.786504,  0.396961,  0.035447),  # LeftHandThumb2
    ( 0.814489,  0.396961,  0.035447),  # LeftHandThumb3
    ( 0.846297,  0.396961,  0.035447),  # LeftHandThumbEnd
    ( 0.756086,  0.423843,  0.010078),  # LeftHandIndex1
    ( 0.819732,  0.423964,  0.011864),  # LeftHandIndex2
    ( 0.856356,  0.423964,  0.011864),  # LeftHandIndex3
    ( 0.879648,  0.423964,  0.011864),  # LeftHandIndex4
    ( 0.907244,  0.422158,  0.010733),  # LeftHandIndexEnd
    ( 0.755246,  0.431573, -0.002881),  # LeftHandMiddle1
    ( 0.817153,  0.428980, -0.012906),  # LeftHandMiddle2
    ( 0.860719,  0.428980, -0.012906),  # LeftHandMiddle3
    ( 0.890687,  0.428980, -0.012906),  # LeftHandMiddle4
    ( 0.913730,  0.426034, -0.013224),  # LeftHandMiddleEnd
    ( 0.752437,  0.428626, -0.016110),  # LeftHandRing1
    ( 0.810982,  0.423764, -0.029848),  # LeftHandRing2
    ( 0.854488,  0.423764, -0.029848),  # LeftHandRing3
    ( 0.881001,  0.423764, -0.029848),  # LeftHandRing4
    ( 0.900362,  0.424541, -0.029849),  # LeftHandRingEnd
    ( 0.752266,  0.426063, -0.028888),  # LeftHandPinky1
    ( 0.803144,  0.412751, -0.046600),  # LeftHandPinky2
    ( 0.833854,  0.412752, -0.046600),  # LeftHandPinky3
    ( 0.849351,  0.412752, -0.046600),  # LeftHandPinky4
    ( 0.868799,  0.411173, -0.046028),  # LeftHandPinkyEnd
    (-0.013938,  0.428594,  0.043146),  # RightShoulder
    (-0.164310,  0.428594, -0.012310),  # RightArm
    (-0.451677,  0.428594, -0.012336),  # RightForeArm
    (-0.723013,  0.428594, -0.012310),  # RightHand
    (-0.745753,  0.414755,  0.019322),  # RightHandThumb1
    (-0.785868,  0.396480,  0.035731),  # RightHandThumb2
    (-0.813817,  0.396480,  0.035731),  # RightHandThumb3
    (-0.845655,  0.396480,  0.035731),  # RightHandThumbEnd
    (-0.755546,  0.423394,  0.010519),  # RightHandIndex1
    (-0.818965,  0.423519,  0.012302),  # RightHandIndex2
    (-0.855514,  0.423519,  0.012302),  # RightHandIndex3
    (-0.878789,  0.423519,  0.012302),  # RightHandIndex4
    (-0.906407,  0.421712,  0.011171),  # RightHandIndexEnd
    (-0.754694,  0.431060, -0.002299),  # RightHandMiddle1
    (-0.816502,  0.428472, -0.012308),  # RightHandMiddle2
    (-0.859991,  0.428472, -0.012308),  # RightHandMiddle3
    (-0.889994,  0.428472, -0.012308),  # RightHandMiddle4
    (-0.913019,  0.425528, -0.012625),  # RightHandMiddleEnd
    (-0.751870,  0.427915, -0.015398),  # RightHandRing1
    (-0.810412,  0.423054, -0.029135),  # RightHandRing2
    (-0.853800,  0.423054, -0.029135),  # RightHandRing3
    (-0.880349,  0.423054, -0.029135),  # RightHandRing4
    (-0.899685,  0.423829, -0.029136),  # RightHandRingEnd
    (-0.751677,  0.425167, -0.028151),  # RightHandPinky1
    (-0.802591,  0.411846, -0.045875),  # RightHandPinky2
    (-0.833218,  0.411846, -0.045875),  # RightHandPinky3
    (-0.848683,  0.411846, -0.045875),  # RightHandPinky4
    (-0.868134,  0.410269, -0.045303),  # RightHandPinkyEnd
    ( 0.100432, -0.084345,  0.025957),  # LeftLeg
    ( 0.100432, -0.516563,  0.017927),  # LeftShin
    ( 0.100432, -0.938114, -0.016888),  # LeftFoot
    ( 0.100432, -0.988708,  0.115427),  # LeftToeBase
    ( 0.100336, -1.005185,  0.180558),  # LeftToeEnd
    (-0.100473, -0.082953,  0.026203),  # RightLeg
    (-0.100473, -0.516575,  0.018148),  # RightShin
    (-0.100473, -0.937749, -0.016636),  # RightFoot
    (-0.100473, -0.988545,  0.116206),  # RightToeBase
    (-0.100377, -1.004888,  0.180812),  # RightToeEnd
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_SOMA77_JOINT_NAMES = [name for name, _ in SOMA77_HIERARCHY]

_REGISTRY = {
    "soma77": {
        "hierarchy": SOMA77_HIERARCHY,
        "joint_names": _SOMA77_JOINT_NAMES,
        "neutral_positions": SOMA77_NEUTRAL_POSITIONS,
    }
}


def register(name, hierarchy, rest_positions=None):
    """Register a skeleton hierarchy under *name*.

    *hierarchy* is a list of ``(joint_name, parent_name_or_None)`` tuples in
    topological order.  *rest_positions* is an optional ``{name: (x,y,z)}``
    dict **or** any sequence of 3-element sequences in meters.  Stored as-is
    (no numpy conversion) so this module has no numpy dependency.
    """
    _REGISTRY[name] = {
        "hierarchy": list(hierarchy),
        "joint_names": [n for n, _ in hierarchy],
        "neutral_positions": rest_positions,
    }


def get_joint_hierarchy(name="soma77"):
    """Return the registered hierarchy list for *name*."""
    if name not in _REGISTRY:
        raise KeyError(f"Skeleton {name!r} not registered. Known: {list(_REGISTRY)}")
    return list(_REGISTRY[name]["hierarchy"])


def get_neutral_positions(name="soma77", hip_height=1.0):
    """Return rest positions as ``{joint_name: (x, y, z)}`` in meters.

    Hips is at origin in the stored data; all positions are shifted so
    ``Hips.y == hip_height`` (default 1.0 m places the character standing).

    Works with the built-in tuple-of-tuples and with any array-like
    (including numpy arrays) passed to :func:`register`.
    """
    if name not in _REGISTRY:
        raise KeyError(f"Skeleton {name!r} not registered. Known: {list(_REGISTRY)}")
    entry = _REGISTRY[name]
    raw   = entry["neutral_positions"]
    if isinstance(raw, dict):
        return raw
    names = entry["joint_names"]
    return {
        names[i]: (float(raw[i][0]), float(raw[i][1]) + hip_height, float(raw[i][2]))
        for i in range(len(names))
    }


def list_skeletons():
    """List the names of every registered skeleton."""
    return list(_REGISTRY.keys())
