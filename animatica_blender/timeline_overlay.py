"""The Animatica lane on the Timeline: prompt blocks, key poses, waypoints.

Drawn under Blender's own Timeline, in the floating bar's look (ui_style):

* a key-pose rail on top -- one diamond per pose the next Generate pins,
  hollow when it falls outside the generation, a count where they crowd;
* the blocks -- neutral tiles with the block's own colour as a swatch (the
  colour its ghosts and trail carry in the viewport); the one under the
  playhead lifted in mulberry, since that is the one the bar's Prompt and
  Redo act on; the selected one outlined; a padlock on a locked one;
* a waypoint rail below -- a flag per waypoint, dragged to retime it.

Frame numbers show only when they are wanted: on the block under the mouse,
the selected one and the one being dragged, with its length while it is. A
take under review is an orange strip under the blocks it covers; a take being
made sweeps orange across them, and the rest dim.

The interaction is in timeline_operators.py; this module draws and says
what is where.
"""

import math

import blf
import bpy
import gpu

from . import ui_style as st


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: Block row height, region pixels, once set by dragging the lane's top edge;
#: None until then, which is DEFAULT_BLOCK_HEIGHT at the UI scale.
_strip_height = None
DEFAULT_BLOCK_HEIGHT = 32    # logical px (× UI scale): two lines, the prompt and its length
MIN_STRIP_HEIGHT = 22        # region px: below this the label no longer fits
MAX_STRIP_HEIGHT = 120
RESIZE_HANDLE_HEIGHT = 4     # logical px either side of the lane's top edge

STRIP_Y_OFFSET = 6           # region px from the bottom of the timeline (or of the marker row)
MARKER_ROW_HEIGHT = 42       # Blender's UI_MARKER_MARGIN_Y, before UI scale
DEFAULT_BLOCK_LENGTH = 50    # frames -- a new block's length when the gap allows it
EDGE_HANDLE_WIDTH = 6        # region px -- the grab zone either side of a block's edge

# logical px (× UI scale)
LANE_PADDING = 3
KEY_RAIL = 14                # key-pose diamonds
WAYPOINT_RAIL = 18           # waypoint icons: the floating bar's size (toolbar ICON)
RAIL_GAP = 2
BLOCK_GAP = 2                # between two blocks that meet
BLOCK_RADIUS = 3
SWATCH = 3
TEXT_PAD = 8
TEXT_SIZE = 11
TAG_SIZE = 9
MIN_LABEL_WIDTH = 40         # narrower than this a block shows no text
LOCK_SIZE = 12
DIAMOND = 5.0                # half-diagonal: a key pose is the thing to see on the lane
CLUSTER = 8                  # marks closer than this draw as one, with a count
TAKE_STRIP = 3

# Identity colours, cycled per block: the swatch on the tile, and the colour
# of the block's ghosts and trail in the viewport (key_poses reads them).
STRIP_COLORS = [
    (0.267, 0.541, 0.878, 1.0),   # blue
    (0.878, 0.435, 0.267, 1.0),   # orange
    (0.365, 0.737, 0.400, 1.0),   # green
    (0.729, 0.333, 0.729, 1.0),   # purple
    (0.878, 0.722, 0.267, 1.0),   # yellow
    (0.267, 0.796, 0.796, 1.0),   # teal
]

UNDER_EDGE = st._hex("8E7CC3")               # the top edge of the block under the playhead
SELECTED_OUTLINE = (1.0, 1.0, 1.0, 0.85)
LANE_EDGE = (1.0, 1.0, 1.0, 0.07)
OUT_OF_RANGE = (0.0, 0.0, 0.0, 0.25)
HATCH = (1.0, 1.0, 1.0, 0.07)
DASH = (1.0, 1.0, 1.0, 0.30)
SWEEP = st.with_alpha(st.SOFT_ORANGE, 0.25)
SELECTION = st.with_alpha(st._hex("8E7CC3"), 0.45)
DIM_GENERATING = 0.6
DIM_DISABLED = 0.4
#: the viewport route's colour, so a flag here and its marker there match
WAYPOINT_COLOR = (1.0, 0.78, 0.25, 0.95)
# (kept for callers of the old names)
WAYPOINT_PIN_RADIUS = WAYPOINT_RAIL / 2

EMPTY_HINT = "Double-click to add a prompt block, or use Prompt… on the bar"
UNCONDITIONED_LABEL = "model decides"
EMPTY_BLOCK_HINT = "Double-click to describe the motion"
EXPECTED_SECONDS = 30.0

# Inline-edit state (written by timeline_operators, read by draw callback)
inline_edit_state = {
    "active": False,
    "index": -1,
    "text": "",
    "cursor": 0,
    "original": "",
    "selection_start": None,  # None = no selection, int = start of selection range
}

#: What the mouse is on and what is being dragged (timeline_operators writes it).
interaction = {
    "hover": None,       # block index under the mouse
    "zone": None,        # 'body' / 'edge_start' / 'edge_end'
    "pin": None,         # name of the waypoint under the mouse
    "drag": None,        # block index being dragged
    "key": None,         # key-pose frame under the mouse
    "key_drag": None,    # (from frame, to frame) while a key pose is dragged
    "band": None,        # (x0, y0, x1, y1) while a rubber band is drawn
}

#: The frames the last generation asked for (the take under review is that one).
activity = {"generating": None}

# Module-level draw-handler storage.  Mirrored onto bpy.app.driver_namespace
# so the handle survives module reloads — otherwise a reload (sys.modules pop
# + re-import) resets our local to None while Blender still holds the old
# handle, and register_draw_handler() would add a second one → duplicate draw.
_NS_KEY = "_animatica_timeline_overlay_handle"
_draw_handle = None


def _schedule_target_reset(scene) -> None:
    """Defer target-state cleanup to the next app tick via the shared
    ``properties.schedule_target_reset`` (mutating scene props from a draw
    callback is forbidden)."""
    from . import properties

    properties.schedule_target_reset(getattr(scene, "name", None) or "")


def markers_visible(context) -> bool:
    """True when Blender reserves the bottom of the timeline for its marker row.

    Blender's Markers keymap owns clicks in that row whenever the editor shows
    markers and the scene has any, ahead of the add-on's Dopesheet keymap.
    """
    space = getattr(context, "space_data", None)
    if space is None or not getattr(space, "show_markers", False):
        return False
    return len(context.scene.timeline_markers) > 0


#: the editor's horizontal scrollbar along the bottom (logical px): it takes
#: the clicks there before the add-on sees them, so the lane sits above it
SCROLLBAR = 14


def lane_y_offset(context) -> int:
    """Region-pixel Y of the bottom of the lane's content: above the scrollbar,
    and above the marker row when there is one, so the lane stays clickable
    and doesn't cover the markers."""
    ui_scale = context.preferences.system.ui_scale
    if not markers_visible(context):
        return STRIP_Y_OFFSET + math.ceil(SCROLLBAR * ui_scale)
    return STRIP_Y_OFFSET + math.ceil(MARKER_ROW_HEIGHT * ui_scale)


class Lane:
    """Where everything sits, in region pixels, bottom to top."""

    def __init__(self, context):
        u = context.preferences.system.ui_scale
        self.u = u
        base = lane_y_offset(context)
        self.y0 = base - LANE_PADDING * u
        self.wp0, self.wp1 = base, base + WAYPOINT_RAIL * u
        self.b0 = self.wp1 + RAIL_GAP * u
        self.b1 = self.b0 + get_strip_height()
        self.k0 = self.b1 + RAIL_GAP * u
        self.k1 = self.k0 + KEY_RAIL * u
        self.y1 = self.k1 + LANE_PADDING * u


def is_in_lane(context, mouse_y) -> bool:
    """Check if mouse Y is within the Animatica lane."""
    g = Lane(context)
    return g.y0 <= mouse_y <= g.y1


def get_strip_height():
    """The block row's height (region pixels)."""
    if _strip_height is None:
        return int(round(DEFAULT_BLOCK_HEIGHT * st.ui_scale()))
    return _strip_height


def set_strip_height(h):
    """Set the block row's height, clamped to [MIN, MAX]."""
    global _strip_height
    _strip_height = max(MIN_STRIP_HEIGHT, min(MAX_STRIP_HEIGHT, int(h)))


# ---------------------------------------------------------------------------
# Overlap helpers (single-track — strips must not overlap)
# ---------------------------------------------------------------------------

def get_sorted_blocks(prompt_blocks):
    """Return list of (index, frame_start, frame_end) sorted by frame_start."""
    items = [(i, fr.frame_start, fr.frame_end) for i, fr in enumerate(prompt_blocks)]
    items.sort(key=lambda x: x[1])
    return items


def find_neighbors(prompt_blocks, idx):
    """Find the left and right neighbor strips for strip *idx*.

    Returns (left_end, right_start) — the frame boundaries imposed by
    neighbors.  ``left_end`` is the frame_end of the nearest strip to
    the left (or 0 if none).  ``right_start`` is the frame_start of the
    nearest strip to the right (or None if none).
    """
    sorted_items = get_sorted_blocks(prompt_blocks)
    fr = prompt_blocks[idx]

    left_end = 0
    right_start = None

    for si, s_start, s_end in sorted_items:
        if si == idx:
            continue
        # Left neighbor: ends before or at our start
        if s_end <= fr.frame_start:
            left_end = max(left_end, s_end)
        # Right neighbor: starts at or after our end
        if s_start >= fr.frame_end:
            if right_start is None or s_start < right_start:
                right_start = s_start

    return left_end, right_start


def find_gap(prompt_blocks, min_length=10, scene_end=250):
    """Find the first gap on the timeline where a new strip can fit.

    Returns (gap_start, gap_end) or None if no room.
    """
    sorted_items = get_sorted_blocks(prompt_blocks)
    cursor = 1  # earliest possible frame

    for _idx, s_start, s_end in sorted_items:
        if s_start - cursor >= min_length:
            return (cursor, min(cursor + DEFAULT_BLOCK_LENGTH, s_start))
        cursor = max(cursor, s_end)

    # Gap after all existing strips
    if scene_end - cursor >= min_length:
        return (cursor, min(cursor + DEFAULT_BLOCK_LENGTH, scene_end))
    # Force-fit even if tiny
    if cursor < scene_end:
        return (cursor, scene_end)
    return None


def blocks_overlap(prompt_blocks, exclude_idx=-1):
    """Return True if any two strips (excluding *exclude_idx*) overlap."""
    items = [(fr.frame_start, fr.frame_end)
             for i, fr in enumerate(prompt_blocks) if i != exclude_idx]
    items.sort()
    for i in range(len(items) - 1):
        if items[i][1] > items[i + 1][0]:
            return True
    return False


# ---------------------------------------------------------------------------
# Hit-testing (used by timeline_operators.py)
# ---------------------------------------------------------------------------

def hit_test_strips(context, mouse_x, mouse_y):
    """Determine which strip (if any) is under the mouse cursor.

    Returns dict: {'index': int|None, 'zone': str|None}
        zone: 'body', 'edge_start', 'edge_end', or None
    """
    scene = context.scene
    if not hasattr(scene, "animatica"):
        return {"index": None, "zone": None}

    props = scene.animatica
    if len(props.prompt_blocks) == 0:
        return {"index": None, "zone": None}

    g = Lane(context)
    if mouse_y < g.b0 or mouse_y > g.b1:
        return {"index": None, "zone": None}
    view2d = context.region.view2d

    # The grab zone of an edge reaches in over the block's grips (drawn a few
    # pixels inside it) and a little out past it, at the UI scale: a fixed 6 px
    # missed the grips entirely on a scaled display.
    u = g.u
    inside, outside = max(EDGE_HANDLE_WIDTH, 11 * u), max(3, 3 * u)
    best = None
    for i, fr in enumerate(props.prompt_blocks):
        x_start, _ = view2d.view_to_region(fr.frame_start, 0, clip=False)
        x_end, _ = view2d.view_to_region(fr.frame_end, 0, clip=False)
        if mouse_x < x_start - outside or mouse_x > x_end + outside:
            continue
        # a block too narrow for both zones keeps a middle to grab it by
        reach = min(inside, max(2.0, (x_end - x_start) / 3))
        if x_start - outside <= mouse_x <= x_start + reach:
            hit = {"index": i, "zone": "edge_start"}
            d = abs(mouse_x - x_start)
        elif x_end - reach <= mouse_x <= x_end + outside:
            hit = {"index": i, "zone": "edge_end"}
            d = abs(mouse_x - x_end)
        else:
            return {"index": i, "zone": "body"}
        # two blocks that meet both claim the edge: the nearer side wins
        if best is None or d < best[0]:
            best = (d, hit)
    return best[1] if best else {"index": None, "zone": None}


def hit_test_waypoint_pin(context, mouse_x, mouse_y):
    """The waypoint whose flag on the lane is under the mouse, or None."""
    from . import waypoints

    g = Lane(context)
    if not (g.y0 - 2 * g.u <= mouse_y <= g.wp1 + 2 * g.u):
        return None
    grab = 6 * g.u  # flags are small; be generous
    view2d = context.region.view2d
    best, best_dx = None, grab + 1
    for obj in waypoints.waypoints(context.scene):
        x, _ = view2d.view_to_region(obj.animatica_waypoint_frame, 0, clip=False)
        dx = abs(mouse_x - x)
        if dx < best_dx:
            best, best_dx = obj, dx
    return best


def hit_test_key_pose(context, mouse_x, mouse_y):
    """The frame of the key-pose diamond under the mouse, or None."""
    g = Lane(context)
    if not (g.k0 - 2 * g.u <= mouse_y <= g.k1 + 2 * g.u):
        return None
    from . import key_poses
    ticks, _window = key_poses.timeline_ticks(context.scene)
    view2d = context.region.view2d
    grab = 6 * g.u
    best, best_dx = None, grab + 1
    for frame, _in_range in ticks:
        x, _ = view2d.view_to_region(frame, 0, clip=False)
        dx = abs(mouse_x - x)
        if dx < best_dx:
            best, best_dx = frame, dx
    return best


def hit_test_lane_resize(context, mouse_x, mouse_y):
    """Return True if mouse is in the lane top-border resize zone."""
    g = Lane(context)
    return abs(mouse_y - g.y1) <= RESIZE_HANDLE_HEIGHT * g.u


def pixel_to_frame(context, pixel_x):
    """Convert a region-pixel X coordinate to a frame number."""
    frame, _ = context.region.view2d.region_to_view(pixel_x, 0)
    return round(frame)


def track_hover(context, mouse_x, mouse_y) -> bool:
    """Note what the mouse is on. True when that changed (redraw)."""
    inside = is_in_lane(context, mouse_y)
    key = hit_test_key_pose(context, mouse_x, mouse_y) if inside else None
    pin = hit_test_waypoint_pin(context, mouse_x, mouse_y) if inside and key is None else None
    hit = (hit_test_strips(context, mouse_x, mouse_y) if pin is None and key is None
           else {"index": None, "zone": None})
    now = (hit["index"], hit["zone"], getattr(pin, "name", None), key)
    was = (interaction["hover"], interaction["zone"], interaction["pin"], interaction["key"])
    interaction["hover"], interaction["zone"], interaction["pin"], interaction["key"] = now
    return now != was


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def _is_unconditioned(fr) -> bool:
    """A block with no prompt becomes an MMCP UnconditionedSegment: the model
    fills it on its own."""
    return not (fr.prompt or "").strip()


def _strip_color(fr, index):
    """The block's identity colour (RGBA): its own, or the palette's.

    The swatch on its tile, and its ghosts and trail in the viewport
    (key_poses), muted the same way for a block with no prompt or one that
    is off.
    """
    if hasattr(fr, "color") and any(c > 0 for c in fr.color):
        color = list(fr.color)
    else:
        color = list(STRIP_COLORS[index % len(STRIP_COLORS)])

    if _is_unconditioned(fr):
        gray = 0.45
        for c in range(3):
            color[c] = gray * 0.6 + color[c] * 0.4
        color[3] *= 0.85

    if not fr.enabled:
        gray = sum(color[:3]) / 3.0
        for c in range(3):
            color[c] = gray * 0.7 + color[c] * 0.3
        color[3] *= DIM_DISABLED

    return color


def _dim(color, k):
    return (color[0], color[1], color[2], color[3] * k)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _font(size):
    blf.size(0, size)


def _fit(text, width):
    """``text`` cut to ``width`` px with an ellipsis, and whether it was cut."""
    if blf.dimensions(0, text)[0] <= width:
        return text, False
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if blf.dimensions(0, text[:mid].rstrip() + "…")[0] <= width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + "…") if lo > 0 else "", True


def _text(text, x, y, color):
    blf.position(0, x, y, 0)
    blf.color(0, *color)
    blf.draw(0, text)


def _baseline(y0, y1):
    """Where text sits to look centred between y0 and y1 (by cap height)."""
    h = blf.dimensions(0, "Ag")[1]
    return y0 + (y1 - y0 - h * 0.8) / 2


# ---------------------------------------------------------------------------
# GPU draw callback
# ---------------------------------------------------------------------------

def draw_timeline_strips():
    """POST_PIXEL draw callback registered on SpaceDopeSheetEditor."""
    context = bpy.context

    # Guard: only draw in Timeline mode
    if not hasattr(context, "space_data") or context.space_data is None:
        return
    if context.space_data.type != "DOPESHEET_EDITOR":
        return
    if context.space_data.mode != "TIMELINE":
        return

    scene = context.scene
    if not hasattr(scene, "animatica"):
        return

    props = scene.animatica
    from . import properties

    # Safety net: if the target rig vanished but the depsgraph handler has
    # not run yet, defer the cleanup to a one-shot timer (mutating scene
    # data from a draw callback is unsafe) and skip rendering this frame.
    if properties._live_armature(props.target_armature) is None:
        if len(props.prompt_blocks) > 0 or props.target_armature is not None:
            inline_edit_state["active"] = False
            inline_edit_state["index"] = -1
            _schedule_target_reset(scene)
        return

    region = context.region
    # Metal (macOS) limits textures to 16384px. Skip drawing on ultra-wide regions
    # to avoid MTLTextureDescriptor validation crash.
    if region.width > 16384 or region.height > 16384:
        return

    from . import key_poses  # noqa: PLC0415 — lazy, key_poses reads our colours
    from . import waypoints
    key_pose_ticks, _window = key_poses.timeline_ticks(scene)
    waypoint_frames = waypoints.timeline_frames(scene)
    try:
        window = tuple(key_poses.plan(scene)["range"])
    except Exception:                       # noqa: BLE001 — never break a draw
        window = (int(scene.frame_start), int(scene.frame_end))

    try:
        _draw(context, region, props, window, key_pose_ticks, waypoint_frames)
    finally:
        gpu.state.blend_set("NONE")


def _draw(context, region, props, window, key_pose_ticks, waypoint_frames):
    g = Lane(context)
    u = g.u
    view2d = region.view2d
    W = region.width

    def fx(frame):
        return view2d.view_to_region(frame, 0, clip=False)[0]

    # --- the lane: opaque, so the editor's lines don't run through it ---
    st.rounded((0, g.y0, W, g.y1), 0, st.EERIE_BLACK)
    st.lines([((0, g.y1), (W, g.y1))], 1, LANE_EDGE)
    # outside the frames the next Generate covers
    if props.prompt_blocks:
        wx0, wx1 = fx(window[0]), fx(window[1])
        if wx0 > 0:
            st.rounded((0, g.y0, min(W, wx0), g.y1), 0, OUT_OF_RANGE)
        if wx1 < W:
            st.rounded((max(0, wx1), g.y0, W, g.y1), 0, OUT_OF_RANGE)

    generating = bool(getattr(props, "is_generating", False))
    previewing = bool(getattr(props, "is_previewing", False)) and not generating
    span = activity.get("generating") or window
    from .toolbar import block_at
    under = block_at(props, int(context.scene.frame_current))
    hover, zone, drag = interaction["hover"], interaction["zone"], interaction["drag"]
    progress = 0.0
    if generating:
        elapsed = float(getattr(props, "generation_elapsed", 0))
        progress = 1.0 - math.exp(-elapsed / EXPECTED_SECONDS)

    if len(props.prompt_blocks) == 0:
        _draw_empty(g, W)

    tagged = {j for j in (hover, props.active_block_index, drag)
              if j is not None and 0 <= j < len(props.prompt_blocks)}
    any_text = any((b.prompt or "").strip() for b in props.prompt_blocks)
    label_room = W           # where the first block starts: room for the lane's name
    card = None              # (index, full prompt) wanting a hover card
    blocks = props.prompt_blocks
    for i, fr in enumerate(blocks):
        x_start, x_end = fx(fr.frame_start), fx(fr.frame_end)
        if x_end < 0 or x_start > W:
            continue
        label_room = min(label_room, x_start)
        # blocks that meet keep a hairline of lane between them
        x0 = max(x_start + BLOCK_GAP * u / 2, -4 * u)
        x1 = min(x_end - BLOCK_GAP * u / 2, W + 4 * u)
        if x1 - x0 < 1:
            x0, x1 = x_start, max(x_start + 1, x_end)
        rect = (x0, g.b0, x1, g.b1)
        r = BLOCK_RADIUS * u

        in_take = span[0] < fr.frame_end and fr.frame_start < span[1]
        k = 1.0
        if not fr.enabled:
            k = DIM_DISABLED
        elif generating and not in_take:
            k = DIM_GENERATING

        empty = _is_unconditioned(fr)
        is_under = i == under
        # The block in its own colour (the colour its ghosts and trail carry
        # in the viewport), deep enough for white text; lighter under the
        # mouse and under the playhead.
        sw = _strip_color(fr, i)
        lift = 0.86 if (i == hover or is_under) else 0.74
        fill = tuple(sw[c] * lift + st.TILE[c] * (1 - lift) for c in range(3)) + (1.0,)
        if empty:
            st.rounded(rect, r, _dim((1, 1, 1, 0.03), k))
            _dashed(rect, u, _dim(DASH, k))
        else:
            st.rounded(rect, r, _dim(fill, k))
        if is_under and not empty:
            # under the playhead: what the bar's Prompt and Redo act on
            st.rounded((x0, g.b1 - 2 * u, x1, g.b1), min(r, u), _dim((sw[0], sw[1], sw[2], 1.0), k))

        if not fr.enabled:
            _hatch(rect, u)

        if generating and in_take:
            sx = fx(span[0]) + progress * (fx(span[1]) - fx(span[0]))
            if sx > x0:
                st.rounded((x0, g.b0, min(x1, sx), g.b1), r, SWEEP)
        if previewing and in_take:
            tx0 = max(x0, fx(span[0]))
            tx1 = min(x1, fx(span[1]))
            if tx1 > tx0:
                st.rounded((tx0, g.b0, tx1, g.b0 + TAKE_STRIP * u), min(r, 1.5 * u), st.PRIMARY)

        if getattr(fr, "selected", False):
            st.outline(rect, r, max(1.0, (1.5 if i == props.active_block_index else 1.0) * u),
                       st.SOFT_ORANGE)

        # grips at both ends: the edges are handles
        wide = x1 - x0 > 30 * u
        grip_h = (g.b1 - g.b0) * 0.36
        cy = (g.b0 + g.b1) / 2
        if wide:
            gc = _dim((1, 1, 1, 0.45 if i in tagged else 0.28), k)
            for gx in (x0 + 4 * u, x1 - 4 * u):
                st.lines([((gx - 1.2 * u, cy - grip_h / 2), (gx - 1.2 * u, cy + grip_h / 2)),
                          ((gx + 1.2 * u, cy - grip_h / 2), (gx + 1.2 * u, cy + grip_h / 2))],
                         max(1.0, 0.8 * u), gc)

        # --- what it says: frames at the edges, the prompt in the middle, its length under it
        locked = bool(getattr(fr, "locked", False))
        tx = x0 + (8 + 3) * u if wide else x0 + 4 * u
        right = x1 - (8 + 3) * u if wide else x1 - 4 * u
        _font(TAG_SIZE * u)
        a_txt, b_txt = str(fr.frame_start), str(fr.frame_end)
        aw, bw = blf.dimensions(0, a_txt)[0], blf.dimensions(0, b_txt)[0]
        if right - tx - aw - bw - 12 * u >= MIN_LABEL_WIDTH * u:
            y = _baseline(g.b0, g.b1)
            _text(a_txt, tx, y, _dim(st.MUTED, k))
            _text(b_txt, right - bw, y, _dim(st.MUTED, k))
            tx += aw + 6 * u
            right -= bw + 6 * u
        if locked and right - tx > LOCK_SIZE * u + MIN_LABEL_WIDTH * u:
            ls = LOCK_SIZE * u
            st.icon("lock", right - ls / 2, cy, ls, _dim((1, 1, 1, 0.75), k))
            right -= ls + 4 * u
        size = min(TEXT_SIZE * u, (g.b1 - g.b0) * 0.5)
        # room for a second line: the length under the prompt
        two_lines = (g.b1 - g.b0) >= (TEXT_SIZE + TAG_SIZE + 10) * u
        if inline_edit_state["active"] and inline_edit_state["index"] == i:
            _draw_strip_text_editing(inline_edit_state["text"], inline_edit_state["cursor"],
                                     tx, right, g.b0, g.b1, size)
            continue
        length = f"{fr.frame_end - fr.frame_start}f"
        if i == drag:
            # while it moves: how long it is
            _font(size)
            nw = blf.dimensions(0, length)[0]
            if right - tx > nw:
                _text(length, (tx + right - nw) / 2, _baseline(g.b0, g.b1), st.WHITE)
            continue
        # With nothing typed anywhere, the empty block is where to start; with
        # prompts around it, it is a stretch the model fills on its own.
        label = fr.prompt if not empty else (UNCONDITIONED_LABEL if any_text else EMPTY_BLOCK_HINT)
        cut = True
        if x1 - x0 >= MIN_LABEL_WIDTH * u and right - tx > 8 * u:
            _font(size)
            shown, cut = _fit(label, right - tx)
            sw_ = blf.dimensions(0, shown)[0]
            color = st.MUTED if empty else st.WHITE
            mid = (tx + right - sw_) / 2
            if two_lines:
                line_y = cy + 1 * u
                _text(shown, mid, line_y, _dim(color, k if fr.enabled else 0.5))
                _font(TAG_SIZE * u)
                lw = blf.dimensions(0, length)[0]
                _text(length, (tx + right - lw) / 2, cy - TAG_SIZE * u - 2 * u, _dim(st.MUTED, k))
            else:
                _text(shown, mid, _baseline(g.b0, g.b1), _dim(color, k if fr.enabled else 0.5))
        if cut and i == hover and zone == "body" and drag is None:
            card = (i, label, x0)

    # --- the edge under the mouse ---
    _draw_edge_handle(g, blocks, fx, hover, zone, drag)
    # the whole prompt of a block whose label is cut: the tile, opened out
    if card is not None:
        _hover_card(card[1], card[2], g, W)

    band = interaction.get("band")
    if band is not None:
        bx0, by0, bx1, by1 = band
        rect = (min(bx0, bx1), min(by0, by1), max(bx0, bx1), max(by0, by1))
        st.rounded(rect, 0, st.with_alpha(st.SOFT_ORANGE, 0.12))
        st.outline(rect, 0, max(1.0, u * 0.75), st.with_alpha(st.SOFT_ORANGE, 0.8))

    _draw_key_pose_rail(g, W, fx, key_pose_ticks)
    _draw_waypoint_rail(g, W, fx, waypoint_frames)

    # the lane's name, where no block is
    _font(8 * u)
    name_w = blf.dimensions(0, "ANIMATICA")[0]
    if label_room > name_w + 14 * u:
        _text("ANIMATICA", 6 * u, _baseline(g.b0, g.b1), (1, 1, 1, 0.28))

    # the grip on the lane's top edge
    cx = W / 2
    st.lines([((cx + dx - 3 * u, g.y1 - 1.5 * u), (cx + dx + 3 * u, g.y1 - 1.5 * u))
              for dx in (-8 * u, 0, 8 * u)], max(1.0, u), (1, 1, 1, 0.18))


def _draw_empty(g, W):
    u = g.u
    _font(8 * u)
    left = 6 * u + blf.dimensions(0, "ANIMATICA")[0] + 10 * u    # after the lane's name
    rect = (left, g.b0, W - 12 * u, g.b1)
    _dashed(rect, u, (1, 1, 1, 0.16))
    _font(min(TEXT_SIZE * u, (g.b1 - g.b0) * 0.5))
    tw = blf.dimensions(0, EMPTY_HINT)[0]
    _text(EMPTY_HINT, max(rect[0] + 8 * u, (W - tw) / 2), _baseline(g.b0, g.b1), st.MUTED)


def _dashed(rect, u, color):
    """A dashed outline round ``rect``."""
    x0, y0, x1, y1 = rect
    dash, gap = 4 * u, 3 * u
    segs = []

    def run(ax, ay, bx, by):
        n = math.hypot(bx - ax, by - ay)
        if n <= 0:
            return
        t = 0.0
        while t < n:
            e = min(n, t + dash)
            segs.append(((ax + (bx - ax) * t / n, ay + (by - ay) * t / n),
                         (ax + (bx - ax) * e / n, ay + (by - ay) * e / n)))
            t += dash + gap

    h = max(1.0, u * 0.75) / 2
    run(x0, y0 + h, x1, y0 + h)
    run(x0, y1 - h, x1, y1 - h)
    run(x0 + h, y0, x0 + h, y1)
    run(x1 - h, y0, x1 - h, y1)
    st.lines(segs, max(1.0, u * 0.75), color)


def _hatch(rect, u):
    """45° hatching over a block that is off."""
    x0, y0, x1, y1 = rect
    h = y1 - y0
    step = 7 * u
    segs = []
    x = x0 - h
    while x < x1:
        ax, ay, bx, by = x, y0, x + h, y1
        if ax < x0:
            ay += x0 - ax
            ax = x0
        if bx > x1:
            by -= bx - x1
            bx = x1
        if bx > ax:
            segs.append(((ax, ay), (bx, by)))
        x += step
    st.lines(segs, max(1.0, u * 0.75), HATCH)


def _draw_edge_handle(g, blocks, fx, hover, zone, drag):
    """The edge under the mouse: a bar, with chevrons where two blocks meet
    (the drag's direction picks which one moves)."""
    if hover is None or zone not in ("edge_start", "edge_end") or drag is not None:
        return
    u = g.u
    fr = blocks[hover]
    frame = fr.frame_start if zone == "edge_start" else fr.frame_end
    x = fx(frame)
    st.rounded((x - u, g.b0 - u, x + u, g.b1 + u), u, st.WHITE)
    shared = any(j != hover and (b.frame_end == frame if zone == "edge_start" else b.frame_start == frame)
                 for j, b in enumerate(blocks))
    if shared:
        cy, s = (g.b0 + g.b1) / 2, 3 * u
        st.polygon(((x - 3 * u, cy), (x - 3 * u - s, cy + s), (x - 3 * u - s, cy - s)), st.WHITE)
        st.polygon(((x + 3 * u, cy), (x + 3 * u + s, cy - s), (x + 3 * u + s, cy + s)), st.WHITE)


def _hover_card(text, x, g, W):
    """The block's tile opened out over its neighbours, showing all of its
    prompt (as far as the editor is wide)."""
    u = g.u
    size = min(TEXT_SIZE * u, (g.b1 - g.b0) * 0.5)
    _font(size)
    pad = TEXT_PAD * u
    shown, _ = _fit(text, W - 8 * u - 2 * pad)
    w = blf.dimensions(0, shown)[0] + 2 * pad
    x0 = max(2 * u, min(x, W - w - 2 * u))
    rect = (x0, g.b0 - u, x0 + w, g.b1 + u)
    st.rounded((rect[0] - 2 * u, rect[1] - 2 * u, rect[2] + 2 * u, rect[3] + u), 5 * u, (0, 0, 0, 0.35))
    st.rounded(rect, BLOCK_RADIUS * u, st.TILE_HOVER)
    st.outline(rect, BLOCK_RADIUS * u, max(1.0, u * 0.5), (1, 1, 1, 0.18))
    _text(shown, x0 + pad, _baseline(g.b0, g.b1), st.WHITE)


def _clusters(xs, gap):
    """Positions closer than ``gap`` together: ``[(x, [items...]), ...]``."""
    out = []
    for x, item in sorted(xs, key=lambda p: p[0]):
        if out and x - out[-1][2] < gap:
            out[-1][1].append(item)
            out[-1][2] = x
        else:
            out.append([x, [item], x])
    return [((c[0] + c[2]) / 2, c[1]) for c in out]


def _draw_key_pose_rail(g, W, fx, ticks):
    """A diamond per key pose; hollow when the next Generate won't reach it."""
    if not ticks:
        return
    u = g.u
    s = DIAMOND * u
    cy = (g.k0 + g.k1) / 2
    drag = interaction.get("key_drag")
    hot = interaction.get("key")

    def diamond(x, k):
        return ((x, cy - s * k), (x + s * k, cy), (x, cy + s * k), (x - s * k, cy))

    marks = [(fx(f), (f, in_range)) for f, in_range in ticks]
    for x, group in _clusters([m for m in marks if -s <= m[0] <= W + s], CLUSTER * u):
        frames = {f for f, _r in group}
        if drag is not None and drag[0] in frames and len(group) == 1:
            # where it was: an outline left behind
            st.lines(list(zip(diamond(x, 1), diamond(x, 1)[1:] + diamond(x, 1)[:1])),
                     max(1.0, u * 0.75), (1, 1, 1, 0.35))
            continue
        if any(r for _f, r in group):
            big = hot in frames and drag is None
            st.polygon(diamond(x, 1.35 if big else 1), st.WHITE if big else (1, 1, 1, 0.92))
        else:
            st.lines(list(zip(diamond(x, 1), diamond(x, 1)[1:] + diamond(x, 1)[:1])),
                     max(1.0, u * 0.75), (1, 1, 1, 0.40))
        if len(group) > 1:
            _font(8 * u)
            _text(str(len(group)), x + s + 2 * u, cy - 3 * u, st.MUTED)
    from . import key_poses
    flash = key_poses._flash
    import time as _time
    age = _time.monotonic() - flash.get("at", 0.0)
    if flash.get("frame") is not None and age < 1.2:
        # just keyed: a ring that opens out from it
        x = fx(flash["frame"])
        k = 1.4 + age * 1.6
        ring = diamond(x, k)
        st.lines(list(zip(ring, ring[1:] + ring[:1])), max(1.0, 1.4 * u),
                 st.with_alpha(st.SOFT_ORANGE, max(0.0, 1.0 - age / 1.2)))
    if drag is not None:
        x = fx(drag[1])
        st.polygon(diamond(x, 1.35), st.REC)
        _font(8 * u)
        label = f"{drag[1]}"
        _text(label, x + 1.35 * s + 3 * u, cy - 3 * u, st.WHITE)


def _draw_waypoint_rail(g, W, fx, frames):
    """A flag per waypoint: where the character should be, and when."""
    if not frames:
        return
    u = g.u
    from . import waypoints
    hot = interaction.get("pin")
    hot_frame = None
    if hot:
        obj = bpy.context.scene.objects.get(hot)
        hot_frame = getattr(obj, "animatica_waypoint_frame", None)
    picked = {int(o.animatica_waypoint_frame) for o in waypoints.waypoints(bpy.context.scene)
              if o.select_get()}
    for x, group in _clusters([(fx(f), f) for f in frames if -8 * u <= fx(f) <= W + 8 * u], CLUSTER * u):
        color = (st.WHITE if hot_frame in group
                 else st.SOFT_ORANGE if picked & set(group) else WAYPOINT_COLOR)
        # the floating bar's Waypoint icon, so the two read as one thing
        size = g.wp1 - g.wp0
        st.icon("waypoint", x, (g.wp0 + g.wp1) / 2, size, color)
        if len(group) > 1:
            _font(8 * u)
            _text(str(len(group)), x + size / 2 + u, g.wp0 + u, st.MUTED)


def _draw_strip_text_editing(text, cursor_pos, area_left, area_right, y_bottom, y_top, size):
    """The prompt being typed: the text, a selection, a cursor."""
    area_width = area_right - area_left
    if area_width < 10:
        return
    u = st.ui_scale()
    _font(size)
    text_y = _baseline(y_bottom, y_top)

    # Keep the cursor in view: trim from the left when the text overflows.
    display_text = text
    left_trim = 0
    if blf.dimensions(0, text)[0] > area_width:
        w_before = blf.dimensions(0, text[:cursor_pos])[0]
        if w_before > area_width - 20 * u:
            target = w_before - area_width + 40 * u
            trimmed = 0.0
            while left_trim < cursor_pos:
                trimmed += blf.dimensions(0, text[left_trim])[0]
                left_trim += 1
                if trimmed >= target:
                    break
        display_text = text[left_trim:]
        while blf.dimensions(0, display_text)[0] > area_width and len(display_text) > 1:
            display_text = display_text[:-1]
    cursor_in_display = cursor_pos - left_trim

    sel_start = inline_edit_state.get("selection_start")
    if sel_start is not None and sel_start != cursor_pos:
        a = max(0, min(min(sel_start, cursor_pos) - left_trim, len(display_text)))
        b = max(0, min(max(sel_start, cursor_pos) - left_trim, len(display_text)))
        if a < b:
            sx0 = area_left + blf.dimensions(0, display_text[:a])[0]
            sx1 = min(area_right, area_left + blf.dimensions(0, display_text[:b])[0])
            st.rounded((sx0, y_bottom + 4 * u, sx1, y_top - 4 * u), 2 * u, SELECTION)

    _text(display_text, area_left, text_y, st.WHITE)

    cx = area_left + blf.dimensions(0, display_text[:cursor_in_display])[0]
    if area_left - 1 <= cx <= area_right + 1:
        st.lines([((cx, y_bottom + 5 * u), (cx, y_top - 5 * u))], max(1.0, u), st.PRIMARY)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_draw_handler():
    """Install the POST_PIXEL draw handler, replacing any left by an earlier
    module load.  The handle lives on ``bpy.app.driver_namespace`` so a reload
    finds it instead of stacking a second one — but it is replaced, not
    reused: the callback Blender holds belongs to the old module and would go
    on drawing from its globals."""
    global _draw_handle
    ns = bpy.app.driver_namespace
    stale = ns.pop(_NS_KEY, None)
    if stale is not None:
        try:
            bpy.types.SpaceDopeSheetEditor.draw_handler_remove(stale, "WINDOW")
        except (ValueError, RuntimeError):
            pass
    _draw_handle = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
        draw_timeline_strips, (), "WINDOW", "POST_PIXEL",
    )
    ns[_NS_KEY] = _draw_handle


def unregister_draw_handler():
    global _draw_handle
    ns = bpy.app.driver_namespace
    handle = _draw_handle or ns.get(_NS_KEY)
    if handle is not None:
        try:
            bpy.types.SpaceDopeSheetEditor.draw_handler_remove(handle, "WINDOW")
        except (ValueError, RuntimeError):
            pass  # Already removed by another unregister
    ns.pop(_NS_KEY, None)
    _draw_handle = None
