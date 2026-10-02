# SPDX-License-Identifier: GPL-3.0-or-later
"""The look the floating bar and the timeline share: Animatica's colours,
the rounded tile, the icons.

Colours carry meaning, one job each, the same in both places:

* Eerie Black #1E1E1E -- the ground the bar and the lane sit on.
* Soft Orange #F0A157 -- the thing to do now, and the take: Generate,
  Accept, the strip under the blocks a take covers, the sweep while one is
  being made. Text and icons on it are dark: white on it does not read.
* Mulberry Mauve Black #423959 -- on: a switch that is on, the block under
  the playhead (what the bar's Prompt and Redo act on).
* Tutuji Pink #E95392 -- recording, and the mark of a switch that is on.

Everything else is neutral, so those four stand out.
"""

from __future__ import annotations

import math
import os
import struct
import zlib

import bpy
import gpu
from gpu_extras.batch import batch_for_shader


def _hex(h: str, a: float = 1.0) -> tuple:
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, a)


EERIE_BLACK = _hex("1E1E1E")
SOFT_ORANGE = _hex("F0A157")
TUTUJI_PINK = _hex("E95392")
MULBERRY = _hex("423959")

GROUND = (*EERIE_BLACK[:3], 0.96)            # the bar, the lane
TILE = _hex("2D2D2F")                         # a button, a block
TILE_HOVER = _hex("38383B")
ON = MULBERRY
ON_HOVER = _hex("51476C")
PRIMARY = SOFT_ORANGE
PRIMARY_HOVER = _hex("F5B373")
ON_PRIMARY = _hex("1E1E1E")                   # text and icons on orange
REC = TUTUJI_PINK
WHITE = (0.94, 0.94, 0.96, 1.0)
MUTED = (0.94, 0.94, 0.96, 0.55)
OUTLINE_HOVER = (1.0, 1.0, 1.0, 0.30)


def ui_scale() -> float:
    return bpy.context.preferences.system.ui_scale


def with_alpha(color, a: float) -> tuple:
    return (color[0], color[1], color[2], a)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

_shader = None

# The shapes are drawn every redraw, and a batch built for each one was a new
# pair of GPU buffers every time: hundreds a frame, and under memory pressure
# one of those allocations stalled in the kernel and froze Blender. A shape is
# built once, in its own coordinates, and drawn where it goes with a
# transform; the cache is bounded, the oldest shape let go first.
from collections import OrderedDict

_batches: "OrderedDict" = OrderedDict()
_CACHE_MAX = 768
_Q = 4.0                                   # quarter-pixel keys: a shape a hair different is the same shape


def _batch(key, build):
    b = _batches.get(key)
    if b is not None:
        _batches.move_to_end(key)
        return b
    verts, tris = build()
    b = batch_for_shader(_uniform(), 'TRIS', {"pos": verts}, indices=tris)
    _batches[key] = b
    if len(_batches) > _CACHE_MAX:
        _batches.popitem(last=False)
    return b


def _draw(batch, x, y, color) -> None:
    sh = _uniform()
    gpu.state.blend_set('ALPHA')             # blf.draw leaves blending off
    sh.bind()
    sh.uniform_float("color", color)
    gpu.matrix.push()
    gpu.matrix.translate((x, y))
    batch.draw(sh)
    gpu.matrix.pop()


def clear_cache() -> None:
    _batches.clear()


def _uniform():
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    return _shader


def rounded(rect, r, color, *, left: bool = True, right: bool = True) -> None:
    """A filled rounded rectangle ``(x0, y0, x1, y1)`` in region pixels.

    ``left`` / ``right`` False square that side's corners: the buttons of a
    segmented group sit flush, and only the group's ends are rounded."""
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return
    w, h = round((x1 - x0) * _Q) / _Q, round((y1 - y0) * _Q) / _Q
    r = max(0.0, min(r, w / 2, h / 2))
    rq = round(r * _Q) / _Q

    def build():
        rr, rl = (rq if right else 0.0), (rq if left else 0.0)
        pts = []
        for cx, cy, a0, rc in ((w - rr, h - rr, 0, rr), (rl, h - rl, 90, rl),
                               (rl, rl, 180, rl), (w - rr, rr, 270, rr)):
            for k in range(7):
                a = math.radians(a0 + 15 * k)
                pts.append((cx + rc * math.cos(a), cy + rc * math.sin(a)))
        verts = [(w / 2, h / 2)] + pts
        tris = [(0, i, i % len(pts) + 1) for i in range(1, len(pts) + 1)]
        return verts, tris
    _draw(_batch(("rounded", w, h, rq, left, right), build), x0, y0, color)


def outline(rect, r, width, color) -> None:
    """A rounded outline, ``width`` px inside ``rect``: the ring between it and
    the rectangle inset by ``width``."""
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return
    w, h = round((x1 - x0) * _Q) / _Q, round((y1 - y0) * _Q) / _Q
    rq = round(max(0.0, min(r, w / 2, h / 2)) * _Q) / _Q
    wq = round(width * _Q) / _Q

    def build():
        seg = []
        for cx, cy, a0 in ((w - rq, h - rq, 0), (rq, h - rq, 90), (rq, rq, 180), (w - rq, rq, 270)):
            for k in range(7):
                a = math.radians(a0 + 15 * k)
                seg.append((cx, cy, math.cos(a), math.sin(a)))
        outer = [(cx + rq * c, cy + rq * s_) for cx, cy, c, s_ in seg]
        ri = max(0.0, rq - wq)
        inner = [(cx + ri * c, cy + ri * s_) for cx, cy, c, s_ in seg]
        if rq < wq:                            # (nearly) square corners: clamp the inner ring
            inner = [(min(max(x, wq), w - wq), min(max(y, wq), h - wq)) for x, y in outer]
        verts, tris = [], []
        n = len(outer)
        for i in range(n):
            verts += [outer[i], inner[i]]
        for i in range(n):
            a_, b_ = 2 * i, 2 * ((i + 1) % n)
            tris += [(a_, a_ + 1, b_), (b_, a_ + 1, b_ + 1)]
        return verts, tris
    _draw(_batch(("outline", w, h, rq, wq), build), x0, y0, color)


def polygon(points, color) -> None:
    """A filled convex polygon. The same shape again (a handle's dot, a
    marker) is drawn from the shape already built, wherever it is."""
    if len(points) < 3:
        return
    pts = [(float(p[0]), float(p[1])) for p in points]
    ox = min(p[0] for p in pts)
    oy = min(p[1] for p in pts)
    rel = tuple((round((x - ox) * _Q), round((y - oy) * _Q)) for x, y in pts)

    def build():
        verts = [(x / _Q, y / _Q) for x, y in rel]
        return verts, [(0, i, i + 1) for i in range(1, len(verts) - 1)]
    _draw(_batch(("poly", rel), build), ox, oy, color)


def triangles(verts, tris, color) -> None:
    """Many triangles in one batch (a filled curve): one draw, not one per piece."""
    if not tris:
        return
    sh = _uniform()
    gpu.state.blend_set('ALPHA')
    sh.bind()
    sh.uniform_float("color", color)
    batch_for_shader(sh, 'TRIS', {"pos": verts}, indices=tris).draw(sh)


def lines(segments, width, color) -> None:
    """Straight segments ``[((x0, y0), (x1, y1)), ...]`` as thin quads (GL line
    widths are not portable). A small shape drawn again and again (a grip, a
    tick, an arrow) is built once; a long path that changes is drawn as is."""
    segments = list(segments)
    if 0 < len(segments) <= 8:
        ox = min(min(a[0], b[0]) for a, b in segments)
        oy = min(min(a[1], b[1]) for a, b in segments)
        rel = tuple((round((a[0] - ox) * _Q), round((a[1] - oy) * _Q), round((b[0] - ox) * _Q), round((b[1] - oy) * _Q))
                    for a, b in segments)
        wq = round(width * _Q)

        def build():
            vs, ts = [], []
            for ax, ay, bx, by in rel:
                ax, ay, bx, by = ax / _Q, ay / _Q, bx / _Q, by / _Q
                dx, dy = bx - ax, by - ay
                n = math.hypot(dx, dy) or 1.0
                ox_, oy_ = -dy / n * (wq / _Q) / 2, dx / n * (wq / _Q) / 2
                k = len(vs)
                vs += [(ax + ox_, ay + oy_), (ax - ox_, ay - oy_), (bx + ox_, by + oy_), (bx - ox_, by - oy_)]
                ts += [(k, k + 1, k + 2), (k + 2, k + 1, k + 3)]
            return vs, ts
        _draw(_batch(("lines", rel, wq), build), ox, oy, color)
        return
    verts, tris = [], []
    for (ax, ay), (bx, by) in segments:
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        ox, oy = -dy / n * width / 2, dx / n * width / 2
        k = len(verts)
        verts += [(ax + ox, ay + oy), (ax - ox, ay - oy), (bx + ox, by + oy), (bx - ox, by - oy)]
        tris += [(k, k + 1, k + 2), (k + 2, k + 1, k + 3)]
    if not verts:
        return
    sh = _uniform()
    gpu.state.blend_set('ALPHA')
    sh.bind()
    sh.uniform_float("color", color)
    batch_for_shader(sh, 'TRIS', {"pos": verts}, indices=tris).draw(sh)


# ---------------------------------------------------------------------------
# Icons: icons/*.png (from icons/src/*.svg, `make icons`), read without bpy.data
# ---------------------------------------------------------------------------

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")


def read_png(path: str):
    """A non-interlaced 8-bit RGBA PNG as ``(width, height, floats 0..1)``,
    bottom row first, as a GPU texture wants."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, color = struct.unpack(">IIBB", chunk[:10])
            if depth != 8 or color != 6:
                raise ValueError("expected 8-bit RGBA")
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride, bpp = w * 4, 4
    rows, prev = [], bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    flat = []
    for line in reversed(rows):
        flat.extend(v / 255.0 for v in line)
    return w, h, flat


_textures: dict = {}


def texture(name: str):
    if name not in _textures:
        try:
            w, h, flat = read_png(os.path.join(ICON_DIR, name + ".png"))
            buf = gpu.types.Buffer('FLOAT', len(flat), flat)
            _textures[name] = gpu.types.GPUTexture((w, h), format='RGBA16F', data=buf)
        except Exception:                       # noqa: BLE001 -- a missing icon draws nothing
            _textures[name] = None
    return _textures[name]


def icon(name: str, cx: float, cy: float, size: float, color) -> None:
    """An icon centred on ``(cx, cy)``, ``size`` px square, tinted ``color``."""
    tex = texture(name)
    if tex is None:
        return
    shader = gpu.shader.from_builtin('IMAGE_COLOR')
    h = round(size * _Q) / (2 * _Q)
    key = ("icon", h)
    batch = _batches.get(key)
    if batch is None:
        # one quad per size, drawn where it goes (built per icon per frame, it
        # was the bar's biggest allocator)
        batch = batch_for_shader(shader, 'TRI_FAN', {
            "pos": ((-h, -h), (h, -h), (h, h), (-h, h)),
            "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1)),
        })
        _batches[key] = batch
        if len(_batches) > _CACHE_MAX:
            _batches.popitem(last=False)
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_sampler("image", tex)
    shader.uniform_float("color", color)
    gpu.matrix.push()
    gpu.matrix.translate((cx, cy))
    batch.draw(shader)
    gpu.matrix.pop()


def clear() -> None:
    """Forget the GPU textures (on unregister)."""
    _textures.clear()
