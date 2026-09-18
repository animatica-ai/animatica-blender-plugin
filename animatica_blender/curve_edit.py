# SPDX-License-Identifier: GPL-3.0-or-later
"""Drag a motion curve; the body follows.

The trail shows where an end effector goes. This makes it the thing you grab:
pull a point on the hand's curve and the hand goes there — at *that* frame,
without the playhead moving — while the Autoposer solves the rest of the body
around it and a ghost shows you the pose you are about to commit.

Why this is possible at all
---------------------------
The Autoposer's solve is a pure function of its effector list: a handful of
joint positions in, a whole body out. It never reads the rig's current pose,
so it can be asked about frame 40 while the artist is looking at frame 12.
The trail already holds the positions of exactly the joints the poser is
steered by — hands, feet, root, head — sampled at every frame, so a drag is:

    take that frame's six positions, move the one under the cursor,
    solve, draw the answer

The other five stay pinned where they were, which is what makes a drag change
*one* effector rather than re-interpreting the whole pose.

Committing on release writes the solved pose into the action at that frame,
typed as the artist's own, so it becomes a key pose like any other — one more
full-body constraint the next generation is asked to hit.

The preview is a skeleton, not a body: a solve is milliseconds but evaluating
a deformed mesh is not, and at 60 frames a second of dragging the stick figure
is what keeps up.
"""

from __future__ import annotations

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
from mathutils.geometry import intersect_line_plane


# A control's grab radius in screen space is taken from its own size — the
# shapes range from a small circle to a double ring — clamped so it is neither
# a pinprick when zoomed out nor a wall when zoomed in.
CONTROL_RADIUS_MIN = 12.0
CONTROL_RADIUS_MAX = 44.0

# Pixels around a point that count as grabbing it. Two radii, because the
# points are not equal: a key pose is a thing the artist put there and is what
# the request carries, while the frames between are the motion that answers it,
# a dot per frame and only a few pixels apart at any sensible zoom. Grabbing
# the frame next to the one you meant was the first thing to go wrong in real
# use, so a key pose wins from much further away, and an in-between has to be
# hit almost exactly.
KEY_PICK_RADIUS = 18.0
FRAME_PICK_RADIUS = 5.0

# Tolerance in metres handed to the poser for every effector in a drag. Tight:
# the artist has just said where these joints are, so the model's job is the
# body between them, not second-guessing the handles.
DRAG_TOL = 0.005

PREVIEW_COLOR = (1.0, 0.85, 0.25, 0.95)     # the pose the drag would commit
PREVIEW_WIDTH = 2.6
TARGET_COLOR = (1.0, 1.0, 1.0, 0.95)
TARGET_RADIUS = 5.0


#: Live drag state. Module-level because the draw callback and the modal
#: operator both need it and a draw callback takes no arguments.
_drag: dict = {
    "active": False,
    "bone": "",           # rig bone being dragged
    "joint": "",          # canonical joint name the poser knows it by
    "frame": -1,
    "origin": None,       # world position of the grabbed point
    "target": None,       # where the cursor has taken it
    "points": None,       # solved joint positions, world space
    "parents": None,      # skeleton parent indices, for drawing
    "solve": None,        # the raw solve, kept for the commit
    "whole_pose": False,  # moving the body, rather than one effector
    "error": "",
}

LABEL_SIZE = 12
LABEL_COLOR = (1.0, 0.9, 0.4, 1.0)
LABEL_OFFSET_PX = 14.0

_draw_handle = None
_label_handle = None
_NS_KEY = "_animatica_curve_drag_handle"
_NS_LABEL_KEY = "_animatica_curve_drag_label_handle"


# ---------------------------------------------------------------------------
# Rig ↔ poser
# ---------------------------------------------------------------------------

def _poser():
    from .autoposer import poser

    return poser


def _canonical(bone_name: str) -> str:
    """``animatica:LeftHand`` → ``LeftHand``."""
    return bone_name.rsplit(":", 1)[-1]


def _to_poser(arm, world: Vector) -> list[float]:
    """World position → the poser's frame (armature space, Y-up)."""
    local = arm.matrix_world.inverted() @ world
    p = _poser().M @ local
    return [p.x, p.y, p.z]


def _from_poser(arm, xyz) -> Vector:
    """The poser's frame → world position."""
    return arm.matrix_world @ (_poser().MT @ Vector(tuple(float(v) for v in xyz)))


# ---------------------------------------------------------------------------
# Picking a point on a curve
# ---------------------------------------------------------------------------

def control_under_cursor(context, x: float, y: float) -> bool:
    """Is an Autoposer control under the cursor?

    They win over anything this module offers, and the reason is geometric
    rather than a preference: the controls are re-seated onto their joints
    every frame, and the motion trail runs through those same joints — so the
    trail's marker for the current frame sits exactly under the control that
    drives it. Picking the curve there would mean a click on a handle
    sometimes grabbed the line behind it.
    """
    from . import key_poses
    from .autoposer import poser

    region, rv3d = context.region, context.region_data
    settings = key_poses._settings(context.scene)
    if region is None or rv3d is None or settings is None:
        return False
    arm = key_poses._target(settings)
    if arm is None or not poser.has_controls(arm):
        return False
    if context.mode != 'POSE':
        return False        # not selectable anyway

    px = key_poses._px()
    cursor = Vector((x, y))
    mw = arm.matrix_world
    for pb in arm.pose.bones:
        if not poser._is_ctrl(pb.bone) or pb.bone.hide:
            continue
        head = view3d_utils.location_3d_to_region_2d(region, rv3d, mw @ pb.head)
        if head is None:
            continue
        tail = view3d_utils.location_3d_to_region_2d(region, rv3d, mw @ pb.tail)
        span = (tail - head).length if tail is not None else CONTROL_RADIUS_MIN
        radius = max(CONTROL_RADIUS_MIN, min(CONTROL_RADIUS_MAX, span * 0.6)) * px
        if (head - cursor).length <= radius:
            return True
    return False


def pick_point(context, x: float, y: float):
    """``(bone, frame, world position)`` of the trail point under the cursor.

    Nearest in screen space wins, which is how a curve reads on screen — the
    point you are pointing at, not the one that happens to be closest to the
    camera.
    """
    from . import key_poses

    region, rv3d = context.region, context.region_data
    settings = key_poses._settings(context.scene)
    if region is None or rv3d is None or settings is None:
        return None
    if not key_poses.trail_on(settings):
        return None

    trail = key_poses._trail
    frames = trail["frames"]
    if not trail["bones"] or not frames:
        return None

    px = key_poses._px()
    keys = set(key_poses.plan(context.scene)["frames"])
    cursor = Vector((x, y))
    best_key = best_frame = None
    best_key_d = KEY_PICK_RADIUS * px
    best_frame_d = FRAME_PICK_RADIUS * px

    for bone in trail["bones"]:
        points = trail["points"].get(bone)
        if not points or len(points) != len(frames):
            continue
        for i, frame in enumerate(frames):
            co = view3d_utils.location_3d_to_region_2d(region, rv3d, Vector(points[i]))
            if co is None:
                continue
            d = (co - cursor).length
            if frame in keys:
                if d < best_key_d:
                    best_key, best_key_d = (bone, frame, Vector(points[i])), d
            elif d < best_frame_d:
                best_frame, best_frame_d = (bone, frame, Vector(points[i])), d
    # A key pose within reach always wins, however close an in-between is.
    return best_key or best_frame


# ---------------------------------------------------------------------------
# What is picked
# ---------------------------------------------------------------------------
#
# A click selects a point; a gizmo moves it. Clicking straight into a drag put
# the whole edit on the accuracy of one press: grab the frame next to the one
# you meant, or twitch the mouse on the way down, and the pose moved before you
# could see which point you had. Selecting first makes the wrong grab cost
# nothing — click again, and only then drag a handle that is unmistakably a
# handle.

_selection: dict = {"bone": "", "frame": -1}


def select_point(bone: str, frame: int) -> None:
    _selection.update({"bone": bone, "frame": int(frame)})


def clear_selection() -> None:
    _selection.update({"bone": "", "frame": -1})


def selected():
    """``(bone, frame)`` of the selected trail point, or None."""
    if not _selection["bone"] or _selection["frame"] < 0:
        return None
    return _selection["bone"], _selection["frame"]


def selected_world(context):
    """Where the selected point is right now, or None if it is gone.

    Read from the trail cache every time rather than remembered: a re-bake
    moves the point, and a gizmo left at the old position would move the wrong
    thing on the next drag.
    """
    from . import key_poses

    pick = selected()
    if pick is None:
        return None
    bone, frame = pick
    settings = key_poses._settings(context.scene)
    if not key_poses.trail_on(settings):
        return None
    trail = key_poses._trail
    frames = trail["frames"]
    points = trail["points"].get(bone)
    if not points or frame not in frames:
        return None
    i = frames.index(frame)
    if i >= len(points):
        return None
    return Vector(points[i])


# ---------------------------------------------------------------------------
# Solving a dragged frame
# ---------------------------------------------------------------------------

def _effectors_at(arm, frame_index: int, dragged_bone: str, target: Vector,
                  *, whole_pose: bool = False):
    """The frame's traced joints as position effectors.

    Two behaviours, because two different things are being asked for:

    * pulling a handle moves **that one** — the rest stay pinned where they
      were, and the body between them is the poser's problem. That holds for
      the hips too: dragging them over a foot is a weight shift, which is the
      commoner thing to want;
    * holding **Shift** moves the whole pose instead: every effector shifts by
      the same delta, so the character is carried bodily to a new place with
      its shape intact.
    """
    from . import key_poses

    trail = key_poses._trail
    delta = target - Vector(trail["points"][dragged_bone][frame_index]) if whole_pose else None
    out = []
    for bone in trail["bones"]:
        points = trail["points"].get(bone)
        if not points:
            continue
        if whole_pose:
            world = Vector(points[frame_index]) + delta
        else:
            world = target if bone == dragged_bone else Vector(points[frame_index])
        out.append({
            "joint": _canonical(bone),
            "type": "pos",
            "pos": _to_poser(arm, world),
            "tol": DRAG_TOL,
        })
    return out


def solve_drag(arm, frame_index: int, dragged_bone: str, target: Vector,
               *, whole_pose: bool = False):
    """Solve the body for this frame with one effector moved — or all of them,
    for a whole-pose move. Never touches the rig, the playhead, or the current
    pose."""
    from .autoposer import engine

    effectors = _effectors_at(arm, frame_index, dragged_bone, target,
                              whole_pose=whole_pose)
    if len(effectors) < 3:
        raise engine.NotReady(
            "the trail follows fewer than three joints — the poser needs 3+")
    eng = engine.get()
    return eng.pose(
        effectors,
        bone_lengths=_poser()._bone_lengths(arm),
        ik_refine=bool(bpy.context.scene.ap_use_ik),
        floor=bool(bpy.context.scene.ap_floor),
        floor_joints="feet",
        hard_floor=bool(bpy.context.scene.ap_floor),
    )


def _preview_from(arm, out):
    """World-space joint positions and the parent table, for the ghost."""
    from .autoposer import engine

    points = [_from_poser(arm, p) for p in out["joints"]]
    parents = list(engine.skeleton().parents)
    return points, parents


# ---------------------------------------------------------------------------
# Committing
# ---------------------------------------------------------------------------

def commit(arm, frame: int, out) -> int:
    """Write the solved pose into the rig's action at ``frame``.

    The solve is turned into the same ``matrix_basis`` the Autoposer would
    write live (``poser.pose_bases``), then decomposed into the channels an
    action stores. Nothing is posed on the way through, so the frame the
    artist is looking at does not so much as flicker.
    """
    from . import pose_edit

    action = pose_edit._editing_action(arm)
    if action is None:
        if arm.animation_data is None:
            arm.animation_data_create()
        action = bpy.data.actions.new(f"{arm.name}Action")
        arm.animation_data.action = action

    bases = _poser().pose_bases(arm, out["names"], out["joints"], out["rotations_6d"])
    channels = []
    for name, basis in bases.items():
        pb = arm.pose.bones.get(name)
        if pb is None:
            continue
        loc, quat, _scale = basis.decompose()
        path = pose_edit._rotation_path(pb)
        if path == "rotation_quaternion":
            values = [quat.w, quat.x, quat.y, quat.z]
        elif path == "rotation_euler":
            values = list(quat.to_euler(pb.rotation_mode))
        else:
            axis, angle = quat.to_axis_angle()
            values = [angle, axis.x, axis.y, axis.z]
        channels += [(f'pose.bones["{name}"].{path}', i, v) for i, v in enumerate(values)]
        if pb.parent is None:
            # The root carries the body's placement; keying rotation alone
            # would leave the character where the old key put it.
            channels += [
                (f'pose.bones["{name}"].location', i, v) for i, v in enumerate(loc)
            ]
    return pose_edit.write_channels(action, frame, channels)


# ---------------------------------------------------------------------------
# Draw
# ---------------------------------------------------------------------------

def _draw():
    if not _drag["active"] or _drag["points"] is None:
        return
    context = bpy.context
    space = getattr(context, "space_data", None)
    if space is None or space.type != 'VIEW_3D':
        return

    from . import key_poses

    points, parents = _drag["points"], _drag["parents"]
    segments = []
    for i, parent in enumerate(parents):
        if parent < 0 or i >= len(points) or parent >= len(points):
            continue
        segments.append(points[parent][:])
        segments.append(points[i][:])
    if not segments:
        return

    px = key_poses._px()
    shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')        # a preview is UI: never hidden by the body
    try:
        shader.bind()
        shader.uniform_float("viewportSize", key_poses._viewport_size())
        shader.uniform_float("lineWidth", PREVIEW_WIDTH * px)
        shader.uniform_float("color", PREVIEW_COLOR)
        batch_for_shader(shader, 'LINES', {"pos": segments}).draw(shader)
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')


def _draw_label():
    """What you grabbed, next to where you are dragging it.

    A drag that silently took the frame next to the one you meant is only
    obvious once it is committed. Naming the frame while the mouse is still
    down makes a mis-grab something you can see and escape.
    """
    if not _drag["active"] or _drag["target"] is None:
        return
    context = bpy.context
    space = getattr(context, "space_data", None)
    if space is None or space.type != 'VIEW_3D':
        return
    region, rv3d = context.region, context.region_data
    if region is None or rv3d is None:
        return
    co = view3d_utils.location_3d_to_region_2d(region, rv3d, _drag["target"])
    if co is None:
        return

    from . import key_poses

    px = key_poses._px()
    text = (f"whole pose · frame {_drag['frame']}" if _drag["whole_pose"]
            else f"{_drag['joint']} · frame {_drag['frame']}")
    if _drag["error"]:
        text = _drag["error"]
    font_id = 0
    blf.size(font_id, int(LABEL_SIZE * px))
    blf.position(font_id, co.x + LABEL_OFFSET_PX * px, co.y + LABEL_OFFSET_PX * px, 0)
    blf.color(font_id, *LABEL_COLOR)
    blf.draw(font_id, text)


def register_draw_handler() -> None:
    """Install both handlers, replacing whatever an earlier module load left.

    Reusing the handle Blender still holds keeps the OLD module's function
    drawing — from the old module's globals — so an edit here would appear to
    do nothing at all. The same trap as in key_poses; the same way out.
    """
    global _draw_handle, _label_handle
    ns = bpy.app.driver_namespace
    for key in (_NS_KEY, _NS_LABEL_KEY):
        stale = ns.pop(key, None)
        if stale is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(stale, 'WINDOW')
            except (ValueError, RuntimeError):
                pass
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
    ns[_NS_KEY] = _draw_handle
    if ns.get(_NS_LABEL_KEY) is None:
        _label_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_label, (), 'WINDOW', 'POST_PIXEL')
        ns[_NS_LABEL_KEY] = _label_handle


def unregister_draw_handler() -> None:
    global _draw_handle, _label_handle
    ns = bpy.app.driver_namespace
    for key, handle in ((_NS_KEY, _draw_handle), (_NS_LABEL_KEY, _label_handle)):
        h = handle or ns.get(key)
        if h is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
            except (ValueError, RuntimeError):
                pass
        ns.pop(key, None)
    _draw_handle = _label_handle = None


def _clear() -> None:
    _drag.update({
        "active": False, "bone": "", "joint": "", "frame": -1,
        "origin": None, "target": None, "points": None, "parents": None,
        "solve": None, "whole_pose": False, "error": "",
    })


# ---------------------------------------------------------------------------
# The drag
# ---------------------------------------------------------------------------

_AXES = {"X": Vector((1.0, 0.0, 0.0)),
         "Y": Vector((0.0, 1.0, 0.0)),
         "Z": Vector((0.0, 0.0, 1.0))}


def _closest_on_axis(point, axis, ray_origin, ray_dir):
    """Where an axis handle has been dragged to: the point on the axis line
    through ``point`` that lies nearest the mouse ray.

    Returns None when the two are near enough to parallel that the answer runs
    off to infinity — looking straight down the axis you are dragging.
    """
    w0 = point - ray_origin
    a = axis.dot(axis)
    b = axis.dot(ray_dir)
    c = ray_dir.dot(ray_dir)
    d = axis.dot(w0)
    e = ray_dir.dot(w0)
    denom = a * c - b * b
    if abs(denom) < 1e-8:
        return None
    return point + axis * ((b * e - c * d) / denom)


class ANIMATICA_OT_drag_motion_curve(bpy.types.Operator):
    bl_idname = "animatica.drag_motion_curve"
    bl_label = "Drag Motion Curve"
    bl_description = (
        "Drag a point on a motion trail: that end effector moves at that "
        "frame, the Autoposer solves the body around it, and releasing keys "
        "the pose. The playhead does not move. Hold Shift to carry the whole "
        "pose instead of the one joint"
    )
    bl_options = {'REGISTER', 'UNDO'}

    bone: bpy.props.StringProperty()
    frame: bpy.props.IntProperty()
    whole_pose: bpy.props.BoolProperty(
        name="Whole Pose",
        description="Carry the whole pose instead of moving one end effector",
        default=False,
    )
    axis: bpy.props.StringProperty(
        name="Constraint",
        description=(
            "What the move is confined to: one world axis (X, Y, Z), a world "
            "plane (XY, YZ, ZX), or empty for the plane facing the viewer"
        ),
        default="",
    )

    def invoke(self, context, event):
        from . import key_poses

        arm = key_poses._target(key_poses._settings(context.scene))
        if arm is None:
            return {'CANCELLED'}
        trail = key_poses._trail
        if self.frame not in trail["frames"]:
            return {'CANCELLED'}

        self._arm = arm
        self._index = trail["frames"].index(self.frame)
        points = trail["points"].get(self.bone)
        if not points:
            return {'CANCELLED'}

        origin = Vector(points[self._index])
        self._whole = self._mode(event)
        _drag.update({
            "whole_pose": self._whole,
            "active": True, "bone": self.bone, "joint": _canonical(self.bone),
            "frame": int(self.frame), "origin": origin, "target": origin.copy(),
            "points": None, "parents": None, "solve": None, "error": "",
        })
        self._plane_no = context.region_data.view_rotation @ Vector((0.0, 0.0, 1.0))
        self._solve(context, event)
        context.area.header_text_set(
            f"Move {_canonical(self.bone)} at frame {self.frame}"
            + (f" along {self.axis}" if self.axis else "")
            + "   |   Shift: whole pose   |   Esc: cancel")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _mode(self, event) -> bool:
        """Whether this drag carries the whole pose, read from the modifiers.

        One rule for every handle: a drag moves the joint you grabbed, and
        **Shift** moves the whole body instead. The root is not a special case
        — the hips are a joint you pose like any other, and shifting weight
        over a foot is the commoner thing to want than relocating the
        character.

        Read on every mouse move rather than latched at the press, the way
        Blender's own transforms read theirs — changing your mind mid-drag is
        what modifiers are for.
        """
        return bool(self.whole_pose) or bool(event.shift)

    def _mouse_world(self, context, event):
        region, rv3d = context.region, context.region_data
        co = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, co)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, co)
        if len(self.axis) == 1:
            hit = _closest_on_axis(_drag["origin"], _AXES[self.axis], origin, direction)
            return hit or _drag["origin"]
        if len(self.axis) == 2:
            a, b = (_AXES[c] for c in self.axis)
            normal = a.cross(b)
        else:
            # The plane facing the viewer, through the grabbed point — the
            # depth the artist cannot see is the one they should not be
            # changing by accident.
            normal = self._plane_no
        hit = intersect_line_plane(
            origin, origin + direction * 10000.0, _drag["origin"], normal,
        )
        return hit or _drag["origin"]

    def _solve(self, context, event):
        from . import key_poses
        from .autoposer import engine

        self._whole = self._mode(event)
        _drag["whole_pose"] = self._whole
        _drag["target"] = self._mouse_world(context, event)
        try:
            out = solve_drag(self._arm, self._index, self.bone, _drag["target"],
                             whole_pose=self._whole)
        except engine.NotReady as exc:
            _drag["error"] = str(exc)
            _drag["solve"] = None
            return False
        except Exception as exc:                # noqa: BLE001 — a drag must not crash
            _drag["error"] = f"solve failed: {exc}"
            _drag["solve"] = None
            return False
        _drag["error"] = ""
        _drag["solve"] = out
        _drag["points"], _drag["parents"] = _preview_from(self._arm, out)
        key_poses.tag_redraw()
        return True

    def modal(self, context, event):
        # Modifier presses and releases arrive as their own events; re-solving
        # on them is what makes holding Shift mid-drag do something.
        if event.type in {'MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            self._solve(context, event)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            _clear()
            context.area.header_text_set(None)
            context.area.tag_redraw()
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            from . import key_poses

            out = _drag["solve"]
            error = _drag["error"]
            frame = int(_drag["frame"])
            whole = bool(_drag["whole_pose"])
            _clear()
            context.area.header_text_set(None)
            context.area.tag_redraw()
            if out is None:
                self.report({'WARNING'}, error or "nothing to key")
                return {'CANCELLED'}
            written = commit(self._arm, frame, out)
            key_poses.flash_keyed(frame)
            key_poses.invalidate_plan()
            key_poses.request_rebuild()
            what = "whole pose moved" if whole else f"{_canonical(self.bone)} moved"
            self.report({'INFO'}, f"{what} at frame {frame} — keyed ({written} channels)")
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


# ---------------------------------------------------------------------------
# The gizmo
# ---------------------------------------------------------------------------

#: Blender's own axis colours, so the handles mean what they mean everywhere else.
#: Blender's translate gizmo, part for part: an arrow per axis, a plane handle
#: per pair, and a ring in the middle for the view plane. Built from the same
#: gizmo primitives Blender's own is, so it takes the theme's axis colours and
#: the artist's gizmo size rather than inventing a look of its own.
_AXIS_ORDER = ("X", "Y", "Z")
#: Each plane handle by the axis it is perpendicular to — which is also the
#: colour Blender gives it.
_PLANES = {"X": "YZ", "Y": "ZX", "Z": "XY"}

ARROW_LENGTH = 1.0
PLANE_OFFSET = 0.42          # along each of the plane's own two axes
PLANE_SCALE = 0.16
RING_SCALE = 0.22


def _theme_axis_colours():
    """The viewport's own axis colours, so X is the red the artist's X is."""
    try:
        ui = bpy.context.preferences.themes[0].user_interface
        return {"X": tuple(ui.axis_x)[:3],
                "Y": tuple(ui.axis_y)[:3],
                "Z": tuple(ui.axis_z)[:3]}
    except Exception:                       # noqa: BLE001 — a theme is not worth a traceback
        return {"X": (0.96, 0.26, 0.31), "Y": (0.55, 0.77, 0.15), "Z": (0.16, 0.45, 0.94)}


def _orient(axis: Vector, origin: Vector) -> Matrix:
    """Place a gizmo that points along its own +Z onto a world axis."""
    up = Vector((0.0, 0.0, 1.0))
    dot = axis.dot(up)
    if dot > 0.9999:
        rot = Matrix.Identity(3)
    elif dot < -0.9999:
        rot = Matrix.Rotation(3.141592653589793, 3, 'X')
    else:
        rot = up.rotation_difference(axis).to_matrix()
    m = rot.to_4x4()
    m.translation = origin
    return m


class ANIMATICA_GGT_curve_point(bpy.types.GizmoGroup):
    """The translate gizmo, standing on the selected trail point.

    Blender's own transform gizmo drives an object or a bone; a point on a
    motion curve is neither, so the same primitives are assembled here — arrow,
    plane, ring — in the same arrangement and the same theme colours. Every
    handle runs one operator; the handle says only what constrains the move.
    """

    bl_idname = "ANIMATICA_GGT_curve_point"
    bl_label = "Motion Curve Point"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    # No 'SCALE': Blender's transform gizmo keeps its screen size as you zoom,
    # and a handle that grew with the view would cover the curve it edits.
    bl_options = {'3D', 'PERSISTENT'}

    @classmethod
    def poll(cls, context):
        return selected_world(context) is not None

    def setup(self, context):
        colours = _theme_axis_colours()
        self._handles = []

        for axis in _AXIS_ORDER:
            colour = colours[axis]
            arrow = self.gizmos.new("GIZMO_GT_arrow_3d")
            arrow.draw_style = 'NORMAL'
            arrow.length = ARROW_LENGTH
            arrow.line_width = 2.0
            self._paint(arrow, colour, 1.0)
            self._handles.append((axis, arrow))

            plane = self.gizmos.new("GIZMO_GT_primitive_3d")
            plane.draw_style = 'PLANE'
            plane.scale_basis = PLANE_SCALE
            self._paint(plane, colour, 0.6)
            self._handles.append((_PLANES[axis], plane))

        ring = self.gizmos.new("GIZMO_GT_move_3d")
        ring.draw_style = 'RING_2D'
        ring.draw_options = {'ALIGN_VIEW'}
        ring.scale_basis = RING_SCALE
        self._paint(ring, (1.0, 1.0, 1.0), 0.6)
        self._handles.append(("", ring))

    @staticmethod
    def _paint(gz, colour, alpha: float) -> None:
        gz.color = colour
        gz.alpha = alpha
        gz.color_highlight = (1.0, 1.0, 1.0)
        gz.alpha_highlight = 1.0
        gz.use_draw_modal = True

    def refresh(self, context):
        pick = selected()
        origin = selected_world(context)
        if pick is None or origin is None:
            return
        bone, frame = pick
        for constraint, gz in self._handles:
            gz.matrix_basis = self._place(constraint, origin)
            props = gz.target_set_operator("animatica.drag_motion_curve")
            props.bone, props.frame, props.axis = bone, frame, constraint

    @staticmethod
    def _place(constraint: str, origin: Vector) -> Matrix:
        if len(constraint) == 1:                       # an axis arrow
            return _orient(_AXES[constraint], origin)
        if len(constraint) == 2:                       # a plane handle, offset into its corner
            a, b = (_AXES[c] for c in constraint)
            corner = origin + (a + b) * PLANE_OFFSET
            return _orient(a.cross(b), corner)
        return Matrix.Translation(origin)              # the view-plane ring


_classes = (ANIMATICA_OT_drag_motion_curve, ANIMATICA_GGT_curve_point)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    register_draw_handler()


def unregister() -> None:
    unregister_draw_handler()
    _clear()
    clear_selection()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
