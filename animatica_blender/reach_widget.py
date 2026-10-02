# SPDX-License-Identifier: GPL-3.0-or-later
"""The reach of an edit, on the floating bar: a falloff curve to drag.

When a pose edit carries to the frames around it (the Autopose tool with
onion skins or the zoetrope on, or Auto Keying), how far it carries and how
strongly were two numbers in a popover. On the bar they are a tile with the
falloff drawn in it (toolbar._draw_reach): drag it sideways for Reach, up or
down for Intensity. The ghosts' fade and the next edit follow as you drag;
Esc puts both back.
"""

from __future__ import annotations

import bpy
from bpy.props import StringProperty

#: logical px of mouse travel per frame of Reach, and for the whole Intensity
PX_PER_FRAME = 6.0
PX_FULL = 90.0
#: travel before the drag decides which way it goes
DEAD = 4.0


def _unit_share(d: float) -> float:
    """The falloff's shape across -1..1 (the edge of the reach at ±1)."""
    t = min(1.0, abs(d))
    return 3 * (1 - t) ** 2 - 2 * (1 - t) ** 3


def _redraw(context):
    for area in context.screen.areas if context.screen else ():
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class ANIMATICA_OT_reach_drag(bpy.types.Operator):
    """How far an edit spreads to the frames around it, and how strongly they follow.
    Drag sideways for Reach, up or down for Intensity. Esc restores the old values"""
    bl_idname = "animatica.reach_drag"
    bl_label = "Reach and Intensity"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    part: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        s = getattr(context.scene, "animatica", None)
        if s is None:
            return {'CANCELLED'}
        self._was = (int(s.trail_radius), float(s.edit_strength))
        self._press = (event.mouse_x, event.mouse_y)
        self._axis = ""
        context.window.cursor_modal_set('SCROLL_XY')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        s = context.scene.animatica
        u = context.preferences.system.ui_scale
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            dx, dy = event.mouse_x - self._press[0], event.mouse_y - self._press[1]
            if not self._axis:
                if max(abs(dx), abs(dy)) < DEAD * u:
                    return {'RUNNING_MODAL'}
                # one thing at a time, whichever way the drag set off
                self._axis = "x" if abs(dx) >= abs(dy) else "y"
                context.window.cursor_modal_set('MOVE_X' if self._axis == "x" else 'MOVE_Y')
            r0, k0 = self._was
            if self._axis == "x":
                s.trail_radius = max(0, min(60, r0 + int(round(dx / (PX_PER_FRAME * u)))))
            else:
                s.edit_strength = max(0.0, min(1.0, k0 + dy / (PX_FULL * u)))
            if context.area:
                context.area.header_text_set(
                    f"Reach ±{s.trail_radius} frames · Intensity {round(s.edit_strength * 100)}%"
                    "   |   Sideways: Reach   |   Up/Down: Intensity   |   Esc: Restore")
            _redraw(context)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return self._end(context)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            s.trail_radius, s.edit_strength = self._was
            return self._end(context, cancel=True)
        return {'RUNNING_MODAL'}

    def _end(self, context, cancel=False):
        context.window.cursor_modal_restore()
        if context.area:
            context.area.header_text_set(None)
        _redraw(context)
        return {'CANCELLED'} if cancel else {'FINISHED'}


def register():
    bpy.utils.register_class(ANIMATICA_OT_reach_drag)


def unregister():
    bpy.utils.unregister_class(ANIMATICA_OT_reach_drag)
