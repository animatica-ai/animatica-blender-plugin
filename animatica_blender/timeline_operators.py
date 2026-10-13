"""Timeline interaction operators for Animatica prompt-block strips.

Uses keymap-based approach: clicking on a strip automatically selects/drags
it without needing to activate a modal operator first.

Provides:
- ANIMATICA_OT_timeline_strip_action: click/drag on strips (keymap-driven)
- ANIMATICA_OT_timeline_strip_add_click: double-click empty area to add strip
- ANIMATICA_OT_timeline_strip_context_menu: right-click context menu
- ANIMATICA_OT_timeline_strip_delete: Delete/Backspace to remove active strip
- ANIMATICA_OT_edit_strip_prompt: popup dialog for editing a strip's prompt
- draw_timeline_header(): appended to DOPESHEET_HT_header for + / - buttons
- register_keymaps() / unregister_keymaps()
"""

import random
import threading
import time

import bpy
from bpy.props import IntProperty, StringProperty

from .operators import ends_cleanly
from .operators import esc_cancels as operators_esc_cancels
from .timeline_overlay import (
    hit_test_strips,
    hit_test_lane_resize,
    hit_test_waypoint_pin,
    hit_test_key_pose,
    pixel_to_frame,
    find_neighbors,
    find_gap,
    inline_edit_state,
    DEFAULT_BLOCK_LENGTH,
    is_in_lane,
    interaction,
    track_hover,
    get_strip_height,
    set_strip_height,
    MIN_STRIP_HEIGHT,
    MAX_STRIP_HEIGHT,
)


# Module-level double-click tracking (persists across operator invocations)
_last_click_time: float = 0.0
_last_click_idx: int = -1
_DOUBLE_CLICK_THRESHOLD: float = 0.35  # seconds
#: the last click on the empty lane (the scrub takes it; the second of a double adds a block)
_last_empty_click: float = 0.0
#: ...and as the lane's own click handler sees it
_scrub_click: float = 0.0

# Keymap storage
_addon_keymaps = []


def _block_sharing_edge(prompt_blocks, idx, zone):
    """The index of the block whose edge meets *idx*'s *zone* edge, or None."""
    fr = prompt_blocks[idx]
    for j, other in enumerate(prompt_blocks):
        if j == idx:
            continue
        if zone == "edge_end" and other.frame_start == fr.frame_end:
            return j
        if zone == "edge_start" and other.frame_end == fr.frame_start:
            return j
    return None


def _timeline_poll(context):
    """Common poll for timeline operators."""
    return (
        context.area is not None
        and context.area.type == "DOPESHEET_EDITOR"
        and hasattr(context, "space_data")
        and context.space_data is not None
        and context.space_data.mode == "TIMELINE"
        and hasattr(context.scene, "animatica")
    )


# ---------------------------------------------------------------------------
# Main strip interaction operator (triggered by keymap on LEFTMOUSE)
# ---------------------------------------------------------------------------
#
# The mouse on the lane, as in the other DCC integrations:
#   left-drag empty ........ scrub the playhead (a click clears the selection)
#   near the playhead ...... scrub: the playhead wins over the pins on its line
#   Ctrl+drag empty ........ rubber-band: blocks and waypoint pins
#   click block ............ select it; Ctrl+click adds or drops it
#   drag block(s) .......... move the selection, snapping to edges, the
#                            playhead and pins; Shift: no snapping
#   Alt+drag block ......... jump past a neighbour, into the nearest free gap
#                            on the drop side
#   drag block edge ........ resize; Alt pushes the neighbour, Ctrl scales the
#                            pins inside with it
#   click / drag a pin ..... select it (and its viewport marker) / move it
#   double-click ........... edit the prompt; on empty, a new block

#: within this many px (x UI scale), a dragged edge snaps
SNAP_PX = 8


def _selected(blocks):
    return [i for i, b in enumerate(blocks) if b.selected]


def _select_only(props, idx):
    for j, b in enumerate(props.prompt_blocks):
        b.selected = j == idx
    props.active_block_index = idx


def _select_none(props):
    # the active block stays (the sidebar's prompt field shows it); none is picked
    for b in props.prompt_blocks:
        b.selected = False


def _snap_targets(context, exclude):
    """Frames a dragged edge snaps to: the other blocks' edges, the
    playhead, the key poses and the waypoints."""
    from . import key_poses, waypoints
    scene = context.scene
    out = {int(scene.frame_current)}
    for j, b in enumerate(scene.animatica.prompt_blocks):
        if j not in exclude:
            out.update((int(b.frame_start), int(b.frame_end)))
    out.update(f for f, _r in key_poses.timeline_ticks(scene)[0])
    out.update(waypoints.timeline_frames(scene))
    return sorted(out)


def _snap(context, frames, targets, reach_px):
    """The shift (in frames) that puts the nearest of ``frames`` on a target
    within ``reach_px``, or 0."""
    v2d = context.region.view2d
    best, shift = reach_px + 1, 0
    for f in frames:
        fx = v2d.view_to_region(f, 0, clip=False)[0]
        for t in targets:
            d = abs(v2d.view_to_region(t, 0, clip=False)[0] - fx)
            if d < best:
                best, shift = d, t - f
    return shift


class ANIMATICA_OT_timeline_strip_action(bpy.types.Operator):
    """Click and drag on the Animatica lane of the Timeline"""

    bl_idname = "animatica.timeline_strip_action"
    bl_label = "Animatica Strip Action"
    bl_options = {"REGISTER", "UNDO"}

    _state: str = "IDLE"

    @classmethod
    def poll(cls, context):
        if not _timeline_poll(context):
            return False
        from . import properties
        return properties._live_armature(context.scene.animatica.target_armature) is not None

    # -- the press ---------------------------------------------------------

    def invoke(self, context, event):
        global _last_click_time, _last_click_idx

        mx, my = event.mouse_region_x, event.mouse_region_y
        if not is_in_lane(context, my):
            return {"PASS_THROUGH"}
        props = context.scene.animatica
        self._u = context.preferences.system.ui_scale
        self._moved = False
        self._press_x = mx
        v2d = context.region.view2d

        # --- the lane's top edge: its height ---
        if props.prompt_blocks and hit_test_lane_resize(context, mx, my):
            self._state = "DRAGGING_LANE_RESIZE"
            self._resize_start_y = my
            self._resize_start_h = get_strip_height()
            context.window.cursor_set("MOVE_Y")
            return self._start(context)

        ph_x = v2d.view_to_region(context.scene.frame_current, 0, clip=False)[0]
        near_playhead = abs(mx - ph_x) <= 5 * self._u

        # --- pins: key poses above the blocks, waypoints below; the playhead
        # wins near its line, or a pin on it could never be scrubbed past ---
        if not near_playhead:
            key = hit_test_key_pose(context, mx, my)
            if key is not None:
                self._state = "DRAGGING_KEY_POSE"
                self._key_from = int(key)
                self._key_moved = False
                interaction["key_drag"] = (self._key_from, self._key_from)
                context.window.cursor_set("MOVE_X")
                return self._start(context)
            pin = hit_test_waypoint_pin(context, mx, my)
            if pin is not None:
                self._state = "DRAGGING_WAYPOINT"
                self._waypoint_name = pin.name
                self._original_frame = int(pin.animatica_waypoint_frame)
                self._ctrl = event.ctrl
                context.window.cursor_set("MOVE_X")
                return self._start(context)

        hit = hit_test_strips(context, mx, my)
        if hit["index"] is None:
            # --- empty lane: a rectangle select, as in Blender's own editors
            # (the playhead line itself still scrubs) ---
            global _scrub_click
            now = time.time()
            if now - _scrub_click < _DOUBLE_CLICK_THRESHOLD:
                # the second click of a double-click: a new block, typed into
                _scrub_click = 0.0
                return ANIMATICA_OT_timeline_strip_add_click._add_strip_at(self, context, event)
            _scrub_click = now
            if near_playhead:
                self._state = "SCRUB"
                self._scrub(context, mx)
                return self._start(context)
            self._band_add = event.shift or event.ctrl or event.oskey
            self._state = "BAND"
            self._band_from = (mx, my)
            interaction["band"] = (mx, my, mx, my)
            return self._start(context)

        idx, zone = hit["index"], hit["zone"]
        fr = props.prompt_blocks[idx]

        # --- double-click: the prompt, in place ---
        now = time.time()
        if idx == _last_click_idx and (now - _last_click_time) < _DOUBLE_CLICK_THRESHOLD and not event.ctrl:
            _select_only(props, idx)
            _last_click_time, _last_click_idx = 0.0, -1
            bpy.ops.animatica.timeline_strip_inline_edit("INVOKE_DEFAULT", index=idx)
            return {"FINISHED"}
        _last_click_time, _last_click_idx = now, idx

        # --- Shift / Cmd / Ctrl + click: in or out of the selection (on release,
        # if the mouse did not move: Shift+drag is a move without snapping) ---
        self._toggle = zone == "body" and (event.shift or event.ctrl or event.oskey)
        if self._toggle:
            if not fr.selected:
                fr.selected = True               # dragged, it moves with the rest
                self._toggle = "added"
        elif not fr.selected:
            _select_only(props, idx)
        props.active_block_index = idx
        if fr.locked:
            # set in stone: its motion is where it is, so its frames stay too
            context.area.tag_redraw()
            return {"FINISHED"}

        self._idx = idx
        self._orig = [(b.frame_start, b.frame_end) for b in props.prompt_blocks]
        self._press_frame = pixel_to_frame(context, mx)
        self._active_idx = idx
        self._original_start, self._original_end = fr.frame_start, fr.frame_end

        shared = _block_sharing_edge(props.prompt_blocks, idx, zone)
        if zone in ("edge_start", "edge_end") and shared is not None and not event.alt:
            # two blocks meet here and the hit test names one: the drag's direction picks
            self._state = "DRAGGING_SHARED_EDGE"
            self._shared_edge = (idx, shared) if zone == "edge_end" else (shared, idx)
            self._shared_frame = fr.frame_end if zone == "edge_end" else fr.frame_start
        elif zone == "edge_start":
            self._state = "DRAGGING_EDGE_START"
        elif zone == "edge_end":
            self._state = "DRAGGING_EDGE_END"
        elif event.alt:
            self._state = "JUMP"
        else:
            self._state = "MOVE"
            self._group = _selected(props.prompt_blocks) or [idx]
        interaction["drag"] = idx
        return self._start(context)

    def _start(self, context):
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    # -- the drag ----------------------------------------------------------

    def modal(self, context, event):
        result = self._modal(context, event)
        if "RUNNING_MODAL" not in result:
            interaction["drag"] = None
            interaction["key_drag"] = None
            interaction["band"] = None
            context.window.cursor_set("DEFAULT")
            for a in context.screen.areas:
                if a.type in {"DOPESHEET_EDITOR", "VIEW_3D"}:
                    a.tag_redraw()
        return result

    def _modal(self, context, event):
        if context.area is None:
            return {"CANCELLED"}
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._cancel_drag(context)
            return {"CANCELLED"}
        if event.type == "MOUSEMOVE" and abs(event.mouse_region_x - self._press_x) > 2:
            self._moved = True
        handler = {
            "SCRUB": self._handle_scrub,
            "BAND": self._handle_band,
            "DRAGGING_KEY_POSE": self._handle_key_pose_drag,
            "DRAGGING_WAYPOINT": self._handle_waypoint_drag,
            "DRAGGING_SHARED_EDGE": self._handle_shared_edge,
            "DRAGGING_LANE_RESIZE": self._handle_lane_resize,
            "MOVE": self._handle_move,
            "JUMP": self._handle_jump,
            "DRAGGING_EDGE_START": self._handle_edge_drag,
            "DRAGGING_EDGE_END": self._handle_edge_drag,
        }.get(self._state)
        if handler is None:
            return {"PASS_THROUGH"}
        return handler(context, event)

    def _released(self, event):
        return event.type == "LEFTMOUSE" and event.value == "RELEASE"

    # scrub

    def _scrub(self, context, mx):
        scene = context.scene
        f = max(scene.frame_start, min(scene.frame_end, pixel_to_frame(context, mx)))
        if f != scene.frame_current:
            scene.frame_set(f)

    def _handle_scrub(self, context, event):
        if event.type == "MOUSEMOVE":
            self._scrub(context, event.mouse_region_x)
            return {"RUNNING_MODAL"}
        if self._released(event):
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    # rubber band

    def _handle_band(self, context, event):
        x0, y0 = self._band_from
        if event.type == "MOUSEMOVE":
            interaction["band"] = (x0, y0, event.mouse_region_x, event.mouse_region_y)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if self._released(event):
            from .timeline_overlay import Lane
            from . import waypoints
            bx0, bx1 = sorted((x0, event.mouse_region_x))
            by0, by1 = sorted((y0, event.mouse_region_y))
            props = context.scene.animatica
            v2d = context.region.view2d
            g = Lane(context)
            if not self._band_add:
                # a fresh selection (and a click on empty space just clears it)
                _select_none(props)
                for obj in waypoints.waypoints(context.scene):
                    obj.select_set(False)
            if not self._moved:
                return {"FINISHED"}
            first = None
            if by0 <= g.b1 and by1 >= g.b0:              # the band reaches the block row
                for j, b in enumerate(props.prompt_blocks):
                    a = v2d.view_to_region(b.frame_start, 0, clip=False)[0]
                    e = v2d.view_to_region(b.frame_end, 0, clip=False)[0]
                    if a <= bx1 and e >= bx0:
                        b.selected = True
                        first = j if first is None else first
            if first is not None:
                props.active_block_index = first
            if by0 <= g.wp1 and by1 >= g.wp0:            # ...and the waypoint row
                for obj in waypoints.waypoints(context.scene):
                    x = v2d.view_to_region(obj.animatica_waypoint_frame, 0, clip=False)[0]
                    if bx0 <= x <= bx1:
                        obj.select_set(True)
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    # move the selection

    def _handle_move(self, context, event):
        props = context.scene.animatica
        blocks = props.prompt_blocks
        if event.type == "MOUSEMOVE":
            delta = pixel_to_frame(context, event.mouse_region_x) - self._press_frame
            group = self._group
            starts = [self._orig[i][0] for i in group]
            ends = [self._orig[i][1] for i in group]
            if not event.shift:
                delta += _snap(context, [min(starts) + delta, max(ends) + delta],
                               _snap_targets(context, set(group)), SNAP_PX * self._u)
            # never onto a block that is not moving
            lo, hi = -10 ** 9, 10 ** 9
            others = [j for j in range(len(blocks)) if j not in group]
            for i in group:
                s0, e0 = self._orig[i]
                left = max([self._orig[j][1] for j in others if self._orig[j][1] <= s0] + [1])
                right = min([self._orig[j][0] for j in others if self._orig[j][0] >= e0] + [10 ** 9])
                lo, hi = max(lo, left - s0), min(hi, right - e0)
            delta = max(lo, min(hi, delta))
            for i in group:
                blocks[i].frame_start = self._orig[i][0] + delta
                blocks[i].frame_end = self._orig[i][1] + delta
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if self._released(event):
            if not self._moved:
                fr = props.prompt_blocks[self._idx]
                if self._toggle == "added":
                    props.active_block_index = self._idx
                elif self._toggle:
                    fr.selected = False              # it was in: a modifier-click takes it out
                    rest = _selected(props.prompt_blocks)
                    if rest and props.active_block_index == self._idx:
                        props.active_block_index = rest[0]
                elif len(self._group) > 1:
                    _select_only(props, self._idx)   # a plain click on one of several: that one
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    # Alt: jump past a neighbour

    def _handle_jump(self, context, event):
        props = context.scene.animatica
        b = props.prompt_blocks[self._idx]
        s0, e0 = self._orig[self._idx]
        if event.type == "MOUSEMOVE":
            delta = pixel_to_frame(context, event.mouse_region_x) - self._press_frame
            b.frame_start, b.frame_end = max(1, s0 + delta), max(1, s0 + delta) + (e0 - s0)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if self._released(event):
            place = self._free_place(props, self._idx, b.frame_start, e0 - s0, b.frame_start >= s0)
            if place is None:
                b.frame_start, b.frame_end = s0, e0
                self.report({"INFO"}, "No free gap there for this block")
            else:
                b.frame_start, b.frame_end = place, place + (e0 - s0)
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def _free_place(self, props, idx, want, length, rightward):
        """Where a block of ``length`` dropped at ``want`` goes: the free gap
        under it, else the nearest one on the drop side, at the end nearest
        to where it was dropped."""
        taken = sorted((self._orig[j] for j in range(len(props.prompt_blocks)) if j != idx))
        gaps, cursor = [], 1
        for s, e in taken:
            if s - cursor >= length:
                gaps.append((cursor, s))
            cursor = max(cursor, e)
        gaps.append((cursor, 10 ** 9))
        mid = want + length / 2
        for a, z in gaps:
            if a <= mid <= z:
                return max(a, min(want, z - length))
        side = [g for g in gaps if (g[0] >= want if rightward else g[1] <= want + length)]
        if not side:
            return None
        a, z = min(side, key=lambda g: abs((g[0] if rightward else g[1] - length) - want))
        return a if rightward else z - length

    # resize an edge (Alt pushes the neighbour, Ctrl scales the pins inside)

    def _handle_edge_drag(self, context, event):
        props = context.scene.animatica
        blocks = props.prompt_blocks
        i = self._idx
        s0, e0 = self._orig[i]
        if event.type == "MOUSEMOVE":
            f = pixel_to_frame(context, event.mouse_region_x)
            if not event.shift:
                f += _snap(context, [f], _snap_targets(context, {i}), SNAP_PX * self._u)
            # everyone back where they started, then the drag
            for j, (a, z) in enumerate(self._orig):
                blocks[j].frame_start, blocks[j].frame_end = a, z
            if self._state == "DRAGGING_EDGE_END":
                f = max(s0 + 1, f)
                after = sorted((j for j in range(len(blocks)) if j != i and self._orig[j][0] >= e0),
                               key=lambda j: self._orig[j][0])
                if event.alt:
                    edge = f
                    for j in after:              # push the neighbours along, as far as needed
                        a, z = self._orig[j]
                        if a >= edge:
                            break
                        blocks[j].frame_start, blocks[j].frame_end = edge, edge + (z - a)
                        edge = edge + (z - a)
                elif after:
                    f = min(f, self._orig[after[0]][0])
                blocks[i].frame_end = f
            else:
                f = max(1, min(e0 - 1, f))
                before = sorted((j for j in range(len(blocks)) if j != i and self._orig[j][1] <= s0),
                                key=lambda j: -self._orig[j][1])
                if event.alt:
                    edge = f
                    for j in before:
                        a, z = self._orig[j]
                        if z <= edge:
                            break
                        if edge - (z - a) < 1:
                            f = blocks[i].frame_start         # no room to push into
                            break
                        blocks[j].frame_start, blocks[j].frame_end = edge - (z - a), edge
                        edge = edge - (z - a)
                elif before:
                    f = max(f, self._orig[before[0]][1])
                blocks[i].frame_start = f
            self._scale_pins = event.ctrl
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if self._released(event):
            if getattr(self, "_scale_pins", False) or event.ctrl:
                self._scale_pins_in(context, (s0, e0), (blocks[i].frame_start, blocks[i].frame_end))
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def _scale_pins_in(self, context, old, new):
        """The key poses and waypoints inside the block, retimed with it."""
        from . import key_poses, waypoints
        (a0, z0), (a1, z1) = old, new
        if (a0, z0) == (a1, z1) or z0 <= a0:
            return

        def to(f):
            return int(round(a1 + (f - a0) * (z1 - a1) / (z0 - a0)))

        scene = context.scene
        for obj in waypoints.waypoints(scene):
            f = int(obj.animatica_waypoint_frame)
            if a0 <= f <= z0 and waypoints.at_frame(scene, to(f)) in (None, obj):
                obj.animatica_waypoint_frame = to(f)
        frames = [f for f, _r in key_poses.timeline_ticks(scene)[0] if a0 <= f <= z0]
        grow = (z1 - a1) > (z0 - a0)
        # moved in the order that never lands one on another still to move
        for f in sorted(frames, reverse=grow if a1 >= a0 else not grow):
            if to(f) != f:
                key_poses.move_key_pose(scene, f, to(f))

    # the edge two blocks share: the drag's direction picks which one moves

    def _handle_shared_edge(self, context, event):
        props = context.scene.animatica
        if event.type == "MOUSEMOVE":
            frame = pixel_to_frame(context, event.mouse_region_x)
            if frame == self._shared_frame:
                return {"RUNNING_MODAL"}
            left_idx, right_idx = self._shared_edge
            if frame > self._shared_frame:
                self._idx, self._state = right_idx, "DRAGGING_EDGE_START"
            else:
                self._idx, self._state = left_idx, "DRAGGING_EDGE_END"
            self._active_idx = self._idx
            props.active_block_index = self._idx
            return self._handle_edge_drag(context, event)
        if self._released(event):
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    # key poses

    def _handle_key_pose_drag(self, context, event):
        from . import constraints_ui, key_poses

        if event.type == "MOUSEMOVE":
            frame = max(0, pixel_to_frame(context, event.mouse_region_x))
            action = key_poses._action(key_poses._target(context.scene.animatica))
            taken = set(constraints_ui.authored_pose_frames(action)[0]) - {self._key_from}
            if frame not in taken:
                interaction["key_drag"] = (self._key_from, frame)
                self._key_moved = self._key_moved or frame != self._key_from
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if self._released(event):
            src, dst = interaction.get("key_drag") or (self._key_from, self._key_from)
            if not self._key_moved:
                context.scene.frame_set(src)            # a click: go to it
                return {"CANCELLED"}                    # (nothing to undo)
            if dst != src and key_poses.move_key_pose(context.scene, src, dst):
                context.scene.frame_set(dst)
                self.report({"INFO"}, f"Key pose moved to frame {dst}")
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    # waypoints

    def _dragged_waypoint(self, context):
        return context.scene.objects.get(getattr(self, "_waypoint_name", ""))

    def _handle_waypoint_drag(self, context, event):
        from . import waypoints

        obj = self._dragged_waypoint(context)
        if obj is None:
            return {"CANCELLED"}
        if event.type == "MOUSEMOVE":
            frame = max(1, pixel_to_frame(context, event.mouse_region_x))
            taken = waypoints.at_frame(context.scene, frame)
            if frame != obj.animatica_waypoint_frame and taken is None:
                obj.animatica_waypoint_frame = frame
                self._waypoint_name = obj.name  # renamed to "Path F<frame>"
                waypoints.tag_redraw()
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if self._released(event):
            if not self._moved:
                # a click: the pin, and its marker in the viewport; Ctrl toggles it
                if self._ctrl:
                    obj.select_set(not obj.select_get())
                else:
                    for other in waypoints.waypoints(context.scene):
                        other.select_set(other == obj)
                waypoints.tag_redraw()
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    # the lane's height

    def _handle_lane_resize(self, context, event):
        if event.type == "MOUSEMOVE":
            set_strip_height(self._resize_start_h + event.mouse_region_y - self._resize_start_y)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if self._released(event):
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    # cancel: everything back

    def _cancel_drag(self, context):
        if self._state == "DRAGGING_WAYPOINT":
            obj = self._dragged_waypoint(context)
            if obj is not None:
                obj.animatica_waypoint_frame = self._original_frame
        elif self._state == "DRAGGING_LANE_RESIZE":
            set_strip_height(getattr(self, "_resize_start_h", get_strip_height()))
        elif hasattr(self, "_orig"):
            for b, (a, z) in zip(context.scene.animatica.prompt_blocks, self._orig):
                b.frame_start, b.frame_end = a, z
        interaction["key_drag"] = None
        interaction["band"] = None
        context.area.tag_redraw()

    def cancel(self, context):
        self._cancel_drag(context)


# ---------------------------------------------------------------------------
# Hover: what the mouse is on, for the lane to light it up
# ---------------------------------------------------------------------------

class ANIMATICA_OT_timeline_hover(bpy.types.Operator):
    """Highlight what the mouse is over in the Animatica lane"""

    bl_idname = "animatica.timeline_hover"
    bl_label = "Animatica Lane Hover"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context) and context.region is not None and context.region.type == "WINDOW"

    def invoke(self, context, event):
        if track_hover(context, event.mouse_region_x, event.mouse_region_y):
            context.area.tag_redraw()
        return {"PASS_THROUGH"}


# ---------------------------------------------------------------------------
# Double-click on empty area to add strip at that position
# ---------------------------------------------------------------------------

class ANIMATICA_OT_timeline_strip_add_click(bpy.types.Operator):
    """Double-click an empty part of the Animatica lane to add a prompt block there."""

    bl_idname = "animatica.timeline_strip_add_click"
    bl_label = "Add Strip at Click"
    bl_options = {"REGISTER", "UNDO"}

    # Module-level tracking for double-click on empty
    _last_empty_click_time: float = 0.0

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context)

    def invoke(self, context, event):
        # Only react if click is in the lane area
        if not is_in_lane(context, event.mouse_region_y):
            return {"PASS_THROUGH"}
        # the lane's click handler does this itself (it takes the first click to scrub)
        if ANIMATICA_OT_timeline_strip_action.poll(context):
            return {"PASS_THROUGH"}

        # Check if there's already a strip here — if so, pass through
        hit = hit_test_strips(context, event.mouse_region_x, event.mouse_region_y)
        if hit["index"] is not None:
            return {"PASS_THROUGH"}

        # Double-click detection on empty area
        global _last_empty_click
        now = time.time()
        if (now - _last_empty_click) < _DOUBLE_CLICK_THRESHOLD:
            _last_empty_click = 0.0
            # Create strip at click position
            return self._add_strip_at(context, event)

        _last_empty_click = now
        return {"PASS_THROUGH"}

    def _add_strip_at(self, context, event):
        """Add a new strip at the click position, fitting into the available gap."""
        props = context.scene.animatica
        click_frame = pixel_to_frame(context, event.mouse_region_x)
        scene_start = max(1, context.scene.frame_start)
        scene_end = context.scene.frame_end

        # Find the gap that contains the click position
        from .timeline_overlay import get_sorted_blocks
        sorted_items = get_sorted_blocks(props.prompt_blocks)

        # Determine gap boundaries around the click
        gap_start = scene_start
        gap_end = scene_end
        for _idx, s_start, s_end in sorted_items:
            if s_end <= click_frame:
                gap_start = max(gap_start, s_end)
            if s_start > click_frame and s_start < gap_end:
                gap_end = s_start
            # Click is inside an existing strip — no room
            if s_start <= click_frame < s_end:
                self.report({"WARNING"}, "No room for a new strip here")
                return {"CANCELLED"}

        gap_length = gap_end - gap_start
        if gap_length < 2:
            self.report({"WARNING"}, "No room for a new strip here")
            return {"CANCELLED"}

        # Against the block on its left (where the motion carries on from),
        # else at the click; the default length, as far as the gap allows.
        has_left = any(e <= click_frame for _i, _s, e in sorted_items)
        new_start = gap_start if has_left else max(gap_start, click_frame)
        new_end = min(gap_end, new_start + DEFAULT_BLOCK_LENGTH)
        if new_end - new_start < 2:
            new_start = max(gap_start, new_end - DEFAULT_BLOCK_LENGTH)

        new_range = props.prompt_blocks.add()
        new_range.prompt = ""
        new_range.frame_start = new_start
        new_range.frame_end = new_end
        new_range.enabled = True

        new_idx = len(props.prompt_blocks) - 1
        props.active_block_index = new_idx

        context.area.tag_redraw()

        # Start inline editing on the new strip (same as double-click)
        bpy.ops.animatica.timeline_strip_inline_edit(
            "INVOKE_DEFAULT", index=new_idx,
        )

        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Add strip between nearest keyframes of source armature
# ---------------------------------------------------------------------------

def _get_armature_keyframes(armature):
    """Collect all unique keyframe frame numbers from an armature's action.

    Uses the Blender 5.0 slotted-actions API (channelbag) to access fcurves.
    """
    from bpy_extras import anim_utils

    frames: set[int] = set()
    if not armature or not armature.animation_data or not armature.animation_data.action:
        return sorted(frames)

    action = armature.animation_data.action
    slot = armature.animation_data.action_slot
    if slot is None:
        return sorted(frames)

    channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
    if channelbag is None:
        return sorted(frames)

    for fc in channelbag.fcurves:
        for kp in fc.keyframe_points:
            frames.add(int(kp.co[0]))
    return sorted(frames)


class ANIMATICA_OT_add_strip_between_keyframes(bpy.types.Operator):
    """Add a prompt block that fills the space between the source armature's
    two keyframes on either side of the click."""

    bl_idname = "animatica.add_strip_between_keyframes"
    bl_label = "Add Strip Between Keyframes"
    bl_options = {"REGISTER", "UNDO"}

    frame: IntProperty(
        name="Frame",
        description="Frame around which to find bracketing keyframes",
        default=1,
    )

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context)

    def execute(self, context):
        props = context.scene.animatica
        armature = props.target_armature

        if not armature:
            self.report({"WARNING"}, "No source armature selected")
            return {"CANCELLED"}

        kf_list = _get_armature_keyframes(armature)
        if len(kf_list) < 2:
            self.report({"WARNING"}, "The source armature needs at least 2 keyframes")
            return {"CANCELLED"}

        click = self.frame

        # Find the two keyframes that bracket the click position
        kf_before = None
        kf_after = None
        for kf in kf_list:
            if kf <= click:
                kf_before = kf
            if kf >= click and kf_after is None:
                kf_after = kf

        # Edge cases: click before first keyframe or after last
        if kf_before is None:
            kf_before = kf_list[0]
        if kf_after is None:
            kf_after = kf_list[-1]

        # If click lands exactly on a keyframe, expand to neighbors
        if kf_before == kf_after:
            idx = kf_list.index(kf_before)
            if idx > 0:
                kf_before = kf_list[idx - 1]
            elif idx < len(kf_list) - 1:
                kf_after = kf_list[idx + 1]

        new_start = kf_before
        new_end = kf_after

        if new_start >= new_end:
            self.report({"WARNING"}, "Could not determine keyframe range")
            return {"CANCELLED"}

        # Clamp to scene bounds
        new_start = max(1, new_start)
        new_end = min(context.scene.frame_end, new_end)

        # Find the gap around the click position and intersect with
        # the keyframe range so the strip fits without overlapping.
        from .timeline_overlay import get_sorted_blocks
        gap_start = max(1, context.scene.frame_start)
        gap_end = context.scene.frame_end
        for _idx, s_start, s_end in get_sorted_blocks(props.prompt_blocks):
            if s_end <= click and s_end > gap_start:
                gap_start = s_end
            if s_start > click and s_start < gap_end:
                gap_end = s_start

        # Intersect keyframe range with gap
        new_start = max(new_start, gap_start)
        new_end = min(new_end, gap_end)

        if new_start >= new_end:
            self.report({"WARNING"}, "No room for a strip between these keyframes")
            return {"CANCELLED"}

        new_range = props.prompt_blocks.add()
        new_range.prompt = ""
        new_range.frame_start = new_start
        new_range.frame_end = new_end
        new_range.enabled = True

        props.active_block_index = len(props.prompt_blocks) - 1

        self.report({"INFO"}, f"Added strip {new_start}–{new_end} (between keyframes)")

        # Redraw all timeline areas
        for area in context.screen.areas:
            if area.type == "DOPESHEET_EDITOR":
                area.tag_redraw()

        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Delete strip (Backspace / Delete key)
# ---------------------------------------------------------------------------

class ANIMATICA_OT_timeline_strip_delete(bpy.types.Operator):
    """Delete an Animatica prompt block (Backspace/Delete).

    Deletes the block under the mouse, or the active block when the mouse is
    in the Animatica lane. Outside the lane, the keys delete keyframes as
    usual.
    """

    bl_idname = "animatica.timeline_strip_delete"
    bl_label = "Delete Animatica Strip"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            _timeline_poll(context)
            and len(context.scene.animatica.prompt_blocks) > 0
        )

    def invoke(self, context, event):
        # Only intercept if the mouse is hovering over the strip lane
        if not is_in_lane(context, event.mouse_region_y):
            return {"PASS_THROUGH"}

        props = context.scene.animatica
        # the selection, as a whole; else the block under the mouse, else the active one
        doomed = _selected(props.prompt_blocks)
        if not doomed:
            hit = hit_test_strips(context, event.mouse_region_x, event.mouse_region_y)
            idx = hit["index"] if hit["index"] is not None else props.active_block_index
            if not (0 <= idx < len(props.prompt_blocks)):
                return {"PASS_THROUGH"}
            doomed = [idx]
        names = [props.prompt_blocks[i].prompt or "(no prompt)" for i in doomed]
        for i in sorted(doomed, reverse=True):
            props.prompt_blocks.remove(i)
        self.report({"INFO"}, f"Deleted {len(doomed)} block{'s' if len(doomed) != 1 else ''}: "
                              + ", ".join(names)[:80])

        # Adjust active index
        if len(props.prompt_blocks) == 0:
            props.active_block_index = 0
        elif props.active_block_index >= len(props.prompt_blocks):
            props.active_block_index = len(props.prompt_blocks) - 1

        from .properties import save_blocks_to_armature

        save_blocks_to_armature(props.target_armature, props)

        context.area.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Right-click context menu
# ---------------------------------------------------------------------------

class ANIMATICA_OT_timeline_strip_context_menu(bpy.types.Operator):
    """Show the right-click menu for Animatica prompt blocks."""

    bl_idname = "animatica.timeline_strip_context_menu"
    bl_label = "Animatica Strip Menu"

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context)

    def invoke(self, context, event):
        # Only show menu if click is in the lane area
        if not is_in_lane(context, event.mouse_region_y):
            return {"PASS_THROUGH"}

        # Check if right-click hit a strip
        hit = hit_test_strips(context, event.mouse_region_x, event.mouse_region_y)

        if hit["index"] is not None:
            # Select the strip
            context.scene.animatica.active_block_index = hit["index"]

        # Store click position for "Add Strip Here"
        self._click_frame = pixel_to_frame(context, event.mouse_region_x)
        self._hit_index = hit["index"]

        wm = context.window_manager
        wm.popup_menu(self._draw_menu, title="Animatica Strip")
        return {"FINISHED"}

    def _draw_menu(self, menu, context):
        layout = menu.layout
        props = context.scene.animatica

        if self._hit_index is not None and 0 <= self._hit_index < len(props.prompt_blocks):
            fr = props.prompt_blocks[self._hit_index]
            layout.label(text=f"Strip: {fr.prompt or '(no prompt)'}")
            op = layout.operator(
                "animatica.timeline_strip_toggle_lock",
                text="Unlock (generate it again)" if fr.locked else "Lock (keep this motion)",
                icon="UNLOCKED" if fr.locked else "LOCKED",
            )
            op.index = self._hit_index
            layout.separator()
            # Seed inspection + pin/unpin. A pinned block (seed > 0) keeps its
            # seed through a full Generate; an unpinned block inherits the
            # global Seed but still records the concrete seed it last ran with.
            if int(getattr(fr, "seed", 0)) > 0:
                layout.label(text=f"Seed pinned: {fr.seed}")
                op = layout.operator(
                    "animatica.clear_block_seed",
                    text="Clear seed (follow global)",
                    icon="X",
                )
                op.index = self._hit_index
            elif int(getattr(fr, "last_used_seed", 0)) > 0:
                layout.label(text=f"Generated with seed: {fr.last_used_seed}")
                op = layout.operator(
                    "animatica.reuse_block_seed",
                    text="Reuse seed (pin it)",
                    icon="LOCKED",
                )
                op.index = self._hit_index
            layout.separator()

            # Edit prompt
            op = layout.operator(
                "animatica.edit_strip_prompt",
                text="Edit Prompt",
                icon="TEXT",
            )
            op.index = self._hit_index

            # Toggle enabled
            toggle_text = "Disable" if fr.enabled else "Enable"
            toggle_icon = "HIDE_ON" if fr.enabled else "HIDE_OFF"
            op = layout.operator(
                "animatica.timeline_strip_toggle_enabled",
                text=toggle_text,
                icon=toggle_icon,
            )
            op.index = self._hit_index

            # Regenerate just this range — available during preview so the
            # user can re-roll one block (typically after bumping the seed)
            # without losing the neighbour bakes.
            regen_row = layout.row()
            regen_row.enabled = bool(getattr(props, "is_previewing", False)) and not bool(getattr(props, "is_generating", False))
            op = regen_row.operator(
                "animatica.regenerate_block",
                text="Regenerate Range",
                icon="FILE_REFRESH",
            )
            op.block_index = self._hit_index

            op = layout.operator("animatica.block_to_playhead", text="Move Block to Playhead",
                                 icon='SNAP_ON')
            op.index = self._hit_index

            layout.separator()

            # Delete
            layout.operator(
                "animatica.timeline_strip_delete",
                text="Delete Strip",
                icon="TRASH",
            )

            layout.separator()

        # Add between keyframes (needs source armature with keyframes)
        has_kf = (
            props.target_armature
            and len(_get_armature_keyframes(props.target_armature)) >= 2
        )
        kf_row = layout.row()
        kf_row.enabled = has_kf
        op = kf_row.operator(
            "animatica.add_strip_between_keyframes",
            text="Add Strip Between Keyframes",
            icon="KEYFRAME_HLT",
        )
        op.frame = self._click_frame

        # Add strip in first available gap
        layout.operator(
            "animatica.add_prompt_block",
            text="Add Strip in Gap",
            icon="ADD",
        )
        layout.separator()
        layout.operator("action.view_all", text="View All", icon='ZOOM_ALL')


class ANIMATICA_OT_block_to_playhead(bpy.types.Operator):
    """Move the block so it starts at the playhead, or as close as the blocks next to it allow"""
    bl_idname = "animatica.block_to_playhead"
    bl_label = "Move Block to Playhead"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)

    def execute(self, context):
        props = context.scene.animatica
        i = self.index if self.index >= 0 else props.active_block_index
        if not 0 <= i < len(props.prompt_blocks):
            return {"CANCELLED"}
        b = props.prompt_blocks[i]
        length = b.frame_end - b.frame_start
        left_end, right_start = find_neighbors(props.prompt_blocks, i)
        start = max(int(context.scene.frame_current), left_end, 1)
        if right_start is not None:
            start = min(start, right_start - length)
        if start < left_end:
            self.report({"WARNING"}, "No room for the block there")
            return {"CANCELLED"}
        b.frame_start, b.frame_end = start, start + length
        for a in context.screen.areas:
            if a.type in {"DOPESHEET_EDITOR", "VIEW_3D"}:
                a.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Toggle strip enabled (for context menu)
# ---------------------------------------------------------------------------

class ANIMATICA_OT_timeline_strip_toggle_enabled(bpy.types.Operator):
    """Turn a prompt block on or off."""

    bl_idname = "animatica.timeline_strip_toggle_enabled"
    bl_label = "Toggle Strip Enabled"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Strip Index", default=0)

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context)

    def execute(self, context):
        props = context.scene.animatica
        if 0 <= self.index < len(props.prompt_blocks):
            fr = props.prompt_blocks[self.index]
            fr.enabled = not fr.enabled
            state = "enabled" if fr.enabled else "disabled"
            self.report({"INFO"}, f"Strip '{fr.prompt}' {state}")

            # Force redraw all timeline areas — context.area may point
            # to the popup menu rather than the actual timeline editor.
            for area in context.screen.areas:
                if area.type == "DOPESHEET_EDITOR":
                    area.tag_redraw()
        return {"FINISHED"}


class ANIMATICA_OT_timeline_strip_toggle_lock(bpy.types.Operator):
    """Lock a block to keep its motion. Generate and Redo leave it as it is,
    and the blocks next to it are made to blend into it. Unlock it to generate
    it again"""

    bl_idname = "animatica.timeline_strip_toggle_lock"
    bl_label = "Lock Block"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Strip Index", default=-1)

    def execute(self, context):
        props = context.scene.animatica
        i = self.index if self.index >= 0 else props.active_block_index
        if not 0 <= i < len(props.prompt_blocks):
            return {"CANCELLED"}
        fr = props.prompt_blocks[i]
        fr.locked = not fr.locked
        self.report({"INFO"}, ("Locked. Generate leaves this block as it is" if fr.locked
                               else "Unlocked. The next Generate makes this block again"))
        for area in context.screen.areas:
            if area.type in {"DOPESHEET_EDITOR", "VIEW_3D"}:
                area.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Reuse recorded seed (lock last_used_seed into seed)
# ---------------------------------------------------------------------------

class ANIMATICA_OT_reuse_block_seed(bpy.types.Operator):
    """Pin the seed this block was last generated with.

    The next generation then reproduces this block's motion instead of using
    a new random seed.
    """

    bl_idname = "animatica.reuse_block_seed"
    bl_label = "Reuse Seed"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Strip Index", default=0)

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context)

    def execute(self, context):
        props = context.scene.animatica
        if not (0 <= self.index < len(props.prompt_blocks)):
            return {"CANCELLED"}
        block = props.prompt_blocks[self.index]
        if int(getattr(block, "last_used_seed", 0)) <= 0:
            self.report({"WARNING"}, "This block has no recorded seed yet. Generate it first")
            return {"CANCELLED"}
        block.seed = int(block.last_used_seed)
        from .properties import save_blocks_to_armature
        save_blocks_to_armature(props.target_armature, props)
        self.report({"INFO"}, f"Locked seed {block.seed} onto this block")
        for area in context.screen.areas:
            if area.type == "DOPESHEET_EDITOR":
                area.tag_redraw()
        return {"FINISHED"}


class ANIMATICA_OT_clear_block_seed(bpy.types.Operator):
    """Clear a block's pinned seed so it follows the global Seed again."""

    bl_idname = "animatica.clear_block_seed"
    bl_label = "Clear Seed"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Strip Index", default=0)

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context)

    def execute(self, context):
        props = context.scene.animatica
        if not (0 <= self.index < len(props.prompt_blocks)):
            return {"CANCELLED"}
        props.prompt_blocks[self.index].seed = 0
        from .properties import save_blocks_to_armature
        save_blocks_to_armature(props.target_armature, props)
        self.report({"INFO"}, "Block now follows the global Seed")
        for area in context.screen.areas:
            if area.type == "DOPESHEET_EDITOR":
                area.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Inline prompt editing (triggered by double-click on strip)
# ---------------------------------------------------------------------------

class InlinePromptEditing:
    """Typing a block's prompt in place: the text, a cursor, a selection, the
    clipboard. Shared by the Timeline block and the floating bar's field, as a
    mixin: a subclass of a registered operator took over its poll, and the
    Timeline's own editor stopped opening."""

    def invoke(self, context, event):
        props = context.scene.animatica
        if not (0 <= self.index < len(props.prompt_blocks)):
            return {"CANCELLED"}

        fr = props.prompt_blocks[self.index]

        # Populate shared state that the draw callback reads
        inline_edit_state["active"] = True
        inline_edit_state["index"] = self.index
        inline_edit_state["text"] = fr.prompt
        inline_edit_state["cursor"] = len(fr.prompt)
        inline_edit_state["original"] = fr.prompt
        inline_edit_state["selection_start"] = None

        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    @staticmethod
    def _has_selection():
        s = inline_edit_state.get("selection_start")
        return s is not None and s != inline_edit_state["cursor"]

    @staticmethod
    def _get_selection_range():
        """Return (lo, hi) of current selection."""
        s = inline_edit_state["selection_start"]
        c = inline_edit_state["cursor"]
        return (min(s, c), max(s, c))

    @staticmethod
    def _delete_selection():
        """Delete selected text, update cursor, clear selection. Returns new (text, pos)."""
        lo, hi = InlinePromptEditing._get_selection_range()
        text = inline_edit_state["text"]
        inline_edit_state["text"] = text[:lo] + text[hi:]
        inline_edit_state["cursor"] = lo
        inline_edit_state["selection_start"] = None
        return inline_edit_state["text"], lo

    def modal(self, context, event):
        if context.area is None:
            self._cancel(context)
            return {"CANCELLED"}

        # --- Confirm ---
        if event.type in {"RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            self._commit(context)
            return {"FINISHED"}

        # --- Cancel ---
        if event.type == "ESC" and event.value == "PRESS":
            self._cancel(context)
            return {"CANCELLED"}

        # --- Click outside strip → confirm ---
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            hit = hit_test_strips(
                context, event.mouse_region_x, event.mouse_region_y,
            )
            if hit["index"] != self.index:
                self._commit(context)
                return {"FINISHED"}
            return {"RUNNING_MODAL"}

        # Only act on key-press events from here on
        if event.value != "PRESS":
            return {"RUNNING_MODAL"}

        text = inline_edit_state["text"]
        pos = inline_edit_state["cursor"]
        handled = True

        if event.type == "BACK_SPACE":
            if self._has_selection():
                self._delete_selection()
            elif event.ctrl or event.oskey:
                # Delete word before cursor
                i = pos
                while i > 0 and text[i - 1] == " ":
                    i -= 1
                while i > 0 and text[i - 1] != " ":
                    i -= 1
                inline_edit_state["text"] = text[:i] + text[pos:]
                inline_edit_state["cursor"] = i
            elif pos > 0:
                inline_edit_state["text"] = text[:pos - 1] + text[pos:]
                inline_edit_state["cursor"] = pos - 1
            inline_edit_state["selection_start"] = None

        elif event.type == "DEL":
            if self._has_selection():
                self._delete_selection()
            elif pos < len(text):
                inline_edit_state["text"] = text[:pos] + text[pos + 1:]
            inline_edit_state["selection_start"] = None

        elif event.type == "LEFT_ARROW":
            if event.shift:
                # Extend selection
                if inline_edit_state["selection_start"] is None:
                    inline_edit_state["selection_start"] = pos
            else:
                # Clear selection; jump to selection start if active
                if self._has_selection():
                    lo, _ = self._get_selection_range()
                    inline_edit_state["cursor"] = lo
                    inline_edit_state["selection_start"] = None
                    context.area.tag_redraw()
                    return {"RUNNING_MODAL"}
                inline_edit_state["selection_start"] = None

            if event.ctrl or event.oskey:
                i = pos
                while i > 0 and text[i - 1] == " ":
                    i -= 1
                while i > 0 and text[i - 1] != " ":
                    i -= 1
                inline_edit_state["cursor"] = i
            else:
                inline_edit_state["cursor"] = max(0, pos - 1)

        elif event.type == "RIGHT_ARROW":
            if event.shift:
                if inline_edit_state["selection_start"] is None:
                    inline_edit_state["selection_start"] = pos
            else:
                if self._has_selection():
                    _, hi = self._get_selection_range()
                    inline_edit_state["cursor"] = hi
                    inline_edit_state["selection_start"] = None
                    context.area.tag_redraw()
                    return {"RUNNING_MODAL"}
                inline_edit_state["selection_start"] = None

            if event.ctrl or event.oskey:
                i = pos
                while i < len(text) and text[i] == " ":
                    i += 1
                while i < len(text) and text[i] != " ":
                    i += 1
                inline_edit_state["cursor"] = i
            else:
                inline_edit_state["cursor"] = min(len(text), pos + 1)

        elif event.type == "HOME":
            if event.shift:
                if inline_edit_state["selection_start"] is None:
                    inline_edit_state["selection_start"] = pos
            else:
                inline_edit_state["selection_start"] = None
            inline_edit_state["cursor"] = 0

        elif event.type == "END":
            if event.shift:
                if inline_edit_state["selection_start"] is None:
                    inline_edit_state["selection_start"] = pos
            else:
                inline_edit_state["selection_start"] = None
            inline_edit_state["cursor"] = len(text)

        elif event.type == "A" and (event.ctrl or event.oskey):
            # Select all
            inline_edit_state["selection_start"] = 0
            inline_edit_state["cursor"] = len(text)

        elif event.type == "V" and (event.ctrl or event.oskey):
            # Paste from clipboard (replace selection if active)
            if self._has_selection():
                text, pos = self._delete_selection()
            clipboard = context.window_manager.clipboard or ""
            clipboard = clipboard.replace("\n", " ").replace("\r", "")
            inline_edit_state["text"] = text[:pos] + clipboard + text[pos:]
            inline_edit_state["cursor"] = pos + len(clipboard)
            inline_edit_state["selection_start"] = None

        elif event.type == "C" and (event.ctrl or event.oskey):
            # Copy selected text (or full text if no selection)
            if self._has_selection():
                lo, hi = self._get_selection_range()
                context.window_manager.clipboard = text[lo:hi]
            else:
                context.window_manager.clipboard = text

        elif event.type == "X" and (event.ctrl or event.oskey):
            # Cut selected text
            if self._has_selection():
                lo, hi = self._get_selection_range()
                context.window_manager.clipboard = text[lo:hi]
                self._delete_selection()

        elif event.type == "TAB":
            # Tab confirms like Enter
            self._commit(context)
            return {"FINISHED"}

        elif event.unicode and event.unicode.isprintable():
            # Replace selection if active, then insert character
            if self._has_selection():
                text, pos = self._delete_selection()
            inline_edit_state["text"] = text[:pos] + event.unicode + text[pos:]
            inline_edit_state["cursor"] = pos + len(event.unicode)
            inline_edit_state["selection_start"] = None

        else:
            handled = False

        if handled:
            context.area.tag_redraw()

        return {"RUNNING_MODAL"}

    # -- helpers --

    def _commit(self, context):
        """Save edited text to the frame range and exit edit mode."""
        props = context.scene.animatica
        idx = inline_edit_state["index"]
        if 0 <= idx < len(props.prompt_blocks):
            props.prompt_blocks[idx].prompt = inline_edit_state["text"]
        inline_edit_state["active"] = False
        # Every editor, not just the Timeline: the sidebar's Generate button
        # waits on a prompt, and it sat greyed until the mouse crossed it.
        for area in context.screen.areas:
            area.tag_redraw()

    def _cancel(self, context):
        """Revert text and exit edit mode."""
        inline_edit_state["active"] = False
        context.area.tag_redraw()

    def cancel(self, context):
        self._cancel(context)


class ANIMATICA_OT_timeline_strip_inline_edit(InlinePromptEditing, bpy.types.Operator):
    """Edit a block's prompt directly on the timeline."""

    bl_idname = "animatica.timeline_strip_inline_edit"
    bl_label = "Inline Edit Strip Prompt"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    index: IntProperty(name="Strip Index", default=0)

    @classmethod
    def poll(cls, context):
        return _timeline_poll(context)


# ---------------------------------------------------------------------------
# Prompt editing popup (right-click menu → Edit Prompt)
# ---------------------------------------------------------------------------

class ANIMATICA_OT_edit_strip_prompt(bpy.types.Operator):
    """Edit the prompt of a block."""

    bl_idname = "animatica.edit_strip_prompt"
    bl_label = "Edit Strip Prompt"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Strip Index", default=0)
    prompt: StringProperty(name="Prompt", default="")

    def invoke(self, context, event):
        props = context.scene.animatica
        if 0 <= self.index < len(props.prompt_blocks):
            self.prompt = props.prompt_blocks[self.index].prompt
        # Mark that we went through invoke (dialog will be shown)
        self._from_dialog = True
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        props = context.scene.animatica

        if 0 <= self.index < len(props.prompt_blocks):
            fr = props.prompt_blocks[self.index]
            layout.label(
                text=f"Range {self.index + 1}:  frames {fr.frame_start} – {fr.frame_end}"
            )

        layout.prop(self, "prompt", text="Prompt")

    def execute(self, context):
        # When called from a popup menu, Blender calls execute() directly
        # instead of invoke(), so the dialog never opens. Detect this and
        # defer to invoke via a timer (menu must close first).
        if not getattr(self, "_from_dialog", False):
            idx = self.index

            def _open_dialog():
                try:
                    bpy.ops.animatica.edit_strip_prompt(
                        "INVOKE_DEFAULT", index=idx
                    )
                except Exception:
                    pass
                return None  # don't repeat

            bpy.app.timers.register(_open_dialog, first_interval=0.1)
            return {"FINISHED"}

        # Normal path — dialog was shown, save the prompt
        self._from_dialog = False
        props = context.scene.animatica
        if 0 <= self.index < len(props.prompt_blocks):
            props.prompt_blocks[self.index].prompt = self.prompt
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Add / remove / regenerate (hooked by header buttons and context menu)
# ---------------------------------------------------------------------------

class ANIMATICA_OT_add_prompt_block(bpy.types.Operator):
    """Add a prompt block in the first free gap on the timeline."""

    bl_idname = "animatica.add_prompt_block"
    bl_label = "Add Prompt Block"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.animatica
        scene_end = context.scene.frame_end or 250
        gap = find_gap(props.prompt_blocks, min_length=10, scene_end=scene_end)
        if gap is None:
            self.report({'WARNING'}, "No room on the timeline for a new block")
            return {'CANCELLED'}

        block = props.prompt_blocks.add()
        block.prompt = ""
        block.frame_start = gap[0]
        block.frame_end = gap[1]
        block.enabled = True
        props.active_block_index = len(props.prompt_blocks) - 1

        # Persist to armature
        from .properties import save_blocks_to_armature
        save_blocks_to_armature(props.target_armature, props)
        return {'FINISHED'}


class ANIMATICA_OT_remove_prompt_block(bpy.types.Operator):
    """Remove the active prompt block."""

    bl_idname = "animatica.remove_prompt_block"
    bl_label = "Remove Prompt Block"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            hasattr(context.scene, "animatica")
            and len(context.scene.animatica.prompt_blocks) > 0
        )

    def execute(self, context):
        props = context.scene.animatica
        if 0 <= props.active_block_index < len(props.prompt_blocks):
            props.prompt_blocks.remove(props.active_block_index)
            props.active_block_index = max(0, min(
                props.active_block_index, len(props.prompt_blocks) - 1,
            ))
            from .properties import save_blocks_to_armature
            save_blocks_to_armature(props.target_armature, props)
        return {'FINISHED'}


class ANIMATICA_OT_regenerate_block(bpy.types.Operator):
    """Generate one prompt block again and keep the rest of the preview.

    Works only on a preview that is still waiting. The frames just before and
    after the block are sent too, so the new motion joins its neighbours
    smoothly. Uses the current Seed, so a common way to work is to change the
    seed, right-click the block and regenerate.
    """

    bl_idname = "animatica.regenerate_block"
    bl_label = "Regenerate Block"

    block_index: IntProperty(
        name="Block Index",
        description="The prompt block to regenerate. -1 means the active block",
        default=-1,
    )
    seed: IntProperty(
        name="Seed",
        description=(
            "Seed for this regeneration. 0 lets the server pick a random seed. "
            "Any other value gives the same result each time. The value is "
            "saved on the block and filled in the next time you regenerate it"
        ),
        default=0, min=0, max=999999,
    )

    # Modal state — kept as plain class attributes; see ANIMATICA_OT_generate_pose
    # for why annotations are off-limits here under from __future__ import annotations.
    _timer = None
    _thread = None
    _result = None
    _error = None
    _target_range = None
    _request_start_frame = None
    _anchor_frames = None
    _start_time = 0.0

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "animatica", None)
        if s is None:
            return False
        if not getattr(s, "is_previewing", False):
            return False
        if getattr(s, "is_generating", False):
            return False
        return len(s.prompt_blocks) > 0

    def _resolve_block_index(self, context) -> int:
        s = context.scene.animatica
        idx = self.block_index if self.block_index >= 0 else s.active_block_index
        if 0 <= idx < len(s.prompt_blocks):
            return idx
        return -1

    def invoke(self, context, event):
        # Pre-fill the seed dialog. Priority:
        #   1. the block's stored seed (if non-zero) — the typical iteration
        #      case, "I just regenerated with 42, let me bump to 43";
        #   2. the global Seed setting — first regen on a fresh block, so
        #      we follow whatever seed the user has loaded for the next gen.
        s = context.scene.animatica
        idx = self._resolve_block_index(context)
        if idx < 0:
            self.report({'ERROR'}, "No prompt block selected")
            return {'CANCELLED'}
        block = s.prompt_blocks[idx]
        if block.locked:
            self.report({'WARNING'}, "This block is locked: unlock it to make it again")
            return {'CANCELLED'}
        self.seed = int(block.seed) if int(block.seed) > 0 else int(s.seed)
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        idx = self._resolve_block_index(context)
        if idx >= 0:
            block = context.scene.animatica.prompt_blocks[idx]
            label = (block.prompt or "(empty prompt)").strip() or "(empty prompt)"
            if len(label) > 48:
                label = label[:47] + "…"
            layout.label(text=f"Regenerate: {label}", icon='FILE_REFRESH')
            layout.label(text=f"Frames {block.frame_start}–{block.frame_end}")
            if int(getattr(block, "last_used_seed", 0)) > 0:
                layout.label(text=f"Last generated with seed: {block.last_used_seed}")
        layout.separator()
        layout.prop(self, "seed")
        layout.label(text="0 picks a new random seed and pins it to this block", icon='INFO')

    def execute(self, context):
        from . import constraints_ui, gltf_to_blender, mmcp_client, request_builder
        from .operators import _live_target_armature_or_clear

        s = context.scene.animatica

        idx = self._resolve_block_index(context)
        if idx < 0:
            self.report({'ERROR'}, "No prompt block selected")
            return {'CANCELLED'}
        if s.prompt_blocks[idx].locked:
            self.report({'WARNING'}, "This block is locked: unlock it to make it again")
            return {'CANCELLED'}

        if s.is_generating:
            self.report({'WARNING'}, "Already generating. Wait, or click Cancel")
            return {'CANCELLED'}

        if not s.is_previewing:
            self.report({'ERROR'}, "Regenerate Block works only on a live preview")
            return {'CANCELLED'}

        arm = _live_target_armature_or_clear(s)
        if arm is None:
            self.report({'ERROR'}, "Set a target armature first")
            return {'CANCELLED'}

        model_caps = mmcp_client.cached_model(s.model_id)
        if model_caps is None:
            self.report({'ERROR'}, "Connect to the server first")
            return {'CANCELLED'}

        preview_action = (
            arm.animation_data.action
            if arm.animation_data and arm.animation_data.action
            else None
        )
        if preview_action is None or not preview_action.name.startswith(
            request_builder._GENERATED_ACTION_PREFIXES
        ):
            self.report({'ERROR'}, "The active action is not an Animatica preview. Generate first")
            return {'CANCELLED'}

        from . import preview_session
        source_action = preview_session.source_of(arm) or (
            bpy.data.actions.get(s.source_action_name)
            if s.source_action_name
            else None
        )

        # Persist the user's seed choice onto the block so subsequent
        # regens pre-fill with it (matches the way pose-generate stashes
        # its last prompt onto the scene props).
        block = s.prompt_blocks[idx]
        # Resolve 0 ("auto") to a concrete seed and PIN it onto the block
        # (block.seed = used_seed). Pinning is what makes a regenerated block
        # survive a later full Generate: the full path keeps blocks whose seed
        # is > 0 and only re-rolls / inherits the global for blocks left at 0.
        # Set the block's Seed back to 0 to let it follow the global Seed again.
        used_seed = int(self.seed) if int(self.seed) > 0 else random.randint(1, 999999)
        block.seed = used_seed
        block.last_used_seed = used_seed
        from . import properties
        properties.save_blocks_to_armature(arm, s)

        try:
            req, (fs, fe) = request_builder.build_request_for_block(
                block_index=idx,
                model_id=s.model_id,
                model_caps=model_caps,
                armature_obj=arm,
                prompt_blocks=s.prompt_blocks,
                settings=s,
                scene=context.scene,
                constraint_objects=constraints_ui.walk_scene_constraints(context.scene),
                preview_action=preview_action,
                source_action=source_action,
                seed_override=used_seed,
            )
        except request_builder.BuildError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        # Frames sent as constraints become KEYFRAME-typed anchors after the
        # splice; everything else from the bake gets typed GENERATED. Same
        # contract as ANIMATICA_OT_generate uses for the full-bake path.
        anchor_frames: set[int] = set()
        for c in req.get("constraints", []):
            t = c.get("type")
            if t == "pose_keyframe":
                anchor_frames.add(int(c["frame"]) + fs)
            elif t == "effector_target":
                for f in c.get("frames", []) or ():
                    anchor_frames.add(int(f) + fs)

        self._target_range = (fs, fe)
        from . import timeline_overlay
        timeline_overlay.activity["generating"] = (fs, fe)
        self._request_start_frame = fs
        self._anchor_frames = anchor_frames
        self._result = None
        self._error = None

        s.is_generating = True
        s.cancel_requested = False
        s.generation_elapsed = 0
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._worker,
            args=(mmcp_client.get_mmcp_url(), req),
            daemon=True,
        )
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _worker(self, server_url, req):
        from . import mmcp_client
        try:
            mmcp_client.clear_failure()
            client = mmcp_client.MmcpClient(server_url)
            self._result = client.generate(req)
        except Exception as exc:                          # noqa: BLE001 — surfaced to UI
            self._error = exc

    def cancel(self, context):
        self._cleanup(context)

    @ends_cleanly
    def modal(self, context, event):
        from . import gltf_to_blender
        from .operators import (
            _announce_quota,
            _clear_quota_state,
            _live_target_armature_or_clear,
            _tick_generation_elapsed,
        )

        s = context.scene.animatica

        if operators_esc_cancels(self, event) or s.cancel_requested:
            self._cleanup(context)
            self.report({'INFO'}, "Regenerate cancelled. The request still finishes on the server")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if self._thread is not None and self._thread.is_alive():
            _tick_generation_elapsed(context, self._start_time)
            return {'RUNNING_MODAL'}

        if self._error is not None:
            self._cleanup(context)
            if _announce_quota(context, self._error):
                self.report({'INFO'}, "Generation limit reached")
            else:
                from . import mmcp_client
                self.report({'ERROR'}, f"The take failed. {mmcp_client.note_failure(self._error)}")
            return {'CANCELLED'}

        if self._result is None:
            self._cleanup(context)
            self.report({'ERROR'}, "The regeneration stopped without a result")
            return {'CANCELLED'}

        _clear_quota_state(s)

        arm = _live_target_armature_or_clear(s)
        if arm is None:
            self._cleanup(context)
            self.report({'ERROR'}, "The target armature is gone. Regenerate stopped")
            return {'CANCELLED'}

        preview_action = (
            arm.animation_data.action
            if arm.animation_data and arm.animation_data.action
            else None
        )
        if preview_action is None:
            self._cleanup(context)
            self.report({'ERROR'}, "The preview action was removed during the regeneration")
            return {'CANCELLED'}

        from . import preview_session
        try:
            # The block's new keys are the take's, not edits of the artist's.
            with preview_session.keeping_edits(preview_action):
                gltf_to_blender.splice_gltf_into_action(
                    self._result,
                    arm,
                    preview_action,
                    sample_index=0,
                    request_start_frame=self._request_start_frame,
                    target_range=self._target_range,
                    anchor_frames=self._anchor_frames,
                )
        except Exception as exc:                          # noqa: BLE001 — surfaced to UI
            self._cleanup(context)
            self.report({'ERROR'}, f"Splice failed: {exc}")
            return {'CANCELLED'}

        self._cleanup(context)
        for area in context.screen.areas:
            area.tag_redraw()
        self.report({'INFO'}, "Block regenerated")
        return {'FINISHED'}

    def _cleanup(self, context):
        s = context.scene.animatica
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        s.is_generating = False
        s.cancel_requested = False


# ---------------------------------------------------------------------------
# Timeline header append (+ / - buttons only)
# ---------------------------------------------------------------------------

def draw_timeline_header(self, context):
    """Appended to DOPESHEET_HT_header — adds Animatica strip controls."""
    if not hasattr(context, "space_data") or context.space_data is None:
        return
    if context.space_data.mode != "TIMELINE":
        return

    layout = self.layout
    layout.separator()
    layout.operator("animatica.add_prompt_block", text="", icon="ADD")
    layout.operator("animatica.remove_prompt_block", text="", icon="REMOVE")


# ---------------------------------------------------------------------------
# Keymap registration
# ---------------------------------------------------------------------------

def register_keymaps():
    """Register keyboard/mouse handlers in the Timeline (DopeSheet) editor."""
    wm = bpy.context.window_manager
    if wm.keyconfigs.addon is None:
        return

    km = wm.keyconfigs.addon.keymaps.new(
        name="Dopesheet", space_type="DOPESHEET_EDITOR"
    )

    # Left-click — strip select/drag (also handles lane resize). Any modifier:
    # a keymap item matches its modifiers exactly, and Ctrl, Alt and Shift
    # mean things on the lane (outside it the click is passed on untouched).
    kmi = km.keymap_items.new(
        "animatica.timeline_strip_action",
        type="LEFTMOUSE",
        value="PRESS",
        any=True,
    )
    _addon_keymaps.append((km, kmi))

    # Double-click on empty — add strip (uses same LEFTMOUSE but the
    # operator internally tracks double-click timing)
    kmi = km.keymap_items.new(
        "animatica.timeline_strip_add_click",
        type="LEFTMOUSE",
        value="PRESS",
    )
    _addon_keymaps.append((km, kmi))

    # Mouse move — the lane lights up what the mouse is on
    kmi = km.keymap_items.new(
        "animatica.timeline_hover",
        type="MOUSEMOVE",
        value="ANY",
    )
    _addon_keymaps.append((km, kmi))

    # Right-click — context menu
    kmi = km.keymap_items.new(
        "animatica.timeline_strip_context_menu",
        type="RIGHTMOUSE",
        value="PRESS",
    )
    _addon_keymaps.append((km, kmi))

    # Delete key — remove active strip
    kmi = km.keymap_items.new(
        "animatica.timeline_strip_delete",
        type="DEL",
        value="PRESS",
    )
    _addon_keymaps.append((km, kmi))

    # Backspace — remove active strip
    kmi = km.keymap_items.new(
        "animatica.timeline_strip_delete",
        type="BACK_SPACE",
        value="PRESS",
    )
    _addon_keymaps.append((km, kmi))


def unregister_keymaps():
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()


# ---------------------------------------------------------------------------
# Class registration
# ---------------------------------------------------------------------------

_classes = (
    ANIMATICA_OT_timeline_strip_action,
    ANIMATICA_OT_timeline_hover,
    ANIMATICA_OT_timeline_strip_toggle_lock,
    ANIMATICA_OT_block_to_playhead,
    ANIMATICA_OT_timeline_strip_add_click,
    ANIMATICA_OT_add_strip_between_keyframes,
    ANIMATICA_OT_timeline_strip_delete,
    ANIMATICA_OT_timeline_strip_context_menu,
    ANIMATICA_OT_timeline_strip_toggle_enabled,
    ANIMATICA_OT_reuse_block_seed,
    ANIMATICA_OT_clear_block_seed,
    ANIMATICA_OT_timeline_strip_inline_edit,
    ANIMATICA_OT_edit_strip_prompt,
    ANIMATICA_OT_add_prompt_block,
    ANIMATICA_OT_remove_prompt_block,
    ANIMATICA_OT_regenerate_block,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.DOPESHEET_HT_header.append(draw_timeline_header)
    register_keymaps()


def unregister():
    unregister_keymaps()
    try:
        bpy.types.DOPESHEET_HT_header.remove(draw_timeline_header)
    except Exception:
        pass
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
