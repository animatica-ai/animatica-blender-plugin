# SPDX-License-Identifier: GPL-3.0-or-later
"""The reach of an edit, in the viewport: a falloff curve to drag.

When a pose edit carries to the frames around it (the Autopose tool with
onion skins or the wormhole on, or Auto Keying), how far it carries and how
strongly were two numbers in a popover. Here they are the curve itself,
above the floating bar: across, the frames around the one you edit; up, the
share of the edit each one takes. Drag an edge to change the Reach, the top
of the curve to change the Intensity. The ghosts' fade, the Timeline's ramp
and the next edit follow at once, and during a drag the curve shows the edit
being made (the wheel still widens it).
"""

from __future__ import annotations

import math

import blf
import bpy
import gpu
from bpy.props import StringProperty

from . import ui_style as st

WIDTH = 300.0          # logical px
HEIGHT = 56.0
GAP = 10.0             # above the floating bar (or its hint)
GRIP = 7.0             # hit slop around an edge or the top
PARTS = ("left", "right", "top")

#: an edge drag keeps the scale it started with, so the edge stays under the mouse
_fixed = {"ppf": None}
_hover: dict = {}


def _settings(context):
    return getattr(context.scene, "animatica", None)


def visible(context) -> bool:
    """While an edit would carry through time, and while one is being made."""
    from . import handles, key_poses, wormhole
    s = _settings(context)
    if s is None or key_poses.bar_mode(context) != 'POSE':
        return False
    if wormhole.propagation.get("f0") is not None or wormhole._drag["active"]:
        return True
    if not handles.tool_active(context):
        return False
    return bool(context.scene.tool_settings.use_keyframe_insert_auto or wormhole.editable(context))


def _u(context):
    return context.preferences.system.ui_scale


def geometry(context):
    """``dict`` with the panel rect, the centre x, the curve's base and
    height, pixels per frame and the frames either side shown, or None."""
    from . import toolbar
    region, area = context.region, context.area
    s = _settings(context)
    if region is None or s is None:
        return None
    u = _u(context)
    top = GAP * u + 64 * u
    try:
        bar, rects, _d = toolbar.layout(context, area, region)
        ys = [r[3] for _it, r in rects] + ([bar[3]] if bar else [])
        if ys:
            top = max(ys) + GAP * u
    except Exception:                               # noqa: BLE001
        pass
    radius = int(s.trail_radius)
    shown = max(radius + 2, 6)
    w, h = WIDTH * u, HEIGHT * u
    cx = region.width / 2
    ppf = _fixed["ppf"] or (w - 24 * u) / (2 * shown)
    x0, x1 = cx - w / 2, cx + w / 2
    y0 = top
    base = y0 + 14 * u
    return {"rect": (x0, y0, x1, y0 + h), "cx": cx, "base": base, "h": h - 24 * u,
            "ppf": ppf, "shown": shown, "radius": radius, "strength": float(s.edit_strength), "u": u}


def _curve(g):
    """Screen points of the neighbours' share across the frames shown."""
    pts = []
    n = g["shown"]
    steps = 8
    for i in range(-n * steps, n * steps + 1):
        d = i / steps
        t = abs(d) / float(g["radius"] + 1) if g["radius"] > 0 else (0.0 if d == 0 else 1.0)
        w = 0.0 if t >= 1.0 else (3 * (1 - t) ** 2 - 2 * (1 - t) ** 3)
        pts.append((g["cx"] + d * g["ppf"], g["base"] + g["h"] * w * g["strength"]))
    return pts


def _edges(g):
    off = (g["radius"] + 0.5) * g["ppf"]
    return g["cx"] - off, g["cx"] + off


def draw(context, highlighted=True):
    g = geometry(context)
    if g is None:
        return
    u = g["u"]
    hot = _hover.get(context.area.as_pointer()) if highlighted else None
    gpu.state.blend_set('ALPHA')
    x0, y0, x1, y1 = g["rect"]
    st.rounded(g["rect"], 6 * u, (*st.GROUND[:3], 0.88))
    # frame ticks, the edited frame brighter
    ticks = []
    for d in range(-g["shown"], g["shown"] + 1):
        x = g["cx"] + d * g["ppf"]
        ticks.append(((x, g["base"] - (4 if d else 7) * u), (x, g["base"])))
    st.lines(ticks, max(1.0, u), (1, 1, 1, 0.22))
    # the share each neighbour takes, filled
    pts = _curve(g)
    base = g["base"]
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        if ya > base + 0.5 or yb > base + 0.5:
            st.polygon([(xa, base), (xb, base), (xb, yb), (xa, ya)], st.with_alpha(st.SOFT_ORANGE, 0.28))
    st.lines(list(zip(pts, pts[1:])), max(1.2, 1.6 * u), st.with_alpha(st.SOFT_ORANGE, 0.95))
    # the edited frame: always the whole edit
    st.lines([((g["cx"], base), (g["cx"], base + g["h"]))], max(1.0, 1.4 * u), (1, 1, 1, 0.7))
    # the edges (Reach) and the top (Intensity), to grab
    le, re = _edges(g)
    for name, x in (("left", le), ("right", re)):
        a = 1.0 if hot == name else 0.75
        st.lines([((x, base - 2 * u), (x, base + g["h"] + 2 * u))], max(1.5, (2.6 if hot == name else 2.0) * u),
                 (*st.SOFT_ORANGE[:3], a))
    if g["radius"] > 0:
        tx, ty = g["cx"] + g["ppf"] * 0.75, base + g["h"] * g["strength"] * _share(0.75, g["radius"])
        r = (5.0 if hot == "top" else 4.0) * u
        st.polygon(_circle(tx, ty, r + 1.2 * u), (0, 0, 0, 0.55))
        st.polygon(_circle(tx, ty, r), (1, 1, 1, 0.95) if hot == "top" else st.SOFT_ORANGE)
    # what it says, in words
    blf.size(0, 9.5 * u)
    text = (f"Reach ±{g['radius']} · Intensity {round(g['strength'] * 100)}%"
            if g["radius"] else "This frame only · drag an edge out to carry it")
    w = blf.dimensions(0, text)[0]
    blf.color(0, 1, 1, 1, 0.8)
    blf.position(0, g["cx"] - w / 2, y1 - 11 * u, 0)
    blf.draw(0, text)
    gpu.state.blend_set('NONE')


def _share(d, radius):
    t = abs(d) / float(radius + 1)
    return 0.0 if t >= 1.0 else 3 * (1 - t) ** 2 - 2 * (1 - t) ** 3


def _circle(cx, cy, r, n=16):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def hit(context, x, y):
    """``"left"``, ``"right"``, ``"top"`` under (x, y), or None."""
    g = geometry(context)
    if g is None:
        return None
    u = g["u"]
    x0, y0, x1, y1 = g["rect"]
    if not (x0 - GRIP * u <= x <= x1 + GRIP * u and y0 - GRIP * u <= y <= y1 + GRIP * u):
        return None
    if g["radius"] > 0:
        tx, ty = g["cx"] + g["ppf"] * 0.75, g["base"] + g["h"] * g["strength"] * _share(0.75, g["radius"])
        if math.hypot(x - tx, y - ty) <= (5 + GRIP) * u:
            return "top"
    le, re = _edges(g)
    near = min((("left", abs(x - le)), ("right", abs(x - re))), key=lambda p: p[1])
    if near[1] <= GRIP * u and g["base"] - GRIP * u <= y <= g["base"] + g["h"] + GRIP * u:
        return near[0]
    return None


class ANIMATICA_GT_reach(bpy.types.Gizmo):
    bl_idname = "ANIMATICA_GT_reach"

    def draw(self, context):
        draw(context, True)

    def test_select(self, context, location):
        part = hit(context, *location)
        key = context.area.as_pointer()
        if _hover.get(key) != part:
            _hover[key] = part
            context.area.tag_redraw()
        return -1 if part is None else PARTS.index(part)


class ANIMATICA_GGT_reach(bpy.types.GizmoGroup):
    bl_idname = "ANIMATICA_GGT_reach"
    bl_label = "Edit Reach"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        return visible(context)

    def setup(self, context):
        self.gz = self.gizmos.new(ANIMATICA_GT_reach.bl_idname)
        for i, part in enumerate(PARTS):
            self.gz.target_set_operator("animatica.reach_drag", index=i).part = part
        self.gz.use_tooltip = True
        self.gz.use_draw_hover = False
        self.gz.use_draw_modal = True


class ANIMATICA_OT_reach_drag(bpy.types.Operator):
    """How far an edit carries to the frames around it (drag an edge) and how strongly
    they follow (drag the top). The ghosts and the Timeline follow as you drag"""
    bl_idname = "animatica.reach_drag"
    bl_label = "Edit Reach"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    part: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        if properties.part == "top":
            return "Intensity: how strongly the frames around the edited one follow it. Drag up or down"
        return "Reach: how many frames either side follow an edit. Drag out to widen, in to narrow"

    def invoke(self, context, event):
        g = geometry(context)
        if g is None:
            return {'CANCELLED'}
        s = _settings(context)
        self._was = (int(s.trail_radius), float(s.edit_strength))
        self._g = g
        _fixed["ppf"] = g["ppf"]
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        s = _settings(context)
        g = self._g
        if event.type == 'MOUSEMOVE':
            if self.part == "top":
                share = _share(0.75, max(1, s.trail_radius))
                k = (event.mouse_region_y - g["base"]) / max(1.0, g["h"] * share)
                s.edit_strength = max(0.0, min(1.0, k))
            else:
                r = int(round(abs(event.mouse_region_x - g["cx"]) / g["ppf"] - 0.5))
                s.trail_radius = max(0, min(60, r))
            _redraw(context)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            _fixed["ppf"] = None
            _redraw(context)
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            s.trail_radius, s.edit_strength = self._was
            _fixed["ppf"] = None
            _redraw(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


def _redraw(context):
    for area in context.screen.areas if context.screen else ():
        if area.type in {'VIEW_3D', 'TIMELINE', 'DOPESHEET_EDITOR'}:
            area.tag_redraw()


_classes = (ANIMATICA_GT_reach, ANIMATICA_GGT_reach, ANIMATICA_OT_reach_drag)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    _hover.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
