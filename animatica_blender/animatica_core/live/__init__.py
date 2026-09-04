"""Live ARDY streaming client (plan PLAN-ardy-mobu-live.md, F1).

Talks to the ``/stream/ardy/*`` extension of the MMCP server (an explicitly
non-standard channel next to ``/generate``): the operator drives a character
in real time (direction / speed / facing), previews it on the Core27
skeleton in the viewport and bakes recorded stretches into takes.

Module split (thread discipline is the whole game here):

    stream_client.py    network only -- urllib NDJSON reader thread and a
                        coalescing control sender; no pyfbsdk, no Qt
    session.py          clock / frame buffer / record marks; pure Python
    skeleton_adapter.py handshake skeleton block -> builder inputs; pure
    mobu_apply.py       the ONLY module touching pyfbsdk: build skeleton,
                        per-tick preview pose, bake to take

Network threads never touch the scene; the scene is only mutated from the
Qt tick on the UI thread (see gui/sections/live_section.py).
"""
