"""Direction-control helpers for live driving. Pure Python, unit-tested.

Shared by the GUI buttons and the keyboard (hold-to-move) handler: a set
of held direction names combines into one normalized XZ vector. Also
holds the timeline lookup that picks the prompt driving the character.
"""

from __future__ import annotations

import math

#: name -> (x, z) unit direction, MMCP frame (+Z forward, +X right)
DIRECTION_VECTORS = {
    "forward": (0.0, 1.0),
    "back":    (0.0, -1.0),
    "left":    (-1.0, 0.0),
    "right":   (1.0, 0.0),
}


def combine_directions(pressed):
    """Combine held direction names into one unit vector, or None.

    Opposite keys cancel; W+D gives the normalized diagonal. An empty or
    fully-cancelling set returns None (= stop moving).
    """
    x = z = 0.0
    for name in pressed:
        vec = DIRECTION_VECTORS.get(name)
        if vec is None:
            continue
        x += vec[0]
        z += vec[1]
    norm = math.hypot(x, z)
    if norm < 1e-9:
        return None
    return (x / norm, z / norm)


def prompt_at_frame(blocks, frame):
    """Text of the timeline block covering *frame*, or ``""``.

    *blocks* is ``[(text, start, end)]`` in scene frames, ends inclusive
    (a block drawn over frames 0-49 covers 49). Overlapping blocks: the
    last one in the list wins, matching the timeline's paint order.
    """
    if frame is None:
        return ""
    hit = ""
    for text, start, end in blocks:
        if start <= frame <= end:
            hit = text
    return hit
