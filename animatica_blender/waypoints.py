# SPDX-License-Identifier: GPL-3.0-or-later
"""Root waypoints: "at frame 48, stand here".

The root path used to be a curve the character was dragged along: its length
set the speed, the clip set the duration, the whole thing was pinned every six
frames, and a "sample density" slider stood in for the question the artist was
actually asking — *when* should it be *where*. The timing was hidden, and the
pinning fought the gait: a root forced along a schedule the legs never agreed
to slides its feet.

Waypoints are the MotionBuilder plugin's answer, ported. A waypoint is a circle
on the ground tied to one frame. Add one at the playhead; drag it where the
character should be; the lines between them show the route and the timeline
shows when. Only the waypoint frames go out, one single-frame ``root_path``
each, so between them the model plans its own stride and its own speed.

Placement follows the same rules as the plugin (``place_new_waypoint``, a
verbatim port): a new waypoint lands where the character actually stands at
that frame, unless that spot already has a pin — then it interpolates between
its neighbours by frame, or extends a stride along the path.

Facing is not sent by default, again as in the plugin: forcing a heading at
every waypoint over-constrains turns. The model faces the way it walks.
"""

from __future__ import annotations

import math

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

#: Identity of a waypoint marker, and the character it steers.
PROP_IS_WAYPOINT = "animatica_is_waypoint"
PROP_OWNER = "animatica_waypoint_owner"

COLLECTION_NAME = "Animatica Waypoints"

#: The marker circle, in metres — the plugin's size, big enough to grab.
MARKER_RADIUS = 0.225
MARKER_LIFT = 0.01

#: Placement, as in the plugin: a stride between neighbours, and how close a
#: sampled position must be to an existing pin to count as sitting on it.
DEFAULT_SPACING_M = 0.75
DEFAULT_COINCIDE_M = 0.10

#: Forward on the ground plane. Blender characters face -Y; the MMCP wire's
#: forward is +Z, and coords maps (x, y, z) -> (x, z, -y).
_FORWARD = (0.0, -1.0)

LINE_COLOR = (1.0, 0.78, 0.25, 0.95)
LINE_WIDTH = 2.5
LABEL_COLOR = (1.0, 0.86, 0.45, 1.0)
LABEL_SIZE = 11


# ---------------------------------------------------------------------------
# Placement — ported verbatim from animatica-mobu-plugin
# ---------------------------------------------------------------------------

def _normalise(dx, dy):
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    return dx / length, dy / length


def place_new_waypoint(frame, existing, sampled=None, *,
                       spacing_m=DEFAULT_SPACING_M, coincide_m=DEFAULT_COINCIDE_M):
    """``(x, y)`` on the ground for a new waypoint at *frame*, or ``None``.

    *existing* is ``[(frame, (x, y))]``; one already at *frame* is ignored,
    since adding there replaces it. *sampled* is where the character stands
    now, and wins unless it sits on an existing pin — the sign the rig is not
    moving and the sample says nothing new. Otherwise: between two waypoints,
    interpolate by frame; before the first or after the last, continue outward
    along the path a stride; with only one, step forward.
    """
    others = sorted(((int(f), (float(p[0]), float(p[1])))
                     for f, p in (existing or []) if int(f) != int(frame)),
                    key=lambda item: item[0])

    if sampled is not None:
        sx, sy = float(sampled[0]), float(sampled[1])
        if all(math.hypot(sx - x, sy - y) >= coincide_m for _, (x, y) in others):
            return (sx, sy)

    if not others:
        return None

    frame = int(frame)
    before = [item for item in others if item[0] < frame]
    after = [item for item in others if item[0] > frame]

    if before and after:
        (f0, (x0, y0)), (f1, (x1, y1)) = before[-1], after[0]
        span = f1 - f0
        t = (frame - f0) / span if span else 0.5
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    if after:
        anchor, neighbour = after[0], (after[1] if len(after) > 1 else None)
    else:
        anchor, neighbour = before[-1], (before[-2] if len(before) > 1 else None)

    (ax, ay) = anchor[1]
    direction = None
    if neighbour is not None:
        direction = _normalise(ax - neighbour[1][0], ay - neighbour[1][1])
    if direction is None:
        direction = _FORWARD
        if after:
            direction = (-direction[0], -direction[1])
    return (ax + direction[0] * spacing_m, ay + direction[1] * spacing_m)


# ---------------------------------------------------------------------------
# The markers
# ---------------------------------------------------------------------------

def is_waypoint(obj) -> bool:
    return obj is not None and bool(obj.get(PROP_IS_WAYPOINT))


def waypoints(scene) -> list:
    """Every waypoint marker in *scene*, earliest frame first."""
    found = [o for o in scene.objects if is_waypoint(o)]
    return sorted(found, key=lambda o: int(o.animatica_waypoint_frame))


def at_frame(scene, frame: int):
    return next((o for o in waypoints(scene) if int(o.animatica_waypoint_frame) == int(frame)), None)


def marker_name(frame: int) -> str:
    return f"Path F{int(frame)}"


def _collection(scene):
    coll = bpy.data.collections.get(COLLECTION_NAME)
    if coll is None:
        coll = bpy.data.collections.new(COLLECTION_NAME)
    if coll.name not in scene.collection.children:
        scene.collection.children.link(coll)
    return coll


def create_marker(scene, frame: int, xy, owner=None):
    """A flat circle on the ground at *xy*, tied to *frame*."""
    obj = bpy.data.objects.new(marker_name(frame), None)
    obj.empty_display_type = 'CIRCLE'
    # An empty's circle is drawn in its local XZ plane — standing up, half of it
    # under the floor. A quarter turn about X lays it flat on the ground.
    obj.rotation_euler = (math.pi / 2, 0.0, 0.0)
    obj.empty_display_size = MARKER_RADIUS
    # A centimetre above the ground, not on it: a floor at z=0 z-fights the
    # circle and leaves only arcs of it showing. Height is never sent.
    obj.location = (float(xy[0]), float(xy[1]), MARKER_LIFT)
    obj.lock_location[2] = True                # a waypoint is a spot on the ground
    obj.lock_rotation = (True, True, True)
    obj.lock_scale = (True, True, True)
    obj.show_name = False
    obj[PROP_IS_WAYPOINT] = True
    if owner is not None:
        obj[PROP_OWNER] = owner.name
    obj.animatica_waypoint_frame = int(frame)
    _collection(scene).objects.link(obj)
    return obj


def _root_xy(arm, frame: int):
    """Where the character's root stands at *frame*, on the ground plane."""
    scene = bpy.context.scene
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    if root is None:
        return None
    keep = scene.frame_current
    scene.frame_set(int(frame))
    world = arm.matrix_world @ root.head
    scene.frame_set(keep)
    return (world.x, world.y)


def _on_frame_changed(self, context):
    """A waypoint was retimed: rename its marker, and redraw the route."""
    if is_waypoint(self):
        want = marker_name(self.animatica_waypoint_frame)
        if self.name != want:
            self.name = want
    tag_redraw()


# ---------------------------------------------------------------------------
# To the request
# ---------------------------------------------------------------------------

def request_constraints(scene, frame_range, *, face_along_path: bool = False) -> list[dict]:
    """One single-frame ``root_path`` per waypoint inside *frame_range*.

    Single-frame, deliberately, as in the plugin: the server conditions on a
    sparse scatter either way, and ``heading_radians`` is all-or-nothing per
    constraint — batching would make per-waypoint facing impossible.
    Waypoints outside the generation window are left out, the same rule the
    key poses follow.
    """
    from . import coords

    lo, hi = int(frame_range[0]), int(frame_range[1])
    inside = [o for o in waypoints(scene) if lo <= int(o.animatica_waypoint_frame) <= hi]
    out = []
    for obj in inside:
        x_m, _y, z_m = coords.blender_pos_to_mmcp(obj.matrix_world.translation)
        out.append({
            "type": "root_path",
            "frames": [int(obj.animatica_waypoint_frame) - lo],
            "positions_xz": [[float(x_m), float(z_m)]],
        })
    if face_along_path and len(out) >= 2:
        _assign_headings(out)
    return out


def _assign_headings(constraints: list[dict]) -> None:
    """Face each waypoint toward the next one; the last keeps the previous facing.

    The plugin's rule, and its sign: MMCP facing for heading ``h`` is
    ``(sin h, cos h)`` in XZ, so the heading of a step ``(dx, dz)`` is
    ``atan2(dx, dz)``. A step too short to have a direction reuses the last.
    """
    last = None
    for here, nxt in zip(constraints, constraints[1:]):
        (x0, z0), (x1, z1) = here["positions_xz"][0], nxt["positions_xz"][0]
        dx, dz = x1 - x0, z1 - z0
        if math.hypot(dx, dz) > 1e-6:
            last = math.atan2(dx, dz)
        if last is not None:
            here["heading_radians"] = [last]
    if last is not None:
        constraints[-1]["heading_radians"] = [last]


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _target(context):
    from . import properties

    settings = getattr(context.scene, "animatica", None)
    return properties._live_armature(settings.target_armature) if settings else None


class ANIMATICA_OT_add_waypoint(bpy.types.Operator):
    bl_idname = "animatica.add_waypoint"
    bl_label = "Add Waypoint"
    bl_description = ("Pin where the character stands at the current frame. Drag the circle "
                      "to where it should be instead — the route between waypoints is the "
                      "model's to plan")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        frame = int(scene.frame_current)
        arm = _target(context)
        existing = [(int(o.animatica_waypoint_frame), (o.location.x, o.location.y))
                    for o in waypoints(scene)]
        sampled = _root_xy(arm, frame) if arm is not None else None
        xy = place_new_waypoint(frame, existing, sampled)
        if xy is None:
            loc = arm.matrix_world.translation if arm is not None else Vector()
            xy = (loc.x, loc.y)

        # A waypoint is (frame) — adding at a frame that has one replaces it.
        old = at_frame(scene, frame)
        if old is not None:
            bpy.data.objects.remove(old, do_unlink=True)
        obj = create_marker(scene, frame, xy, owner=arm)

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        tag_redraw()
        self.report({'INFO'}, f"waypoint at frame {frame} — drag it where the character should be")
        return {'FINISHED'}


class ANIMATICA_OT_remove_waypoint(bpy.types.Operator):
    bl_idname = "animatica.remove_waypoint"
    bl_label = "Remove Waypoint"
    bl_description = "Delete this waypoint"
    bl_options = {'REGISTER', 'UNDO'}

    frame: bpy.props.IntProperty()

    def execute(self, context):
        obj = at_frame(context.scene, self.frame)
        if obj is None:
            return {'CANCELLED'}
        bpy.data.objects.remove(obj, do_unlink=True)
        tag_redraw()
        return {'FINISHED'}


class ANIMATICA_OT_go_to_waypoint(bpy.types.Operator):
    bl_idname = "animatica.go_to_waypoint"
    bl_label = "Go to Waypoint"
    bl_description = "Select this waypoint and move the playhead to its frame"
    bl_options = {'REGISTER'}

    frame: bpy.props.IntProperty()

    def execute(self, context):
        obj = at_frame(context.scene, self.frame)
        if obj is None:
            return {'CANCELLED'}
        context.scene.frame_set(int(self.frame))
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {'FINISHED'}


class ANIMATICA_OT_curve_to_waypoints(bpy.types.Operator):
    bl_idname = "animatica.curve_to_waypoints"
    bl_label = "Convert to Waypoints"
    bl_description = ("Replace this root-path curve with waypoints along it, one a second, "
                      "timed by distance — so the route is kept and the timing becomes yours")
    bl_options = {'REGISTER', 'UNDO'}

    name: bpy.props.StringProperty()

    def execute(self, context):
        from . import constraints_ui, request_builder

        scene = context.scene
        curve = bpy.data.objects.get(self.name)
        if curve is None:
            return {'CANCELLED'}
        polyline = constraints_ui._root_path_world_polyline(curve)
        if polyline is None:
            self.report({'ERROR'}, "that curve has no usable shape")
            return {'CANCELLED'}
        settings = scene.animatica
        lo, hi = request_builder.compute_frame_range(settings.prompt_blocks,
                                                     _target(context), scene)
        fps = scene.render.fps / (scene.render.fps_base or 1.0)
        step = max(1, int(round(fps)))
        frames = list(range(lo, hi + 1, step))
        if frames[-1] != hi:
            frames.append(hi)
        arc = constraints_ui._arc_lengths(polyline)
        arm = _target(context)
        for f in frames:
            point, _t = constraints_ui._point_at_distance(
                polyline, arc, arc[-1] * (f - lo) / max(1, hi - lo))
            old = at_frame(scene, f)
            if old is not None:
                bpy.data.objects.remove(old, do_unlink=True)
            create_marker(scene, f, (point.x, point.y), owner=arm)
        bpy.data.objects.remove(curve, do_unlink=True)
        tag_redraw()
        self.report({'INFO'}, f"{len(frames)} waypoints along the old path")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Drawing: the route on the ground, and which frame each circle belongs to
# ---------------------------------------------------------------------------

_NS_LINES = "_animatica_waypoint_lines_handle"
_NS_LABELS = "_animatica_waypoint_labels_handle"


def tag_redraw():
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR', 'TIMELINE'}:
                area.tag_redraw()


def _visible(context) -> bool:
    space = getattr(context, "space_data", None)
    return space is not None and space.type == 'VIEW_3D' and space.overlay.show_overlays


def _draw_lines():
    context = bpy.context
    if not _visible(context):
        return
    pts = [o.matrix_world.translation for o in waypoints(context.scene)]
    if len(pts) < 2:
        return
    from . import key_poses

    segments = []
    for a, b in zip(pts, pts[1:]):
        segments += [(a.x, a.y, a.z + 0.005), (b.x, b.y, b.z + 0.005)]
    shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    try:
        shader.bind()
        shader.uniform_float("viewportSize", key_poses._viewport_size())
        shader.uniform_float("lineWidth", LINE_WIDTH * key_poses._px())
        shader.uniform_float("color", LINE_COLOR)
        batch_for_shader(shader, 'LINES', {"pos": segments}).draw(shader)
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')


def _draw_labels():
    import blf

    context = bpy.context
    if not _visible(context):
        return
    region, rv3d = context.region, context.region_data
    if region is None or rv3d is None:
        return
    from . import key_poses

    px = key_poses._px()
    font = 0
    blf.size(font, int(LABEL_SIZE * px))
    blf.color(font, *LABEL_COLOR)
    marks = waypoints(context.scene)
    placed = []
    for i, obj in enumerate(marks):
        co = view3d_utils.location_3d_to_region_2d(region, rv3d, obj.matrix_world.translation)
        if co is None:
            continue
        text = f"F{int(obj.animatica_waypoint_frame)}"
        if i > 0:
            # The distance from the previous waypoint and the time allowed for
            # it: the two numbers that decide whether this is a walk or a run.
            prev = marks[i - 1]
            dist = (obj.matrix_world.translation - prev.matrix_world.translation).length
            frames = int(obj.animatica_waypoint_frame) - int(prev.animatica_waypoint_frame)
            fps = context.scene.render.fps / (context.scene.render.fps_base or 1.0)
            if frames > 0 and fps > 0:
                text += f"  ·  {dist / (frames / fps):.1f} m/s"
        x, y = co.x + 14 * px, co.y + 10 * px
        w, h = blf.dimensions(font, text)
        # A path that comes back to where it began puts two waypoints almost on
        # top of each other; stack the later label under the earlier one
        # rather than printing them across each other.
        while any(x < px2 + w2 and px2 < x + w and y < py2 + h2 and py2 < y + h
                  for px2, py2, w2, h2 in placed):
            y -= h + 4 * px
        placed.append((x, y, w, h))
        blf.position(font, x, y, 0)
        blf.draw(font, text)


def register_draw_handlers():
    ns = bpy.app.driver_namespace
    for key in (_NS_LINES, _NS_LABELS):
        stale = ns.pop(key, None)
        if stale is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(stale, 'WINDOW')
            except (ValueError, RuntimeError):
                pass
    ns[_NS_LINES] = bpy.types.SpaceView3D.draw_handler_add(_draw_lines, (), 'WINDOW', 'POST_VIEW')
    ns[_NS_LABELS] = bpy.types.SpaceView3D.draw_handler_add(_draw_labels, (), 'WINDOW', 'POST_PIXEL')


def unregister_draw_handlers():
    ns = bpy.app.driver_namespace
    for key in (_NS_LINES, _NS_LABELS):
        handle = ns.pop(key, None)
        if handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except (ValueError, RuntimeError):
                pass


def timeline_frames(scene) -> list[int]:
    """Waypoint frames, for pins on the timeline."""
    return [int(o.animatica_waypoint_frame) for o in waypoints(scene)]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (ANIMATICA_OT_add_waypoint, ANIMATICA_OT_remove_waypoint,
            ANIMATICA_OT_go_to_waypoint, ANIMATICA_OT_curve_to_waypoints)


def register():
    bpy.types.Object.animatica_waypoint_frame = bpy.props.IntProperty(
        name="Frame",
        description="The frame at which the character should stand on this waypoint",
        default=1,
        update=_on_frame_changed,
    )
    for cls in _classes:
        bpy.utils.register_class(cls)
    register_draw_handlers()


def unregister():
    unregister_draw_handlers()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Object.animatica_waypoint_frame
