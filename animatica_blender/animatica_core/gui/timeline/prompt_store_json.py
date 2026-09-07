"""User-facing JSON file import/export for Animatica prompts.

Schema (version 3)::

    {
      "version": 3,
      "frame_offset": int,
      "segments": [
        {"text": str, "start_frame": int, "end_frame": int, "color_idx": int},
        ...
      ],
      "constraints": [
        {"frame": int, "joint": str, "type": str, "value": dict},
        ...
      ]
    }

The shape matches ``PromptTimeline.load_segments`` so loading is a direct
round-trip with no boundary translation. ``constraints`` mirrors
``core.prompt_model.ConstraintMarker`` field-for-field. Version 1 files (no
``constraints`` key) load fine -- constraints come back empty.

**Frame spaces (version 3)**: every frame in the file -- segments AND
constraint markers -- is take-LOCAL, one coherent space, so a file saved in
a take starting at frame N round-trips exactly into a take starting at M
(callers lift constraint frames into absolute with the CURRENT take offset
on load). ``frame_offset`` records the take start at save time for
provenance. Version 1/2 files stored constraint frames ABSOLUTE while
segments were take-local; they are loaded under the legacy assumption that
they were saved in a take starting at 0 (absolute == local there), which
matches the pre-v3 behavior -- no version branch is needed in the loader.

``maya_kimodo``'s sister store used (r, g, b) float tuples instead of a palette
index; a tiny adapter can be added if cross-tool interchange becomes a
requirement.
"""

import json
import os


_FILE_VERSION = 3


def save_to_file(path, segments, constraints=(), frame_offset=0):
    """Write *segments* (and optional *constraints*) to a portable JSON file.

    *segments* is an iterable of dicts shaped like::

        {"text": str, "start_frame": int, "end_frame": int, "color_idx": int}

    *constraints* is an iterable of dicts shaped like::

        {"frame": int, "joint": str, "type": str, "value": dict}

    All frames -- segment AND constraint -- must be take-LOCAL (the caller
    converts absolute marker frames before passing them in); *frame_offset*
    is the take start at save time, recorded for provenance (see module
    docstring).
    """
    payload = {
        "version": _FILE_VERSION,
        "frame_offset": int(frame_offset),
        "segments": [
            {
                "text": s.get("text", "") or "",
                "start_frame": int(s["start_frame"]),
                "end_frame": int(s["end_frame"]),
                "color_idx": int(s.get("color_idx", 0)),
            }
            for s in segments
        ],
        "constraints": [
            {
                "frame": int(c["frame"]),
                "joint": c.get("joint", "") or "",
                "type": c["type"],
                "value": c.get("value", {}) or {},
            }
            for c in constraints
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return True


def load_from_file(path, with_constraints=False):
    """Load a prompts file.

    By default returns ``[(start, end, text, color_idx), ...]`` ready for
    ``load_segments`` (unchanged v1 contract). When *with_constraints* is True,
    returns ``(segments, constraints)`` where *constraints* is a list of dicts
    ``{"frame", "joint", "type", "value"}`` (empty for version-1 files).
    Constraint frames come back take-LOCAL, the same space as the segments --
    callers lift them into absolute with the CURRENT take offset (v1/v2 files
    read identically under the saved-at-take-0 legacy assumption; see module
    docstring).
    """
    if not os.path.isfile(path):
        return ([], []) if with_constraints else []
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    segs = []
    for s in payload.get("segments", []):
        try:
            segs.append((
                int(s["start_frame"]),
                int(s["end_frame"]),
                s.get("text", "") or "",
                int(s.get("color_idx", 0)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    segs.sort(key=lambda t: t[0])

    if not with_constraints:
        return segs

    cons = []
    for c in payload.get("constraints", []):
        try:
            cons.append({
                "frame": int(c["frame"]),
                "joint": c.get("joint", "") or "",
                "type": c["type"],
                "value": c.get("value", {}) or {},
            })
        except (KeyError, TypeError, ValueError):
            continue
    cons.sort(key=lambda d: d["frame"])
    return segs, cons
