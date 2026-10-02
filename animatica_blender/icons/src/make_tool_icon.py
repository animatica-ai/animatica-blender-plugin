"""Write the Autopose tool's icon in Blender's toolbar icon format (.dat):
'VCO\\0', size, offset, then triangles as byte coordinates and RGBA bytes per
vertex. A figure reaching up, with the handles it is posed by as dots.
Run: python3 make_tool_icon.py  (writes ../tool/ops.animatica.autopose.dat)"""
import math, os, struct

BODY = (225, 225, 225, 255)
DOT = (233, 83, 146, 255)        # Tutuji Pink: a handle that is on
tris = []


def tri(a, b, c, col):
    tris.append(((a, b, c), col))


def bar(p, q, w, col):
    (x0, y0), (x1, y1) = p, q
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1.0
    ox, oy = -dy / n * w / 2, dx / n * w / 2
    a, b, c, d = (x0 + ox, y0 + oy), (x0 - ox, y0 - oy), (x1 + ox, y1 + oy), (x1 - ox, y1 - oy)
    tri(a, b, c, col)
    tri(c, b, d, col)
    disc(p, w / 2, col, 10)
    disc(q, w / 2, col, 10)


def disc(c, r, col, n=20):
    cx, cy = c
    pts = [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]
    for k in range(n):
        tri((cx, cy), pts[k], pts[(k + 1) % n], col)


# y up, 0..255
W = 17
hip, chest, head = (122, 104), (128, 160), (132, 200)
bar(hip, chest, W, BODY)
disc(head, 22, BODY)
bar(chest, (176, 196), W - 2, BODY)           # arm reaching up
bar((176, 196), (204, 226), W - 3, BODY)
bar(chest, (88, 140), W - 2, BODY)            # other arm, down
bar((88, 140), (66, 110), W - 3, BODY)
bar(hip, (154, 58), W, BODY)                  # legs
bar((154, 58), (168, 18), W - 1, BODY)
bar(hip, (100, 56), W, BODY)
bar((100, 56), (80, 20), W - 1, BODY)
for c, r in (((212, 234), 19), ((122, 104), 17), ((66, 108), 15), ((168, 18), 15), ((80, 20), 15)):
    disc(c, r + 5, (30, 30, 30, 255))        # a dark rim, so the dots read on the figure
    disc(c, r, DOT)


def clamp(v):
    return max(0, min(255, int(round(v))))


out = bytearray(b"VCO\x00") + struct.pack("<BBBB", 255, 255, 0, 0)
for (a, b, c), _col in tris:
    for x, y in (a, b, c):
        out += struct.pack("<BB", clamp(x), clamp(y))
for _t, col in tris:
    out += bytes(col) * 3
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tool", "ops.animatica.autopose.dat")
with open(path, "wb") as fh:
    fh.write(out)
print(path, len(tris), "triangles")
