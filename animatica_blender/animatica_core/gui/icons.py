"""Inline SVG icon helpers for the Kimodo to Maya GUI.

Icon path strings come from the Claude Design handoff
(test/gui_design/html_handoff/project/Kimodo to Maya.html).  Rendered at
call time via QSvgRenderer and returned as QIcon or QPixmap so buttons
and QLabels can use them without shipping PNG assets.
"""

from .qt_compat import QtCore, QtGui, QtSvg


ICON_PATHS = {
    "gear":
        "M8 10.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zm5.5-2.5"
        "a5.5 5.5 0 0 0-.07-.87l1.4-1.09-1.5-2.6-1.64.66"
        "A5.5 5.5 0 0 0 10 3.28V1.5H6v1.78"
        "a5.5 5.5 0 0 0-1.69.82L2.67 3.44l-1.5 2.6 1.4 1.09"
        "A5.5 5.5 0 0 0 2.5 8c0 .3.03.59.07.87L1.17 9.96l1.5 2.6 1.64-.66"
        "c.52.35 1.09.62 1.69.82v1.78h4v-1.78"
        "a5.5 5.5 0 0 0 1.69-.82l1.64.66 1.5-2.6-1.4-1.09"
        "c.04-.28.07-.57.07-.87z",
    "bone":
        "M5.5 2.5a2 2 0 0 1 0 2.83L2.83 8l2.67 2.67"
        "a2 2 0 1 1-2.83 2.83L0 10.83V5.17L2.67 2.5"
        "a2 2 0 0 1 2.83 0zm5 0a2 2 0 0 1 2.83 0L16 5.17v5.66l-2.67 2.67"
        "a2 2 0 1 1-2.83-2.83L13.17 8l-2.67-2.67a2 2 0 0 1 0-2.83z",
    "folder":
        "M1.5 3.5A1 1 0 0 1 2.5 2.5h3.79L7.5 4H13.5"
        "a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1V3.5z",
    "folderOpen":
        "M1.5 4.5A1 1 0 0 1 2.5 3.5h3.79L7.5 5H13.5a1 1 0 0 1 1 1v1H1.5V4.5z"
        "m-1 3h15l-1.5 6h-12z",
    "wand":
        "M11 1l1 2 2 1-2 1-1 2-1-2-2-1 2-1 1-2zM4 7l1.5 3 3 1.5-3 1.5L4 16"
        "l-1.5-3L-.5 11.5l3-1.5L4 7z",
    "terminal":
        "M2 3.5A1.5 1.5 0 0 1 3.5 2h9A1.5 1.5 0 0 1 14 3.5v9"
        "a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12.5v-9z"
        "m2.5 2l2.5 2.5-2.5 2.5m4 0h3",
    "timeline":
        "M1 8h14M4 5v6M8 4v8M12 6v4",
    "trash":
        "M3 4.5h10M6.5 2.5h3M5 4.5v9a1 1 0 0 0 1 1h4"
        "a1 1 0 0 0 1-1v-9M6.5 7v4M9.5 7v4",
    "plus":
        "M8 2v12M2 8h12",
    "check":
        "M2.5 8l4 4 7-8",
    "spark":
        "M8 1l1.2 3.5L13 5.5 10 8l.9 3.5L8 9.5 5.1 11.5 6 8 3 5.5l3.8-1L8 1z",
    "chevronDown":
        "M4 6l4 4 4-4",
    "chevronRight":
        "M6 4l4 4-4 4",
    "chevronLeft":
        "M10 4l-4 4 4 4",
}


# Animatica brand mark — the slim "A" with the lower-left dot. Path data
# lifted from ``doc/design/animatica_logo_white.svg`` (Adobe Illustrator
# export, viewBox 0 0 958.7 835). The clipped EBEBEB inner highlight from
# the source SVG is dropped here so the small-size render stays legible
# and tints cleanly to a single colour.
_ANIMATICA_LOGO_VIEWBOX = "0 0 958.7 835"
_ANIMATICA_LOGO_PATHS = (
    '<path d="M661.7,707L418.7,172.5c-16.7-36.7-0.5-79.9,36.2-96.6'
    'c36.7-16.7,79.9-0.5,96.6,36.2L839.3,745c4.1,9,0.6,20.3-12.4,20.3'
    'l-29,0.3C742,765.6,696.6,771.6,661.7,707z"/>'
    '<circle cx="481.1" cy="656.5" r="87.8"/>'
    '<path d="M297,707.6l243.1-534.5c27.2-48.7,6.4-81.8-21.3-95.6'
    'c-3.7-1.8-7.9-3.1-11.7-4.3c-32.8-13.3-83.2,2.8-99.9,39.4L119.4,745.6'
    'c-4.1,9-0.6,20.3,12.4,20.3l29,0.3C216.7,766.2,262.2,772.2,297,707.6z"/>'
)


def _render_svg(svg_str, size):
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    if QtSvg is None:
        return pm
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg_str.encode("utf-8")))
    painter = QtGui.QPainter(pm)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return pm


def svg_pixmap(name, size=14, color="#8888a0", stroke=1.5):
    """Render ICON_PATHS[name] into a QPixmap at (size, size)."""
    d = ICON_PATHS[name]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"'
        f' viewBox="0 0 16 16" fill="none" stroke="{color}"'
        f' stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="{d}"/></svg>'
    )
    return _render_svg(svg, size)


def svg_icon(name, size=14, color="#8888a0", stroke=1.5):
    return QtGui.QIcon(svg_pixmap(name, size=size, color=color, stroke=stroke))


# ---------------------------------------------------------------------------
# Extended icon registry — full-SVG glyphs from the Compact GUI redesign
# (doc/design/gui_update/Compact GUI.html). Unlike ICON_PATHS (a single stroked
# path on a fixed 0 0 16 16 box), these carry their own viewBox, multiple
# elements, and a fill-vs-stroke render mode. Rendered via svg_pixmap_ex; the
# legacy ICON_PATHS / svg_pixmap path is left untouched for back-compat.
#
#   mode "stroke": root paints fill=none stroke=<color> (round caps/joins);
#                  per-icon "stroke" is the width in viewBox units.
#   mode "fill":   root paints fill=<color> stroke=none.
# The "__C__" sentinel in a body is replaced with the resolved colour, so a
# stroke-mode icon may still carry a few filled accents (e.g. timeline dots).
# ---------------------------------------------------------------------------

ICON_SVG = {
    # ---- section-header glyphs ----------------------------------------
    "skeleton_fig": {
        "viewbox": "0 0 24 24", "mode": "stroke", "stroke": 1.7,
        "body": (
            '<circle cx="12" cy="3.6" r="2"/><path d="M12 5.6V12"/>'
            '<path d="M9.5 7.6H14.5M9.8 9.4H14.2"/>'
            '<path d="M12 6.6 8.8 8.4 8.2 11M12 6.6 15.2 8.4 15.8 11"/>'
            '<path d="M9.8 12 12 13.2 14.2 12"/>'
            '<path d="M10.6 13 9.6 16.8 8.8 20M13.4 13 14.4 16.8 15.2 20"/>'
            '<path d="M8.8 20H7.3M15.2 20H16.7M7.7 11H8.7M15.3 11H16.3"/>'
        ),
    },
    "import_tray": {
        "viewbox": "0 0 24 24", "mode": "stroke", "stroke": 1.7,
        "body": (
            '<path d="M5 13v5h14v-5"/>'
            '<path d="M12 4v9M8.5 9.5 12 13l3.5-3.5"/>'
        ),
    },
    "run_figure": {
        "viewbox": "11 14 46 48", "mode": "stroke", "stroke": 3.4,
        "body": (
            '<path d="M 37.2151,16.2292C 39.4286,16.2292 41.2229,18.0235 '
            '41.2229,20.237C 41.2229,22.4505 39.4286,24.2448 37.2151,24.2448C '
            '35.0017,24.2448 33.2073,22.4505 33.2073,20.237C 33.2073,18.0235 '
            '35.0017,16.2292 37.2151,16.2292 Z"/>'
            '<path d="M 47.9027,57.4948C 47.9027,58.9704 46.7064,60.1667 '
            '45.2308,60.1667C 43.7551,60.1667 42.5589,58.9704 42.5589,57.4948L '
            '42.5589,48.8557L 38.8699,43.7381L 36.949,48.9609C 36.8408,49.3345 '
            '36.6581,49.6664 36.4218,49.945L 35.9497,50.7547L 29.9014,57.887C '
            '28.9469,59.0124 27.2609,59.151 26.1355,58.1966C 25.01,57.2422 '
            '24.8714,55.5562 25.8258,54.4307L 31.8742,47.2985L 31.8859,47.2848L '
            '34.658,39.7477L 34.5433,38.7917L 34.5433,29.4401C 34.5433,27.2267 '
            '36.3376,25.4323 38.5511,25.4323C 40.0854,25.4323 41.4184,26.2945 '
            '42.0919,27.5608L 46.7549,32.0476L 52.7735,33.5394C 53.8477,33.8056 '
            '54.5027,34.8923 54.2365,35.9665C 53.9702,37.0407 52.8835,37.6957 '
            '51.8093,37.4295L 45.6796,35.9102C 45.3136,35.8195 44.9963,35.6336 '
            '44.7475,35.3853C 44.4599,35.2957 44.1883,35.1395 43.9564,34.9164L '
            '42.5589,33.5718L 42.5589,38.7917L 42.4765,39.604L 47.4696,46.5308C '
            '47.8994,47.1466 48.0338,47.8803 47.9026,48.5638L 47.9027,57.4948 Z"/>'
            '<path d="M 27.7083,31.6667L 19.7916,31.6667"/>'
            '<path d="M 25.7292,39.5833L 17.0208,39.5833"/>'
            '<path d="M 21.375,47.5L 13.4583,47.5"/>'
        ),
    },
    "pose_figure": {
        "viewbox": "15 15 46 46", "mode": "fill",
        "body": (
            '<path d="M 39.75,19C 41.683,19 43.25,20.567 43.25,22.5C '
            '43.25,24.433 41.683,26 39.75,26C 37.817,26 36.25,24.433 36.25,22.5C '
            '36.25,20.567 37.817,19 39.75,19 Z M 46.5,55.2209C 46.6722,56.0312 '
            '46.155,56.8277 45.3446,57C 44.5343,57.1722 43.7378,56.655 '
            '43.5655,55.8446L 41.8509,47.7778L 41.8237,47.5974L 36.7097,41.5027L '
            '35.2594,41.0999L 34.4691,46.7234C 34.4213,47.0638 33.941,48.0815 '
            '33.7957,48.3053L 28.766,56.0502C 28.3148,56.745 27.3858,56.9425 '
            '26.6911,56.4913C 25.9963,56.0401 25.7988,55.1111 26.25,54.4163L '
            '31.4983,46.3058L 32.5291,38.1631C 32.5752,37.8357 33.2237,34.7979 '
            '34.2165,30.4882L 30.4611,32.4934L 29.4548,37.715C 29.3242,38.3928 '
            '28.6687,38.8365 27.9908,38.7058C 27.313,38.5752 26.8693,37.9198 '
            '27,37.2419L 28.0307,31.8935C 28.0469,31.8095 28.3476,30.7878 '
            '28.7639,30.5655L 35.5516,26.9412C 35.6263,26.9013 36.9417,26.2501 '
            '37.875,26.2501C 39.5756,26.2501 41,27 41.2491,28.4396L 44,34.3389L '
            '44.0244,34.3947L 48.5878,36.3317C 49.2232,36.6014 49.5197,37.3353 '
            '49.25,37.9708C 48.9802,38.6062 48.2464,38.9027 47.6109,38.633L '
            '42.7006,36.5486C 42.3459,36.3981 42.0968,36.1029 41.7342,35.3954L '
            '40.4968,32.7418L 39.02,39.5889L 44.2293,45.797C 44.4391,46.0471 '
            '44.7459,46.9684 44.7853,47.154L 46.5,55.2209 Z"/>'
        ),
    },
    "timeline_keys": {
        "viewbox": "0 0 24 24", "mode": "stroke", "stroke": 1.7,
        "body": (
            '<path d="M3 16h18"/><path d="M7 11.5V16M12 10.5V16M17 11.5V16"/>'
            '<circle cx="7" cy="9" r="1.5" fill="__C__" stroke="none"/>'
            '<circle cx="12" cy="8" r="1.5" fill="__C__" stroke="none"/>'
            '<circle cx="17" cy="9" r="1.5" fill="__C__" stroke="none"/>'
        ),
    },
    # Transport glyphs for Live Drive. Filled, like the transport of any
    # recorder: a stroked outline reads as "shape", not "go"/"arm".
    "play_tri": {
        "viewbox": "0 0 16 16", "mode": "fill",
        "body": '<path d="M5 3.3 12.4 8 5 12.7 Z"/>',
    },
    "record_dot": {
        "viewbox": "0 0 16 16", "mode": "fill",
        "body": '<circle cx="8" cy="8" r="4.6"/>',
    },
    "stop_sq": {
        "viewbox": "0 0 16 16", "mode": "fill",
        "body": '<rect x="4" y="4" width="8" height="8" rx="1.2"/>',
    },
    # ---- constraint-type glyphs (per-type colour applied by caller) ----
    "c_fullbody": {
        "viewbox": "55 0 96 206.326", "mode": "fill",
        "body": (
            '<path d="M104.265,117.959c-0.304,3.58,2.126,22.529,3.38,29.959'
            'c0.597,3.52,2.234,9.255,1.645,12.3c-0.841,4.244-1.084,9.736-0.621,12.934'
            'c0.292,1.942,1.211,10.899-0.104,14.175c-0.688,1.718-1.949,10.522-1.949,10.522'
            'c-3.285,8.294-1.431,7.886-1.431,7.886c1.017,1.248,2.759,0.098,2.759,0.098'
            'c1.327,0.846,2.246-0.201,2.246-0.201c1.139,0.943,2.467-0.116,2.467-0.116'
            'c1.431,0.743,2.758-0.627,2.758-0.627c0.822,0.414,1.023-0.109,1.023-0.109'
            'c2.466-0.158-1.376-8.05-1.376-8.05c-0.92-7.088,0.913-11.033,0.913-11.033'
            'c6.004-17.805,6.309-22.53,3.909-29.24c-0.676-1.937-0.847-2.704-0.536-3.545'
            'c0.719-1.941,0.195-9.748,1.072-12.848c1.692-5.979,3.361-21.142,4.231-28.217'
            'c1.169-9.53-4.141-22.308-4.141-22.308c-1.163-5.2,0.542-23.727,0.542-23.727'
            'c2.381,3.705,2.29,10.245,2.29,10.245c-0.378,6.859,5.541,17.342,5.541,17.342'
            'c2.844,4.332,3.921,8.442,3.921,8.747c0,1.248-0.273,4.269-0.273,4.269'
            'l0.109,2.631c0.049,0.67,0.426,2.977,0.365,4.092c-0.444,6.862,0.646,5.571,0.646,5.571'
            'c0.92,0,1.931-5.522,1.931-5.522c0,1.424-0.348,5.687,0.42,7.295'
            'c0.919,1.918,1.595-0.329,1.607-0.78c0.243-8.737,0.768-6.448,0.768-6.448'
            'c0.511,7.088,1.139,8.689,2.265,8.135c0.853-0.407,0.073-8.506,0.073-8.506'
            'c1.461,4.811,2.569,5.577,2.569,5.577c2.411,1.693,0.92-2.983,0.585-3.909'
            'c-1.784-4.92-1.839-6.625-1.839-6.625c2.229,4.421,3.909,4.257,3.909,4.257'
            'c2.174-0.694-1.9-6.954-4.287-9.953c-1.218-1.528-2.789-3.574-3.245-4.789'
            'c-0.743-2.058-1.304-8.674-1.304-8.674c-0.225-7.807-2.155-11.198-2.155-11.198'
            'c-3.3-5.282-3.921-15.135-3.921-15.135l-0.146-16.635c-1.157-11.347-9.518-11.429-9.518-11.429'
            'c-8.451-1.258-9.627-3.988-9.627-3.988c-1.79-2.576-0.767-7.514-0.767-7.514'
            'c1.485-1.208,2.058-4.415,2.058-4.415c2.466-1.891,2.345-4.658,1.206-4.628'
            'c-0.914,0.024-0.707-0.733-0.707-0.733C115.068,0.636,104.01,0,104.01,0h-1.688'
            'c0,0-11.063,0.636-9.523,13.089c0,0,0.207,0.758-0.715,0.733'
            'c-1.136-0.03-1.242,2.737,1.215,4.628c0,0,0.572,3.206,2.058,4.415'
            'c0,0,1.023,4.938-0.767,7.514c0,0-1.172,2.73-9.627,3.988'
            'c0,0-8.375,0.082-9.514,11.429l-0.158,16.635c0,0-0.609,9.853-3.922,15.135'
            'c0,0-1.921,3.392-2.143,11.198c0,0-0.563,6.616-1.303,8.674'
            'c-0.451,1.209-2.021,3.255-3.249,4.789c-2.408,2.993-6.455,9.24-4.29,9.953'
            'c0,0,1.689,0.164,3.909-4.257c0,0-0.046,1.693-1.827,6.625'
            'c-0.35,0.914-1.839,5.59,0.573,3.909c0,0,1.117-0.767,2.569-5.577'
            'c0,0-0.779,8.099,0.088,8.506c1.133,0.555,1.751-1.047,2.262-8.135'
            'c0,0,0.524-2.289,0.767,6.448c0.012,0.451,0.673,2.698,1.596,0.78'
            'c0.779-1.608,0.429-5.864,0.429-7.295c0,0,0.999,5.522,1.933,5.522'
            'c0,0,1.099,1.291,0.648-5.571c-0.073-1.121,0.32-3.422,0.369-4.092'
            'l0.106-2.631c0,0-0.274-3.014-0.274-4.269c0-0.311,1.078-4.415,3.921-8.747'
            'c0,0,5.913-10.488,5.532-17.342c0,0-0.082-6.54,2.299-10.245'
            'c0,0,1.69,18.526,0.545,23.727c0,0-5.319,12.778-4.146,22.308'
            'c0.864,7.094,2.53,22.237,4.226,28.217c0.886,3.094,0.362,10.899,1.072,12.848'
            'c0.32,0.847,0.152,1.627-0.536,3.545c-2.387,6.71-2.083,11.436,3.921,29.24'
            'c0,0,1.848,3.945,0.914,11.033c0,0-3.836,7.892-1.379,8.05'
            'c0,0,0.192,0.523,1.023,0.109c0,0,1.327,1.37,2.761,0.627'
            'c0,0,1.328,1.06,2.463,0.116c0,0,0.91,1.047,2.237,0.201'
            'c0,0,1.742,1.175,2.777-0.098c0,0,1.839,0.408-1.435-7.886'
            'c0,0-1.254-8.793-1.945-10.522c-1.318-3.275-0.387-12.251-0.106-14.175'
            'c0.453-3.216,0.21-8.695-0.618-12.934c-0.606-3.038,1.035-8.774,1.641-12.3'
            'c1.245-7.423,3.685-26.373,3.38-29.959l1.008,0.354'
            'C103.809,118.312,104.265,117.959,104.265,117.959z"/>'
        ),
    },
    "c_leg": {
        "viewbox": "0 0 128 128", "mode": "stroke", "stroke": 9,
        "body": (
            '<path d="M17.58 12.11l29.27.01c.83 0 1.62.33 2.2.92s.89 1.38.88 2.21'
            'c-.32 17.78.11 40.34 3.99 46.91c5.33 9.03 14.13 18.13 19.22 21.39'
            'c2.21 1.41 4.17 3.33 6.26 5.36c.79.77 1.61 1.57 2.45 2.34'
            'c3.38 3.11 6.73 5.12 11.57 6.93c.41.15.81.31 1.2.46c2.04.79 4.15 1.61 6.71 1.84'
            'c.52.05 1.17.07 2.06.07c.76 0 1.62-.02 2.5-.03c.88-.02 1.78-.03 2.61-.03'
            'c1.15 0 1.95.03 2.4.1c1.42.21 2.71.9 3.96 1.57c1.44.76 2.8 1.49 4.38 1.49'
            'c.48 0 .95-.07 1.4-.21l.09.06c.33.26.62 1.29.48 1.68l-.37.58'
            'c-.69 1.07-1.14 2.28-1.35 3.61c-.17 1.1-.64 2.57-1.96 3.31'
            'c-.8.45-1.53 1.02-2.16 1.7c-.42.45-1.18 1.02-2.43 1.23'
            'c-.95.15-1.84.44-2.64.85c-.54.28-1.65.77-3.02.96a9.28 9.28 0 0 0-4.37 1.82'
            'c-.66.5-1.68 1.1-2.76 1.1c-.15 0-.31-.01-.46-.04c-.94-.15-1.41-.32-1.61-.4'
            'c-1.26-.54-2.55-.82-3.83-.82c-.67 0-1.33.08-1.97.23c-2.05.48-5.22 1.06-8.69 1.06'
            'c-3.53 0-6.62-.59-9.2-1.76c-3.43-1.55-7.5-3.83-11.44-6.04'
            'c-7.88-4.41-15.32-8.58-20.83-9.19c-1.91-.21-4.43-.28-7.34-.35'
            'c-7.75-.2-20.73-.53-24.18-4.87c-4.47-5.61-5.87-11.02-.5-23.56'
            'c5.66-13.2 5.78-21.69 5.51-35.57c-.16-8.2-.69-17.39-1.1-23.65'
            'c-.06-.86.24-1.68.82-2.3c.59-.63 1.39-.97 2.25-.97"/>'
            '<path d="M120.72 108.62c-1.01-1.2-2.15-1.33-3-1.25c-1.31.12-1.29-2.2-4.04-2.7"/>'
            '<path d="M117.6 114.12c-.8-1.37-2.43-3.02-4-3.25c-1.38-3.12-3.24-4.42-6.92-5.12"/>'
            '<path d="M112.35 117c.02-1.85-2.15-3.73-4-3.88c-1.31-2.89-5.5-4.74-8.58-5.46"/>'
            '<path d="M105.18 119.33c.03-.79-.68-1.96-1.33-2.42c-.66-.46-1.19-.7-1.83-1.17'
            'c-.69-.5-.61-.77-1.03-1.5c-1.26-2.17-5.23-2.95-7.64-3.13"/>'
            '<path d="M24.06 68.69c-2.13 2.52-3.15 5.96-2.72 9.24s2.28 6.34 4.99 8.24"/>'
        ),
    },
    "c_arm": {
        "viewbox": "0 0 24 24", "mode": "stroke", "stroke": 1.7,
        "body": (
            '<path d="M9 11.5V8a1.1 1.1 0 0 1 2.2 0v2.4"/>'
            '<path d="M11.2 10.4V6.6a1.1 1.1 0 0 1 2.2 0V10.4"/>'
            '<path d="M13.4 10.4V7.2a1.1 1.1 0 0 1 2.2 0V11.4"/>'
            '<path d="M15.6 11.4V8.8a1.1 1.1 0 0 1 2.2 0V15a4.6 4.6 0 0 1-4.6 4.5'
            'h-1.2a4.6 4.6 0 0 1-3.4-1.5l-2.8-3a1.2 1.2 0 0 1 1.75-1.65L8.9 14"/>'
        ),
    },
    "c_path": {
        "viewbox": "10 46 277 205", "mode": "stroke", "stroke": 14,
        "body": (
            '<g transform="rotate(90 148.5 148.5)">'
            '<path d="M 119.16,80 V 105 A 14,14 0 0 1 105.16,119 H 85 A 20,20 0 0 0 '
            '65,139 V 158 A 20,20 0 0 0 85,178 H 148 A 20,20 0 0 1 168,198 V 223 '
            'A 20,20 0 0 1 148,243 H 46 A 20,20 0 0 0 26,263"/>'
            '<circle cx="119.16" cy="62" r="15"/><circle cx="119.16" cy="105" r="10"/>'
            '<circle cx="65" cy="148" r="10"/><circle cx="168" cy="210" r="10"/>'
            '<circle cx="26" cy="278" r="15"/></g>'
        ),
    },
    # ---- constraint action-button glyphs (filled, circle-enclosed) ----
    "c_add": {
        "viewbox": "0 0 512 512", "mode": "fill",
        "body": (
            '<path d="M256,0C114.6,0,0,114.6,0,256s114.6,256,256,256s256-114.6,'
            '256-256S397.4,0,256,0z M405.3,277.3c0,11.8-9.5,21.3-21.3,21.3h-85.3V384'
            'c0,11.8-9.5,21.3-21.3,21.3h-42.7c-11.8,0-21.3-9.6-21.3-21.3v-85.3H128'
            'c-11.8,0-21.3-9.6-21.3-21.3v-42.7c0-11.8,9.5-21.3,21.3-21.3h85.3V128'
            'c0-11.8,9.5-21.3,21.3-21.3h42.7c11.8,0,21.3,9.6,21.3,21.3v85.3H384'
            'c11.8,0,21.3,9.6,21.3,21.3V277.3z"/>'
        ),
    },
    "c_convert": {
        "viewbox": "0 0 24 24", "mode": "fill",
        "body": (
            '<path d="M12 .037C5.373.037 0 5.394 0 12c0 6.606 5.373 11.963 12 11.963'
            ' 6.628 0 12-5.357 12-11.963C24 5.394 18.627.037 12 .037zm-.541 4.8'
            'c1.91-.13 3.876.395 5.432 1.934 1.426 1.437 2.51 3.44 2.488 5.317h2.133'
            'l-4.444 4.963-4.445-4.963h2.313c-.001-1.724-.427-2.742-1.78-4.076'
            '-1.325-1.336-2.667-2.11-4.978-2.303a9.245 9.245 0 0 1 3.281-.871z'
            'M6.934 6.95l4.445 4.963H9.066c0 1.724.426 2.742 1.778 4.076'
            ' 1.326 1.336 2.667 2.112 4.978 2.305-2.684 1.268-6.22 1.398-8.71-1.064'
            '-1.427-1.437-2.512-3.44-2.489-5.317H2.488L6.934 6.95z"/>'
        ),
    },
}


def _render_svg_fit(svg_str, size):
    """Render *svg_str* centred inside a ``size``×``size`` pixmap, preserving the
    viewBox aspect ratio. Unlike ``_render_svg`` (which fills the whole device —
    fine for the square legacy icons and the brand mark), this fits non-square
    viewBoxes without distortion. The SVG must omit width/height so the
    renderer's ``defaultSize`` reflects the viewBox aspect.
    """
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    if QtSvg is None:
        return pm
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg_str.encode("utf-8")))
    ds = renderer.defaultSize()
    painter = QtGui.QPainter(pm)
    try:
        if ds.width() > 0 and ds.height() > 0:
            scale = min(size / ds.width(), size / ds.height())
            w, h = ds.width() * scale, ds.height() * scale
            target = QtCore.QRectF((size - w) / 2.0, (size - h) / 2.0, w, h)
            renderer.render(painter, target)
        else:
            renderer.render(painter)
    finally:
        painter.end()
    return pm


def _viewbox_mirror_tx(viewbox: str) -> float:
    """Horizontal-mirror translate offset for a viewBox ``minx miny w h``.

    Reflecting about the box's vertical centre is ``scale(-1,1)`` followed by
    ``translate(2*minx + w, 0)`` so the glyph stays inside the same box.
    """
    parts = viewbox.split()
    minx, w = float(parts[0]), float(parts[2])
    return 2 * minx + w


def svg_pixmap_ex(name, size=16, color="#C2B8AB", mirror=False):
    """Render an ICON_SVG glyph into a QPixmap, optionally mirrored horizontally.

    ``mirror`` reuses a left-limb glyph for its right-limb counterpart (matches
    the mockup's ``transform:scaleX(-1)``). The legacy ``svg_pixmap`` is for the
    single-path ICON_PATHS set; this one handles full-SVG, fill-or-stroke glyphs.
    """
    spec = ICON_SVG[name]
    vb = spec["viewbox"]
    body = spec["body"].replace("__C__", color)
    if spec.get("mode") == "fill":
        paint = f'fill="{color}" stroke="none"'
    else:
        sw = spec.get("stroke", 1.7)
        paint = (
            f'fill="none" stroke="{color}" stroke-width="{sw}"'
            f' stroke-linecap="round" stroke-linejoin="round"'
        )
    if mirror:
        tx = _viewbox_mirror_tx(vb)
        body = f'<g transform="translate({tx},0) scale(-1,1)">{body}</g>'
    # No width/height: _render_svg_fit reads defaultSize from the viewBox so a
    # non-square glyph (c_fullbody 96×206, c_path 277×205, …) is letterboxed,
    # not stretched into the square pixmap.
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" {paint}>{body}</svg>'
    )
    return _render_svg_fit(svg, size)


def svg_icon_ex(name, size=16, color="#C2B8AB", mirror=False):
    return QtGui.QIcon(svg_pixmap_ex(name, size=size, color=color, mirror=mirror))


def header_mark_pixmap(color="#ED8E5C", size=16):
    """Render the Animatica brand mark filled with *color*."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"'
        f' viewBox="{_ANIMATICA_LOGO_VIEWBOX}" fill="{color}" stroke="none">'
        f'{_ANIMATICA_LOGO_PATHS}</svg>'
    )
    return _render_svg(svg, size)


# Back-compat alias for any future code that wants the brand explicitly.
animatica_logo_pixmap = header_mark_pixmap
