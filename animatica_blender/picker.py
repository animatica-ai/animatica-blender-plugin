# SPDX-License-Identifier: GPL-3.0-or-later
"""The Autoposer's handles on a picture of the character.

A card in the viewport, in the floating bar's look: the character's mesh as it
stands in its rest pose (a T-pose), seen from the front, and every Autoposer
handle on it where its joint is. Click a handle to pick it, Shift-click to add
or drop one; under the figure the picked handles are switched on or off, given
their rotation or not, and told how strictly the pose keeps to them (slack).

The handles are the Autopose tool's (see handles.py), not bones: picking one
here picks it in the viewport too, and the other way round.

Drawn as the toolbar is (see toolbar.py): one gizmo draws the card, Blender's
button gizmos under it take the clicks, hover and tooltips.
"""

from __future__ import annotations

import math
import time

import blf
import bpy
import gpu
import numpy as np
from bpy.props import EnumProperty, StringProperty
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

from . import ui_style as st
from . import handles


def pose_bone_is_selected(h) -> bool:
    """Picked: the handle's own selection (handles are not bones any more)."""
    return bool(h.select)


def pose_bone_select_set(h, value: bool) -> None:
    h.select = bool(value)

# logical px (× UI scale)
CARD_W = 196
PAD = 10
HEAD_H = 22
FIG_MAX = 230
FIG_MIN = 64
CAPTION_H = 14
ROW_H = 24
GAP = 6
RADIUS = 8
DOT = 5.5
MARGIN = 14
#: below the viewport's own text in the top left (view name, object and frame)
TOP_TEXT = 66
TEXT = 11
SMALL = 9

#: Slack, as the handles' own properties allow it (metres; the turn's is unitless).
SLACK_MIN, SLACK_MAX = 0.001, 0.5

FIGURE = st._hex("4E4A59")
DOT_ON = st.TUTUJI_PINK
DOT_OFF = (1.0, 1.0, 1.0, 0.45)
#: Above the head, where the Look-at handle is drawn (metres): it aims, it is not on the body.
AIM_ABOVE = 0.22

#: Adding: the handles the rig can still have, drawn where they would go.
_adding = {"on": False}
#: the last handle clicked, and when: a second click on it soon after switches it
_click = {"bone": "", "at": 0.0, "picked": ()}
DOUBLE_CLICK = 0.35
#: and for the rest: how many cover each (a gizmo's click area is round)
SLOTS = {"collapse": 4, "add": 1, "all": 1, "close": 1, "on": 2, "off": 2, "rot": 2,
         "remove": 1, "slack_pos": 8, "slack_rot": 8}

_figure = {"key": None, "batch": None, "bbox": None, "joints": {}}
#: the gizmos per area (hover), and the last layout per area (the slider's drag)
_live: dict = {}
_last: dict = {}


def _ui() -> float:
    return bpy.context.preferences.system.ui_scale


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------

def _arm(context):
    from . import properties
    s = getattr(context.scene, "animatica", None)
    return properties._live_armature(s.target_armature) if s is not None else None


def _controls(arm):
    from .autoposer import poser
    return handles.refs(bpy.context.scene, arm)


def picked(arm) -> list:
    """The handles picked (their control pose bones)."""
    return [pb for pb in _controls(arm) if pose_bone_is_selected(pb)]


def _label(pb) -> str:
    from .autoposer import poser
    return poser.control_label(pb.bone.ap_joint, pb.bone.get("ap_kind") or "")


def shown(context) -> bool:
    from .autoposer import poser
    s = getattr(context.scene, "animatica", None)
    if s is None or not getattr(s, "show_picker", True):
        return False
    arm = _arm(context)
    return arm is not None and handles.has(context.scene, arm)


# ---------------------------------------------------------------------------
# The figure: the mesh in rest pose, front on, and where each handle sits
# ---------------------------------------------------------------------------

def _bodies(arm):
    return [o for o in bpy.context.scene.objects
            if o.type == 'MESH' and o.find_armature() == arm and o.data is not None]


def _axes(arm, A):
    """``(lateral, up, sign)``: which poser axes are across and up the body,
    and the sign that puts the character's left on the right of the card --
    facing the viewer, as a picker shows it."""
    from .autoposer import poser

    def rest(joint):
        b = poser.joint_bone(arm, joint)
        return None if b is None else np.array(A @ b.head_local)

    lh, rh, head, foot = rest("LeftHand"), rest("RightHand"), rest("Head"), rest("LeftFoot")
    lateral, up, sign = 0, 1, 1.0
    if head is not None and foot is not None:
        up = int(np.argmax(np.abs(head - foot)))
    if lh is not None and rh is not None:
        d = lh - rh
        d[up] = 0.0
        lateral = int(np.argmax(np.abs(d)))
        sign = 1.0 if d[lateral] > 0 else -1.0
    if lateral == up:
        lateral = (up + 1) % 3
    return lateral, up, sign


def _figure_of(arm):
    """The figure for ``arm``, built once and kept while the rig is the same."""
    from .autoposer import poser

    bodies = _bodies(arm)
    ctrl = tuple((pb.name, pb.bone.ap_joint, pb.bone.get("ap_kind") or "") for pb in _controls(arm))
    key = (arm.name, arm.data.name, len(arm.data.bones), ctrl,
           tuple((o.name, len(o.data.vertices), len(o.data.polygons)) for o in bodies))
    if _figure["key"] == key:
        return _figure
    A = poser._frame(arm)[0]
    lateral, up, sign = _axes(arm, A)
    Am = np.array(A, dtype=np.float64)

    def project(pts_arm):
        p = pts_arm @ Am.T
        return np.stack([p[:, lateral] * sign, p[:, up]], axis=1)

    tris = []
    inv = arm.matrix_world.inverted()
    for o in bodies:
        me = o.data
        n = len(me.vertices)
        if not n:
            continue
        co = np.empty(n * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(n, 3)
        T = np.array(inv @ o.matrix_world, dtype=np.float64)
        arm_space = co @ T[:3, :3].T + T[:3, 3]
        flat = project(arm_space)
        me.calc_loop_triangles()
        m = len(me.loop_triangles)
        idx = np.empty(m * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", idx)
        tris.append(flat[idx])
    pts = np.concatenate(tris) if tris else np.zeros((0, 2))
    ground = float(pts[:, 1].min()) if len(pts) else 0.0

    def spot(bone, kind):
        p = project(np.array([bone.head_local], dtype=np.float64))[0]
        if kind == "aim":
            p = p + np.array([0.0, AIM_ABOVE])
        elif kind == "root":
            p = np.array([p[0], ground])        # on the ground under the hips
        return p

    joints = {}
    for pb in _controls(arm):
        b = pb.bone
        bone = poser.joint_bone(arm, b.ap_joint)
        if bone is not None:
            joints[pb.name] = spot(bone, b.get("ap_kind") or "")
    # and every handle the rig could have, where it would go
    for spec in poser.rig_def():
        bone = poser.joint_bone(arm, spec["joint"])
        if bone is not None and spec["name"] not in joints:
            joints["+" + spec["name"]] = spot(bone, spec["kind"])
    every = np.concatenate([pts, np.array(list(joints.values()))]) if joints else pts
    if not len(every):
        return None
    bbox = tuple(float(v) for v in (every[:, 0].min(), every[:, 1].min(),
                                    every[:, 0].max(), every[:, 1].max()))
    joints = {name: (float(p[0]), float(p[1])) for name, p in joints.items()}
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": pts.astype(np.float32)}) if len(pts) else None
    _figure.update(key=key, batch=batch, bbox=bbox, joints=joints)
    return _figure


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------

def layout(context, area, region) -> dict | None:
    """Every rect of the card, in region pixels, and each handle's dot."""
    arm = _arm(context)
    if arm is None:
        return None
    u = _ui()
    s = context.scene.animatica
    left = 0.0
    headers = 0.0
    if context.preferences.system.use_region_overlap:
        # the regions drawn over this one: the tool shelf on the left, the
        # headers along the top
        for r in area.regions:
            if r.type == 'TOOLS' and r.width > 1:
                left = r.width
            if r.type in {'HEADER', 'TOOL_HEADER'} and r.height > 1 and r.y >= region.y + region.height - r.height - 2:
                headers += r.height
    x0 = left + MARGIN * u
    x1 = x0 + CARD_W * u
    inner0, inner1 = x0 + PAD * u, x1 - PAD * u
    out = {"card": None, "dots": {}, "collapsed": bool(s.picker_collapsed)}
    fig = None if out["collapsed"] else _figure_of(arm)
    # Top left, under the viewport's own text: the floating bar has the bottom.
    # The figure as tall as the rig's proportions want (a T-pose is about as
    # wide as it is tall), within what the viewport has room for.
    # ...and above the floating bar (its height and its gap from the bottom),
    # so the slack rows never slide under it
    bar = 0.0
    try:
        from . import toolbar
        if toolbar.shown(context):
            bar = toolbar.BOTTOM + 2 * toolbar.PAD + toolbar.BUTTON + 12
    except Exception:                                  # noqa: BLE001
        pass
    room = (region.height - headers) / u - TOP_TEXT - MARGIN - 175 - bar
    want = FIG_MAX
    if fig is not None:
        bx0, by0, bx1, by1 = fig["bbox"]
        want = (CARD_W - 2 * PAD) * (by1 - by0) / max(bx1 - bx0, 1e-6) + 8
    fig_h = max(FIG_MIN, min(FIG_MAX, want, room)) * u
    body = 0.0 if out["collapsed"] else (2 * ROW_H + 4 + GAP + ROW_H + GAP + CAPTION_H + GAP) * u + fig_h + GAP * u / 2
    top = region.height - headers - TOP_TEXT * u
    y0 = top - (PAD * u + body + HEAD_H * u + PAD * u * 0.6)
    y = y0 + PAD * u
    if not out["collapsed"]:
        out["slack_rot"] = (inner0, y, inner1, y + ROW_H * u)
        y += (ROW_H + 4) * u
        out["slack_pos"] = (inner0, y, inner1, y + ROW_H * u)
        y += (ROW_H + GAP) * u
        square = ROW_H * u
        third = (inner1 - inner0 - square - 6 * u - 2 * u) / 3      # one strip, as on the bar
        for k, name in enumerate(("on", "off", "rot")):
            a = inner0 + k * (third + u)
            out[name] = (a, y, a + third, y + ROW_H * u)
        out["remove"] = (inner1 - square, y, inner1, y + ROW_H * u)
        y += (ROW_H + GAP) * u
        out["caption"] = (inner0, y, inner1, y + CAPTION_H * u)
        y += (CAPTION_H + GAP) * u
        out["figure"] = (inner0, y, inner1, y + fig_h)
        y += fig_h + GAP * u / 2
    head = (inner0, y, inner1, y + HEAD_H * u)
    out["head"] = head
    side = HEAD_H * u
    out["close"] = (inner1 - side, head[1], inner1, head[3])
    out["all"] = (inner1 - 2 * side - 2 * u, head[1], inner1 - side - 2 * u, head[3])
    out["add"] = (inner1 - 3 * side - 4 * u, head[1], inner1 - 2 * side - 4 * u, head[3])
    out["collapse"] = (inner0, head[1], out["add"][0] - 4 * u, head[3])
    y += HEAD_H * u + PAD * u * 0.6
    out["card"] = (x0, y0, x1, y)
    if out["collapsed"] or fig is None:
        return out
    fx0, fy0, fx1, fy1 = out["figure"]
    bx0, by0, bx1, by1 = fig["bbox"]
    k = 0.94 * min((fx1 - fx0) / max(bx1 - bx0, 1e-6), (fy1 - fy0) / max(by1 - by0, 1e-6))
    ox = (fx0 + fx1) / 2 - k * (bx0 + bx1) / 2
    oy = (fy0 + fy1) / 2 - k * (by0 + by1) / 2
    out["transform"] = (ox, oy, k)
    out["ghosts"] = {}
    for name, p in fig["joints"].items():
        at = (ox + k * p[0], oy + k * p[1])
        if not name.startswith("+"):
            out["dots"][name] = at
        elif _adding["on"]:
            out["ghosts"][name[1:]] = at
    _spread(out["dots"], out["ghosts"], DOT * u * 2.5, out["figure"])
    return out


def _spread(dots: dict, ghosts: dict, gap: float, box) -> None:
    """Push apart marks closer than ``gap`` (region px), in place, so each
    can be seen and clicked: the neck, the head and the collarbones are a few
    centimetres apart, and a toe is in front of its foot -- the same spot,
    seen from the front. The handles the rig has move least; the ones it
    could have give way."""
    marks = [(n, False) for n in dots] + [(n, True) for n in ghosts]
    if len(marks) < 2:
        return
    pos = np.array([dots[n] if not g else ghosts[n] for n, g in marks], dtype=np.float64)
    give = np.array([3.0 if g else 1.0 for _n, g in marks])
    for _it in range(40):
        moved = False
        for i in range(len(pos)):
            for j in range(i + 1, len(pos)):
                d = pos[j] - pos[i]
                dist = float(np.hypot(*d))
                if dist >= gap:
                    continue
                if dist < 1e-6:
                    d, dist = np.array([1.0, 0.0]), 1.0       # one on the other: side by side
                push = (gap - dist) * d / dist
                wi, wj = give[i] / (give[i] + give[j]), give[j] / (give[i] + give[j])
                pos[i] -= push * wi
                pos[j] += push * wj
                moved = True
        pos[:, 0] = np.clip(pos[:, 0], box[0] + gap / 2, box[2] - gap / 2)
        pos[:, 1] = np.clip(pos[:, 1], box[1] + gap / 2, box[3] - gap / 2)
        if not moved:
            break
    for (n, g), p in zip(marks, pos):
        (ghosts if g else dots)[n] = (float(p[0]), float(p[1]))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _text(text, x, y, size, color):
    blf.size(0, size)
    blf.color(0, *color)
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def _fit(text, width, size):
    blf.size(0, size)
    if blf.dimensions(0, text)[0] <= width:
        return text
    while text and blf.dimensions(0, text + "…")[0] > width:
        text = text[:-1]
    return text.rstrip(", ") + "…"


def _mid(rect, size):
    return (rect[1] + rect[3]) / 2 - size * 0.36



def _slack_pos(value) -> float:
    """Where ``value`` sits along the slider, 0..1 (logarithmic: a millimetre
    and a centimetre are as far apart as a centimetre and ten)."""
    v = max(SLACK_MIN, min(SLACK_MAX, float(value)))
    return math.log(v / SLACK_MIN) / math.log(SLACK_MAX / SLACK_MIN)


def _slack_at(t) -> float:
    t = max(0.0, min(1.0, t))
    return SLACK_MIN * (SLACK_MAX / SLACK_MIN) ** t


def _mm(v) -> str:
    v *= 1000.0
    return f"{v:.0f} mm" if v < 100 else f"{v / 10:.0f} cm"


def draw_card(context, highlighted: bool = True):
    area, region = context.area, context.region
    arm = _arm(context)
    if arm is None:
        return
    try:
        L = layout(context, area, region)
    except Exception:                       # noqa: BLE001 -- never break the viewport
        return
    if L is None:
        return
    _last[area.as_pointer()] = L
    _drawing["hot"] = _hover.get(area.as_pointer(), "") if highlighted else ""
    live = None
    u = _ui()
    ctrls = _controls(arm)
    sel = [pb for pb in ctrls if pose_bone_is_selected(pb)]
    n_on = sum(1 for pb in ctrls if pb.bone.ap_enabled)

    st.rounded(L["card"], RADIUS * u, st.GROUND)

    # header
    head = L["head"]
    size = TEXT * u
    chevron = "▸" if L["collapsed"] else "▾"
    col = st.WHITE if not _hot(live, "collapse") else (1, 1, 1, 1)
    _text(f"{chevron}  Handles", head[0], _mid(head, size), size, col)
    blf.size(0, size)
    w = blf.dimensions(0, f"{chevron}  Handles")[0]
    _text(f"{n_on} of {len(ctrls)} on", head[0] + w + 8 * u, _mid(head, SMALL * u), SMALL * u, st.MUTED)
    for name, glyph in (("add", "+"), ("all", "All"), ("close", "×")):
        r = L[name]
        if name == "add" and _adding["on"]:
            st.rounded(r, 4 * u, st.ON)
        elif _hot(live, name):
            st.rounded(r, 4 * u, st.TILE_HOVER)
        gs = (SMALL if name == "all" else TEXT + 3) * u
        blf.size(0, gs)
        gw = blf.dimensions(0, glyph)[0]
        _text(glyph, (r[0] + r[2] - gw) / 2, _mid(r, gs), gs, st.MUTED if not _hot(live, name) else st.WHITE)
    if L["collapsed"]:
        gpu.state.blend_set('NONE')
        return

    # the figure
    fig = _figure_of(arm)
    if fig is not None and fig["batch"] is not None and "transform" in L:
        ox, oy, k = L["transform"]
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        with gpu.matrix.push_pop():
            gpu.matrix.translate((ox, oy))
            gpu.matrix.scale((k, k))
            shader.bind()
            shader.uniform_float("color", FIGURE)
            fig["batch"].draw(shader)

    # the handles
    r = DOT * u
    hover = None
    for pb in ctrls:
        p = L["dots"].get(pb.name)
        if p is None:
            continue
        x, y = p
        hot = _hot(live, "dot:" + pb.name)
        if hot:
            hover = pb
        rr = r * (1.25 if hot else 1.0)
        # the viewport's shapes: a square for the body, a diamond for an aim, a circle else
        shape = handles._shape(pb.item, x, y, rr)
        if pose_bone_is_selected(pb):
            ring = handles._shape(pb.item, x, y, rr + 3 * u)
            st.polygon(ring, (1, 1, 1, 0.95))
            st.polygon(handles._shape(pb.item, x, y, rr + 1.5 * u), st.GROUND)
        if pb.bone.ap_enabled:
            st.polygon(shape, DOT_ON)
            if pb.bone.ap_rot:            # keeps its turn too: a dot in the dot
                st.polygon(handles._circle(x, y, rr * 0.38, 12), st.GROUND)
        else:
            st.polygon(shape, st.GROUND)
            st.lines(list(zip(shape, shape[1:] + shape[:1])), max(1.0, 1.3 * u), DOT_OFF)
    # the handle under the mouse, else what is picked, on the line under the figure
    cap = L["caption"]
    ghost_hot = None
    for name, (x, y) in L.get("ghosts", {}).items():
        hot = _hot(live, "ghost:" + name)
        if hot:
            ghost_hot = name
        rr = r * (1.25 if hot else 1.0)
        dot = (x - rr, y - rr, x + rr, y + rr)
        color = st.WHITE if hot else (1, 1, 1, 0.55)
        st.rounded(dot, rr, st.with_alpha(st.EERIE_BLACK, 0.85))
        st.outline(dot, rr, max(1.0, 1.0 * u), color)
        c = rr * 0.55
        st.lines([((x - c, y), (x + c, y)), ((x, y - c), (x, y + c))], max(1.0, 1.2 * u), color)
    if ghost_hot is not None:
        from .autoposer import poser
        spec = next((sp for sp in poser.rig_def() if sp["name"] == ghost_hot), None)
        text = f"Add {_spec_label(spec)}" if spec else "Add"
        _text(_fit(text, cap[2] - cap[0], SMALL * u), cap[0], _mid(cap, SMALL * u), SMALL * u, st.WHITE)
    elif hover is not None:
        _text(_fit(_describe(hover), cap[2] - cap[0], SMALL * u), cap[0], _mid(cap, SMALL * u),
              SMALL * u, st.WHITE)
    elif _adding["on"]:
        text = ("Click a + to add that handle" if L.get("ghosts")
                else "Every handle this rig can have is on it")
        _text(text, cap[0], _mid(cap, SMALL * u), SMALL * u, st.MUTED)
    elif sel:
        names = ", ".join(_label(pb) for pb in sel)
        _text(_fit(names, cap[2] - cap[0], SMALL * u), cap[0], _mid(cap, SMALL * u), SMALL * u, st.WHITE)
    else:
        _text(_fit("Shift: pick more · Ctrl-click: on/off", cap[2] - cap[0], SMALL * u),
              cap[0], _mid(cap, SMALL * u), SMALL * u, st.MUTED)

    # On / Off / Rot
    have = bool(sel)
    states = {
        "on": have and all(pb.bone.ap_enabled for pb in sel),
        "off": have and not any(pb.bone.ap_enabled for pb in sel),
        "rot": have and all(pb.bone.ap_rot for pb in sel),
    }
    for name, text in (("on", "On"), ("off", "Off"), ("rot", "Rotation")):
        rect = L[name]
        fill = st.ON if states[name] else (st.TILE_HOVER if _hot(live, name) and have else st.TILE)
        st.rounded(rect, 5 * u, fill if have else st.with_alpha(st.TILE, 0.18),
                   left=name == "on", right=name == "rot")
        blf.size(0, SMALL * u)
        tw = blf.dimensions(0, text)[0]
        color = (st.REC if states[name] else st.WHITE) if have else (1, 1, 1, 0.22)
        _text(text, (rect[0] + rect[2] - tw) / 2, _mid(rect, SMALL * u), SMALL * u, color)

    rect = L["remove"]
    if have:
        st.rounded(rect, 5 * u, st.TILE_HOVER if _hot(live, "remove") else st.TILE)
    else:
        st.rounded(rect, 5 * u, st.with_alpha(st.TILE, 0.18))
    st.icon("remove", (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2, 14 * u,
            st.WHITE if have else (1, 1, 1, 0.22))

    # slack
    rot_sel = [pb for pb in sel if pb.bone.ap_rot and pb.bone.ap_enabled]
    pos_sel = [pb for pb in sel if pb.bone.ap_enabled]
    _slider(L["slack_pos"], "Slack", [pb.bone.ap_tol_m for pb in pos_sel], _mm, live, "slack_pos", u)
    _slider(L["slack_rot"], "Turn slack", [pb.bone.ap_rot_tol_m for pb in rot_sel],
            lambda v: f"{v:.3f}", live, "slack_rot", u)
    gpu.state.blend_set('NONE')


def _slider(rect, label, values, fmt, live, name, u):
    """A slack dial: the label, a track filled to the value, the value."""
    have = bool(values)
    a = 1.0 if have else 0.25
    size = SMALL * u
    _text(label, rect[0], _mid(rect, size), size, (1, 1, 1, 0.8 * a))
    track = _track(rect, u)
    st.rounded(track, 2 * u, (1, 1, 1, 0.10 * a + (0.06 if _hot(live, name) and have else 0)))
    if have:
        v = values[0]
        t = _slack_pos(v)
        mixed = any(abs(x - v) > 1e-6 for x in values)
        x = track[0] + t * (track[2] - track[0])
        st.rounded((track[0], track[1], x, track[3]), 2 * u, st.with_alpha(st.TUTUJI_PINK, 0.85))
        knob = 5 * u
        cy = (track[1] + track[3]) / 2
        st.rounded((x - knob, cy - knob, x + knob, cy + knob), knob, st.WHITE)
        text = "mixed" if mixed else fmt(v)
    else:
        text = "—"
    blf.size(0, size)
    tw = blf.dimensions(0, text)[0]
    _text(text, rect[2] - tw, _mid(rect, size), size, (1, 1, 1, 0.8 * a))


def _track(rect, u):
    """The slider's track inside its row: after the label, before the value."""
    cy = (rect[1] + rect[3]) / 2
    return (rect[0] + 62 * u, cy - 2 * u, rect[2] - 44 * u, cy + 2 * u)


def _describe(pb) -> str:
    """A handle, named, with its state."""
    b = pb.bone
    if not b.ap_enabled:
        return f"{_label(pb)}  ·  off"
    return f"{_label(pb)}  ·  {_mm(b.ap_tol_m)}" + ("  ·  keeps its turn" if b.ap_rot else "")


# ---------------------------------------------------------------------------
# The gizmos: one draws the card, the rest take the clicks
# ---------------------------------------------------------------------------

#: what the mouse is over on the card, per area: "on", "dot:<bone>", "ghost:<control>", ...
_hover: dict = {}
MAX_PARTS = 64


#: what is hot in the card being drawn
_drawing = {"hot": ""}


def _hot(_live, name) -> bool:
    return _drawing["hot"] == name


def _hit(L, x, y, u):
    """What on the card is at ``(x, y)``: a key, or ''."""
    if L is None or L.get("card") is None:
        return ""
    cx0, cy0, cx1, cy1 = L["card"]
    if not (cx0 <= x <= cx1 and cy0 <= y <= cy1):
        return ""
    reach = DOT * u * 1.9
    best, best_d = "", reach
    for kind, marks in (("ghost", L.get("ghosts", {})), ("dot", L.get("dots", {}))):
        for name, (px, py) in marks.items():
            d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if d < best_d:
                best, best_d = f"{kind}:{name}", d
    if best:
        return best
    for name in SLOTS:
        rect = L.get(name)
        if rect is None or (L["collapsed"] and name not in ("collapse", "add", "all", "close")):
            continue
        if name in ("slack_pos", "slack_rot"):
            t = _track(rect, u)
            rect = (t[0] - 4 * u, rect[1], t[2] + 4 * u, rect[3])
        if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
            return name
    return "card"          # on the card, not on anything: the click stays here


class ANIMATICA_GT_picker_card(bpy.types.Gizmo):
    """The card: draws itself, and says what on it is under the mouse."""
    bl_idname = "ANIMATICA_GT_picker_card"

    def draw(self, context):
        draw_card(context, bool(self.is_highlight))

    def test_select(self, context, location):
        area, region = context.area, context.region
        try:
            L = layout(context, area, region)
        except Exception:                       # noqa: BLE001
            L = None
        key = _hit(L, location[0], location[1], _ui())
        if _hover.get(area.as_pointer()) != key:
            _hover[area.as_pointer()] = key
            area.tag_redraw()
        if not key:
            return -1
        return abs(hash(key)) % (MAX_PARTS - 1) + 1


_OPS = {
    "collapse": ("animatica.picker_collapse", {}),
    "all": ("animatica.picker_all", {}),
    "add": ("animatica.picker_adding", {}),
    "remove": ("animatica.picker_remove", {}),
    "close": ("animatica.picker_close", {}),
    "on": ("animatica.picker_set", {"what": 'ON'}),
    "off": ("animatica.picker_set", {"what": 'OFF'}),
    "rot": ("animatica.picker_set", {"what": 'ROT'}),
    "slack_pos": ("animatica.picker_slack", {"which": 'POS'}),
    "slack_rot": ("animatica.picker_slack", {"which": 'ROT'}),
}
SLOTS = list(_OPS)


def _op_for(key):
    """``(operator, props)`` for what is at ``key`` on the card, or None."""
    if key.startswith("dot:"):
        return "animatica.picker_select", {"bone": key[4:]}
    if key.startswith("ghost:"):
        return "animatica.picker_add", {"control": key[6:]}
    return _OPS.get(key)


class ANIMATICA_GGT_picker(bpy.types.GizmoGroup):
    bl_idname = "ANIMATICA_GGT_picker"
    bl_label = "Animatica Handle Picker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        return shown(context)

    def setup(self, context):
        self.card = self.gizmos.new(ANIMATICA_GT_picker_card.bl_idname)
        for part in range(MAX_PARTS):
            self.card.target_set_operator("animatica.picker_click", index=part)
        self.card.use_tooltip = True


class ANIMATICA_OT_picker_click(bpy.types.Operator):
    """The handle picker"""
    bl_idname = "animatica.picker_click"
    bl_label = "Handle Picker"
    bl_options = {'INTERNAL', 'UNDO'}  # what the click ran is one undo step: a nested operator records none

    @classmethod
    def description(cls, context, properties):
        key = _hover.get(context.area.as_pointer(), "") if context.area else ""
        found = _op_for(key)
        if not found:
            return ""
        op, props = found
        group, name = op.split(".", 1)
        cls_ = getattr(bpy.types, f"{group.upper()}_OT_{name}", None)
        title = {"collapse": "Fold", "all": "Pick All", "add": "Add a Handle", "remove": "Remove Handles",
                 "close": "Hide the Picker", "on": "Switch On", "off": "Switch Off", "rot": "Rotation",
                 "slack_pos": "Slack", "slack_rot": "Turn Slack"}.get(key)
        if cls_ is None:
            return title or ""
        if hasattr(cls_, "description"):
            class _P:                                  # the operator's own properties, as it reads them
                pass
            p = _P()
            for k, v in props.items():
                setattr(p, k, v)
            text = cls_.description(context, p)
        else:
            text = (cls_.__doc__ or "").strip()
        return f"{title}\n{text}" if title else text

    def invoke(self, context, event):
        key = _hover.get(context.area.as_pointer(), "") if context.area else ""
        found = _op_for(key)
        if not found:
            return {'CANCELLED'}
        op, props = found
        if op == "animatica.picker_select":
            props = dict(props, extend=bool(event.shift), toggle=bool(event.ctrl))  # Shift: add or drop; Ctrl: on/off
        group, name = op.split(".", 1)
        result = getattr(getattr(bpy.ops, group), name)('INVOKE_DEFAULT', **props)
        return {'CANCELLED'} if result == {'CANCELLED'} else {'FINISHED'}


# ---------------------------------------------------------------------------
# What the clicks do
# ---------------------------------------------------------------------------

def _redraw(context):
    for a in context.screen.areas:
        if a.type == 'VIEW_3D':
            a.tag_redraw()


class ANIMATICA_OT_picker_select(bpy.types.Operator):
    bl_idname = "animatica.picker_select"
    bl_label = "Pick Handle"
    bl_options = {'INTERNAL', 'UNDO'}

    bone: StringProperty()
    extend: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    toggle: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        arm = _arm(context)
        pb = handles.ref(context.scene, arm, properties.bone) if arm else None
        if pb is None:
            return "Pick this handle"
        b = pb.bone
        state = (f"on, {_mm(b.ap_tol_m)} slack" + (", keeps its rotation" if b.ap_rot else "")
                 if b.ap_enabled else "off, so the pose decides this joint")
        return (f"{_label(pb)}: {state}.\nClick to pick it, Shift-click to add it to or remove it "
                "from the picks, and double-click to switch it on or off")

    def invoke(self, context, event):
        arm = _arm(context)
        pb = handles.ref(context.scene, arm, self.bone) if arm else None
        if pb is None:
            return {'CANCELLED'}
        now = time.monotonic()
        double = _click["bone"] == pb.name and now - _click["at"] < DOUBLE_CLICK
        before = _click["picked"]
        _click.update(bone="" if double else pb.name, at=now,
                      picked=tuple(p.name for p in picked(arm)))
        shift = event.shift or self.extend
        if event.ctrl or self.toggle:
            # Ctrl-click: on or off, as on the handles in the viewport
            switch(context, arm, [pb], not pb.bone.ap_enabled)
            _redraw(context)
            return {'FINISHED'}
        if double and not shift:
            # Double-click: on or off. The picked handles together when this
            # is one of them, so a whole limb goes in one go -- picked as they
            # were before the first click of the two, which picked this alone.
            bones = [h for h in _controls(arm) if h.name in before]
            if pb in bones:
                for p_ in bones:
                    pose_bone_select_set(p_, True)
            else:
                bones = [pb]
            switch(context, arm, bones, not pb.bone.ap_enabled)
            _redraw(context)
            return {'FINISHED'}
        if shift:
            now = not pose_bone_is_selected(pb)
            pose_bone_select_set(pb, now)
        else:
            for other in _controls(arm):
                pose_bone_select_set(other, False)
            pose_bone_select_set(pb, True)
        _redraw(context)
        return {'FINISHED'}


class ANIMATICA_OT_picker_all(bpy.types.Operator):
    """Pick every handle. Click again to pick none"""
    bl_idname = "animatica.picker_all"
    bl_label = "Pick All Handles"
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        arm = _arm(context)
        if arm is None:
            return {'CANCELLED'}
        ctrls = _controls(arm)
        every = all(pose_bone_is_selected(pb) for pb in ctrls)
        for pb in ctrls:
            pose_bone_select_set(pb, not every)
        _redraw(context)
        return {'FINISHED'}


def _batch(context, arm, bones, change, *, resolve: bool, place_on=()):
    """Change several handles with one solve after, not one per handle."""
    for pb in bones:
        change(pb.bone)
    if resolve:
        handles.resolve(context, arm)


def switch(context, arm, bones, on: bool) -> None:
    """Switch handles on or off: on is inert (the handle starts where its
    joint is, nothing moves -- see poser._on_enabled), off re-solves once."""
    if on:
        off = [pb for pb in bones if not pb.bone.ap_enabled]
        _batch(context, arm, off, lambda b: setattr(b, "ap_enabled", True), resolve=False, place_on=off)
    else:
        _batch(context, arm, bones, lambda b: setattr(b, "ap_enabled", False), resolve=True)


class ANIMATICA_OT_picker_set(bpy.types.Operator):
    bl_idname = "animatica.picker_set"
    bl_label = "Set Handles"
    bl_options = {'INTERNAL', 'UNDO'}

    what: EnumProperty(items=(('ON', "On", ""), ('OFF', "Off", ""), ('ROT', "Rotation", "")))

    @classmethod
    def description(cls, context, properties):
        return {
            'ON': "Switch the picked handles on, so their joints go where the handles are",
            'OFF': "Switch the picked handles off, so the pose decides those joints",
            'ROT': "Make the picked handles hold their joints' rotation as well as their position. "
                   "Click again to let the pose decide the rotation",
        }[properties.what]

    def execute(self, context):
        arm = _arm(context)
        sel = picked(arm) if arm else []
        if not sel:
            return {'CANCELLED'}
        if self.what in {'ON', 'OFF'}:
            switch(context, arm, sel, self.what == 'ON')
        else:
            to = not all(pb.bone.ap_rot for pb in sel)
            _batch(context, arm, sel, lambda b: setattr(b, "ap_rot", to),
                   resolve=not to, place_on=() if to else sel)
        _redraw(context)
        return {'FINISHED'}


class ANIMATICA_OT_picker_slack(bpy.types.Operator):
    """How strictly the pose keeps to the picked handles. Drag left to tighten (the
    joint goes exactly there) or right to loosen (the pose may move it a little)"""
    bl_idname = "animatica.picker_slack"
    bl_label = "Handle Slack"
    bl_options = {'INTERNAL', 'UNDO'}

    which: EnumProperty(items=(('POS', "Slack", ""), ('ROT', "Turn slack", "")))

    def _bones(self, arm):
        sel = [pb for pb in picked(arm) if pb.bone.ap_enabled]
        return [pb for pb in sel if pb.bone.ap_rot] if self.which == 'ROT' else sel

    def _set(self, context, event):
        L = _last.get(context.area.as_pointer()) if context.area else None
        if not L:
            return
        track = _track(L["slack_rot" if self.which == 'ROT' else "slack_pos"], _ui())
        t = (event.mouse_region_x - track[0]) / max(track[2] - track[0], 1.0)
        value = _slack_at(t)
        prop = "ap_rot_tol_m" if self.which == 'ROT' else "ap_tol_m"
        arm = _arm(context)
        _batch(context, arm, self._bones(arm), lambda b: setattr(b, prop, value), resolve=True)
        context.area.tag_redraw()

    def invoke(self, context, event):
        arm = _arm(context)
        if arm is None or not self._bones(arm):
            return {'CANCELLED'}
        self._set(context, event)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self._set(context, event)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return {'FINISHED'}
        return {'RUNNING_MODAL'}


def _specs(arm):
    """Every handle the Autoposer has for this rig, in the rig's order: ``[(spec, have)]``."""
    from .autoposer import poser
    have = {h.name for h in handles.items(bpy.context.scene, arm)}
    out = []
    for spec in poser.rig_def():
        if poser.joint_bone(arm, spec["joint"]) is None:
            continue                            # a joint this rig does not have
        out.append((spec, spec["name"] in have))
    return out


def _spec_label(spec) -> str:
    from .autoposer import poser
    label = poser.control_label(spec["joint"], spec["kind"])
    hint = {"guide": "aims the bend", "ik": "reach", "cog": "body", "root": "on the ground",
            "aim": "where the head looks"}.get(spec["kind"], "")
    return f"{label}   ({hint})" if hint else label


class ANIMATICA_OT_picker_adding(bpy.types.Operator):
    """Add a handle. Every handle this rig can still have shows on the figure where
    it would go. Click one to add it, or click + again to stop without adding"""
    bl_idname = "animatica.picker_adding"
    bl_label = "Add Handles"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        _adding["on"] = not _adding["on"]
        _redraw(context)
        return {'FINISHED'}


class ANIMATICA_OT_picker_add(bpy.types.Operator):
    """Add this handle to the rig where its joint is now. Nothing moves until you drag it"""

    @classmethod
    def description(cls, context, properties):
        from .autoposer import poser
        spec = next((sp for sp in poser.rig_def() if sp["name"] == properties.control), None)
        return (f"Add {_spec_label(spec)}. It starts where the joint is now, and nothing "
                "moves until you drag it") if spec else "Add this handle"

    bl_idname = "animatica.picker_add"
    bl_label = "Add Handle"
    bl_options = {'INTERNAL', 'UNDO'}

    control: StringProperty()

    def execute(self, context):
        arm = _arm(context)
        if arm is None:
            return {'CANCELLED'}
        if handles.add(context.scene, arm, self.control) is None:
            return {'CANCELLED'}
        _adding["on"] = False                  # one at a time: added, and back to picking
        pb = handles.ref(context.scene, arm, self.control)
        if pb is not None:                      # picked, so it can be set up straight away
            for other in _controls(arm):
                pose_bone_select_set(other, False)
            pose_bone_select_set(pb, True)
        _redraw(context)
        return {'FINISHED'}


class ANIMATICA_OT_picker_remove(bpy.types.Operator):
    """Remove the picked handles from the rig, so the pose decides those joints again.
    Use the + in the picker's title to add them back"""
    bl_idname = "animatica.picker_remove"
    bl_label = "Remove Handles"
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        arm = _arm(context)
        names = [pb.name for pb in picked(arm)] if arm else []
        if not names:
            return {'CANCELLED'}
        gone = handles.remove(context.scene, arm, set(names))
        self.report({'INFO'}, f"Removed {gone} handle{'s' if gone != 1 else ''}")
        _redraw(context)
        return {'FINISHED'}


class ANIMATICA_OT_picker_collapse(bpy.types.Operator):
    """Fold the handle picker to its title, or open it again"""
    bl_idname = "animatica.picker_collapse"
    bl_label = "Fold Handle Picker"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        s = context.scene.animatica
        s.picker_collapsed = not s.picker_collapsed
        return {'FINISHED'}


class ANIMATICA_OT_picker_close(bpy.types.Operator):
    """Hide the handle picker. Use the picker button on the floating bar to show it again"""
    bl_idname = "animatica.picker_close"
    bl_label = "Hide Handle Picker"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        context.scene.animatica.show_picker = False
        self.report({'INFO'}, "Handle picker hidden. Use the picker button on the floating bar to show it again")
        return {'FINISHED'}


_classes = (ANIMATICA_OT_picker_click, ANIMATICA_GT_picker_card, ANIMATICA_GGT_picker,
            ANIMATICA_OT_picker_select,
            ANIMATICA_OT_picker_all, ANIMATICA_OT_picker_set, ANIMATICA_OT_picker_slack,
            ANIMATICA_OT_picker_collapse, ANIMATICA_OT_picker_close,
            ANIMATICA_OT_picker_adding, ANIMATICA_OT_picker_add,
            ANIMATICA_OT_picker_remove)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    _live.clear()
    _last.clear()
    _adding["on"] = False
    _figure.update(key=None, batch=None, bbox=None, joints={})
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
