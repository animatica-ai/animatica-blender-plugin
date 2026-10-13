# SPDX-License-Identifier: GPL-3.0-or-later
"""The waypoint gizmo: drag waypoints in any mode, pose mode included.

Waypoint markers are plain empties, so in pose mode the 3D cursor's own tools
can't reach them: the armature owns the selection. The gizmo answers for the
ring round each marker (not its inside: the first waypoint stands under the
character, and its feet need those clicks), selects a waypoint on a click and
moves the group along the ground on a drag, whatever the mode.

The maths at the top (`ground_hit`, `ring_distance_px`, `apply_drag`) needs no
``bpy.context`` and is tested headless; the gizmo classes below use it.
"""

from __future__ import annotations

import math

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import ui_style
from . import waypoints

#: How close (in pixels) the cursor must be to a ring to grab it.
RING_PX = 6

_EPS = 1e-9


def ground_hit(origin, direction, z) -> Vector | None:
    """Where the ray meets the plane at height ``z``.

    ``None`` when the ray runs parallel to the plane or the hit lies behind the
    origin.
    """
    o = Vector(origin)
    d = Vector(direction)
    if abs(d.z) < _EPS:
        return None
    t = (z - o.z) / d.z
    if t < 0.0:
        return None
    return o + d * t


def _segment_distance(p, a, b) -> float:
    abx, aby = b[0] - a[0], b[1] - a[1]
    len2 = abx * abx + aby * aby
    if len2 < _EPS:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / len2
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + abx * t), p[1] - (a[1] + aby * t))


def ring_distance_px(center_2d, edge_points_2d, mouse) -> float:
    """Distance from the cursor to the ring on screen, in pixels.

    The ring is the closed polyline through ``edge_points_2d``: points on the
    circle projected to the screen. In perspective a circle on the ground is an
    ellipse, so the distance is taken to those points, not to a radius from
    ``center_2d`` (kept for the caller's symmetry; the distance doesn't use it).
    """
    pts = list(edge_points_2d)
    if not pts:
        return math.inf
    if len(pts) == 1:
        return math.hypot(mouse[0] - pts[0][0], mouse[1] - pts[0][1])
    return min(_segment_distance(mouse, pts[i - 1], pts[i]) for i in range(len(pts)))


def apply_drag(starts, delta_xy) -> dict:
    """New XY for every marker in the group: its start plus the drag delta."""
    dx, dy = delta_xy
    return {name: (x + dx, y + dy) for name, (x, y) in starts.items()}


# ---------------------------------------------------------------------------
# The gizmo: one for every waypoint of the current character
# ---------------------------------------------------------------------------

#: points round a ring, for the hit test and for drawing it
HIT_SEGMENTS = 24
DRAW_SEGMENTS = 48
#: how far the mouse goes before a press is a drag, not a click
CLICK_PX = 3
HIGHLIGHT_COLOR = (1.0, 1.0, 1.0, 1.0)
HIGHLIGHT_WIDTH = 4.0

#: the waypoint under the mouse, per area pointer
_hover: dict = {}
#: the drag under way, if any (one at a time: the gizmo is modal)
_drag: dict = {}


def _markers(context) -> list:
    """The current character's waypoints that are on screen to be grabbed."""
    return [o for o in waypoints.waypoints(context.scene) if o.visible_get()]


def _ring(obj, segments: int) -> list:
    """Points round the marker's circle, flat on the ground, in world space."""
    center = obj.matrix_world.translation
    r = obj.empty_display_size * obj.matrix_world.to_scale().x
    return [center + Vector((r * math.cos(a), r * math.sin(a), 0.0))
            for a in (2.0 * math.pi * i / segments for i in range(segments))]


def _cursor_on_plane(context, co, z):
    """The point under the cursor on the level plane at height *z*."""
    region, rv3d = context.region, context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, co)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, co)
    return ground_hit(origin, direction, z)


def _select_only(context, obj):
    """*obj* selected, the character's other waypoints not. Bones and the
    active object are left alone: pose mode stays."""
    for other in waypoints.waypoints(context.scene):
        if other is not obj and other.select_get():
            other.select_set(False)
    obj.select_set(True)


def _draw_rings(objs):
    from . import key_poses

    segments = []
    for obj in objs:
        pts = _ring(obj, DRAW_SEGMENTS)
        for i in range(len(pts)):
            segments += [tuple(pts[i - 1]), tuple(pts[i])]
    if not segments:
        return
    shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    gpu.state.blend_set('ALPHA')
    # no depth test: the ring that answers the mouse shows even under a foot
    gpu.state.depth_test_set('NONE')
    try:
        shader.bind()
        shader.uniform_float("viewportSize", key_poses._viewport_size())
        shader.uniform_float("lineWidth", HIGHLIGHT_WIDTH * key_poses._px())
        shader.uniform_float("color", HIGHLIGHT_COLOR)
        batch_for_shader(shader, 'LINES', {"pos": segments}).draw(shader)
    finally:
        gpu.state.blend_set('NONE')


class ANIMATICA_GT_waypoint(bpy.types.Gizmo):
    """Every waypoint's ring: says which is under the mouse, and drags it."""
    bl_idname = "ANIMATICA_GT_waypoint"

    def draw(self, context):
        # drawn only while highlighted or dragging (use_draw_hover)
        objects = context.scene.objects
        if _drag:
            names = list(_drag["starts"])
        else:
            names = [_hover.get(context.area.as_pointer(), "")]
        _draw_rings([o for o in (objects.get(n) for n in names if n) if o is not None])

    def test_select(self, context, location):
        area, region, rv3d = context.area, context.region, context.region_data
        hit = ""
        try:
            best = RING_PX * ui_style.ui_scale()
            for obj in _markers(context):
                pts = [view3d_utils.location_3d_to_region_2d(region, rv3d, p)
                       for p in _ring(obj, HIT_SEGMENTS)]
                if any(p is None for p in pts):
                    continue                    # partly behind the view
                center = view3d_utils.location_3d_to_region_2d(
                    region, rv3d, obj.matrix_world.translation)
                d = ring_distance_px(center, pts, location)
                if d <= best:                   # the nearest ring wins an overlap
                    best, hit = d, obj.name
        except Exception:                       # noqa: BLE001
            pass
        if _hover.get(area.as_pointer()) != hit:
            _hover[area.as_pointer()] = hit
            area.tag_redraw()
        return 0 if hit else -1

    def invoke(self, context, event):
        grabbed = context.scene.objects.get(_hover.get(context.area.as_pointer(), ""))
        if grabbed is None:
            return {'CANCELLED'}
        # a selected waypoint takes the character's other selected ones along
        if grabbed.select_get():
            group = [o for o in _markers(context) if o.select_get()]
        else:
            group = [grabbed]
        z = grabbed.matrix_world.translation.z
        co = (event.mouse_region_x, event.mouse_region_y)
        _drag.clear()
        _drag.update(
            grabbed=grabbed.name,
            starts={o.name: tuple(o.location) for o in group},
            z=z,
            anchor=_cursor_on_plane(context, co, z),
            press=co,
            shift=event.shift,
            moved=False,
        )
        return {'RUNNING_MODAL'}

    def modal(self, context, event, tweak):
        if not _drag:
            return {'RUNNING_MODAL'}
        co = (event.mouse_region_x, event.mouse_region_y)
        grabbed = context.scene.objects.get(_drag["grabbed"])
        if grabbed is None:
            return {'RUNNING_MODAL'}
        if not _drag["moved"]:
            px, py = _drag["press"]
            if math.hypot(co[0] - px, co[1] - py) < CLICK_PX:
                return {'RUNNING_MODAL'}
            _drag["moved"] = True
            if not grabbed.select_get():
                _select_only(context, grabbed)
        hit = _cursor_on_plane(context, co, _drag["z"])
        if hit is None:
            return {'RUNNING_MODAL'}            # the plane is edge-on or behind
        if _drag["anchor"] is None:
            _drag["anchor"] = hit               # pressed where the plane wasn't
        anchor = _drag["anchor"]
        delta = (hit.x - anchor.x, hit.y - anchor.y)
        starts = {n: (s[0], s[1]) for n, s in _drag["starts"].items()}
        # XY only: the height follows the ground (waypoints._on_depsgraph)
        for name, (x, y) in apply_drag(starts, delta).items():
            obj = context.scene.objects.get(name)
            if obj is not None:
                obj.location.x, obj.location.y = x, y
        context.area.header_text_set(f"{grabbed.name}  Δ {math.hypot(*delta):.2f} m")
        return {'RUNNING_MODAL'}

    def exit(self, context, cancel):
        drag = dict(_drag)
        _drag.clear()
        try:
            if not drag:
                return
            objects = context.scene.objects
            if cancel:
                if drag["moved"]:
                    for name, start in drag["starts"].items():
                        obj = objects.get(name)
                        if obj is not None:
                            obj.location = start
            elif drag["moved"]:
                bpy.ops.ed.undo_push(message="Move Waypoint")
            else:
                # a click: select this one (Shift: toggle it)
                grabbed = objects.get(drag["grabbed"])
                if grabbed is not None:
                    if drag["shift"]:
                        grabbed.select_set(not grabbed.select_get())
                    else:
                        _select_only(context, grabbed)
        finally:
            if context.area is not None:
                context.area.header_text_set(None)
            waypoints.tag_redraw()


class ANIMATICA_GGT_waypoints(bpy.types.GizmoGroup):
    bl_idname = "ANIMATICA_GGT_waypoints"
    bl_label = "Animatica Waypoints"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    # '3D': draw() runs in world space; the hit test stays in region pixels,
    # since a gizmo with test_select and no draw_select is tested in 2D
    bl_options = {'3D', 'PERSISTENT'}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (waypoints._visible(context) and space.show_gizmo
                and bool(waypoints.waypoints(context.scene)))

    @classmethod
    def setup_keymap(cls, keyconfig):
        # Activate on press, not on drag (the default "Drag" preference): a
        # click on a ring selects the waypoint and must not reach the bones.
        km = keyconfig.keymaps.find("Generic Gizmo", space_type='EMPTY', region_type='WINDOW')
        if km is None:
            km = keyconfig.keymaps.new("Generic Gizmo", space_type='EMPTY', region_type='WINDOW')
        return km

    def setup(self, context):
        self.ring = self.gizmos.new(ANIMATICA_GT_waypoint.bl_idname)
        self.ring.use_draw_modal = True
        self.ring.use_draw_hover = True


_classes = (ANIMATICA_GT_waypoint, ANIMATICA_GGT_waypoints)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    _hover.clear()
    _drag.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
