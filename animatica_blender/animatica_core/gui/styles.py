"""Ember theme — palette + stylesheets for the Animatica tool window.

Values mirror the CSS variables in the reference design
``doc/design/animatica_core _standalone_.html``. The MoBu-tuned palette
sits on neutral mid-greys instead of the original near-black so the
window blends with MotionBuilder's UI shell.
"""

# ---- Ember palette (active) ------------------------------------------------

EMBER = {
    "bg_0":          "#3D3D3D",
    "bg_1":          "#454545",
    "bg_2":          "#4D4D4D",
    "bg_3":          "#575757",
    "border":        "#2E2E2E",
    "border_s":      "#666666",
    "text":          "#F2ECE4",
    "text_2":        "#C2B8AB",
    "text_3":        "#8A7F71",
    "accent":        "#ED8E5C",
    "accent_h":      "#F4A476",
    "accent_2":      "#6E4530",
    "accent_soft":   "rgba(237,142,92,0.16)",
    "accent_border": "rgba(237,142,92,0.35)",
    "on_accent":     "#1a0e07",
    "success":       "#A6CF7B",
    "danger":        "#DE6E61",
    "info":          "#8FB6DA",
}

PALETTES = {"ember": EMBER}
ACTIVE = EMBER

# ---- Back-compat aliases used across the existing UI -----------------------

BG_DARK              = ACTIVE["bg_0"]
BG_MID               = ACTIVE["bg_2"]
BG_SURFACE           = ACTIVE["bg_1"]
BG_RAISED            = "rgba(255,255,255,0.04)"
BG_RAISED_HOVER      = "rgba(255,255,255,0.08)"
INPUT_BG             = ACTIVE["bg_3"]

TEXT_PRIMARY         = ACTIVE["text"]
TEXT_SECONDARY       = ACTIVE["text_2"]
TEXT_MUTED           = ACTIVE["text_3"]

ACCENT               = ACTIVE["accent"]
ACCENT_HOVER         = ACTIVE["accent_h"]
ACCENT_PRESSED       = ACTIVE["accent_2"]
ACCENT_SOFT          = ACTIVE["accent_soft"]
ACCENT_SOFT_BORDER   = ACTIVE["accent_border"]
ON_ACCENT            = ACTIVE["on_accent"]

BORDER               = ACTIVE["border"]
BORDER_LIGHT         = ACTIVE["border_s"]
BORDER_FOCUS         = "rgba(237,142,92,0.5)"

# Short aliases (parity with maya_kimodo's styles — used by the timeline port).
BG_0      = ACTIVE["bg_0"]
BG_1      = ACTIVE["bg_1"]
BG_2      = ACTIVE["bg_2"]
BG_3      = ACTIVE["bg_3"]
TEXT      = ACTIVE["text"]
TEXT_2    = ACTIVE["text_2"]
TEXT_3    = ACTIVE["text_3"]
BORDER_S  = ACTIVE["border_s"]
ACCENT_H  = ACTIVE["accent_h"]
ACCENT_2  = ACTIVE["accent_2"]

DANGER               = ACTIVE["danger"]
DANGER_SOFT          = "rgba(222,110,97,0.14)"
DANGER_SOFT_HOVER    = "rgba(222,110,97,0.22)"
DANGER_BORDER        = "rgba(222,110,97,0.25)"
DANGER_BORDER_HOVER  = "rgba(222,110,97,0.40)"

SUCCESS              = ACTIVE["success"]
INFO                 = ACTIVE["info"]

CONSOLE_BG           = ACTIVE["bg_0"]

# Prompt-block tints cycled by ``PromptBox.color_idx``. Pastel set from the
# new standalone HTML design — accent ember first, then five analogous
# tints that read clearly against the mid-grey timeline track.
PROMPT_COLORS = [
    "#ED8E5C",  # accent ember
    "#9CC0E8",  # blue
    "#9DD4B8",  # green
    "#E0A7B7",  # pink
    "#D4A8DC",  # purple
    "#E8C087",  # amber
]
PROMPT_ORANGE = PROMPT_COLORS[0]
PROMPT_BLUE   = PROMPT_COLORS[1]
PROMPT_GREEN  = PROMPT_COLORS[2]
PROMPT_RED    = PROMPT_COLORS[3]
PROMPT_PURPLE = PROMPT_COLORS[4]
PROMPT_TEAL   = "#9DD4B8"


# ---- Stylesheet builders ---------------------------------------------------

def window_stylesheet():
    return f"""
    QWidget {{
        background-color: {BG_DARK};
        color: {TEXT_PRIMARY};
        font-family: "Geist", "Inter", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    """


def button_stylesheet():
    return f"""
    QPushButton {{
        background-color: {BG_RAISED};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        padding: 0 14px;
        min-height: 30px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background-color: {BG_RAISED_HOVER};
        border-color: {BORDER_LIGHT};
    }}
    QPushButton:pressed {{
        background-color: {BG_RAISED};
    }}
    QPushButton:disabled {{
        background-color: rgba(255,255,255,0.02);
        color: {TEXT_MUTED};
        border-color: {BORDER};
    }}

    QPushButton#accent_btn {{
        background-color: {ACCENT};
        border: 1px solid {ACCENT};
        color: {ON_ACCENT};
        font-weight: 600;
    }}
    QPushButton#accent_btn:hover {{
        background-color: {ACCENT_HOVER};
        border-color: {ACCENT_HOVER};
    }}
    QPushButton#accent_btn:pressed {{
        background-color: {ACCENT_PRESSED};
        border-color: {ACCENT_PRESSED};
    }}
    QPushButton#accent_btn:disabled {{
        background-color: rgba(237,142,92,0.28);
        border-color: rgba(237,142,92,0.28);
        color: rgba(26,14,7,0.6);
    }}

    QPushButton#soft_btn {{
        background-color: {ACCENT_SOFT};
        border: 1px solid {ACCENT_SOFT_BORDER};
        color: {ACCENT};
    }}
    QPushButton#soft_btn:hover {{
        background-color: rgba(237,142,92,0.24);
    }}

    QPushButton#danger_btn {{
        background-color: {DANGER_SOFT};
        border: 1px solid {DANGER_BORDER};
        color: {DANGER};
    }}
    QPushButton#danger_btn:hover {{
        background-color: {DANGER_SOFT_HOVER};
        border-color: {DANGER_BORDER_HOVER};
    }}

    QPushButton#ghost_accent_btn {{
        background-color: transparent;
        border: 1px solid {BORDER};
        color: {TEXT_SECONDARY};
        padding: 0 10px;
        min-height: 26px;
        border-radius: 5px;
        font-size: 11px;
        font-weight: 500;
    }}
    QPushButton#ghost_accent_btn:hover {{
        background-color: {BG_RAISED_HOVER};
        color: {TEXT_PRIMARY};
        border-color: {BORDER_LIGHT};
    }}

    /* Icon-only round-ish ghost button (used by atoms.IconBtn). */
    QPushButton#ibtn {{
        background: transparent;
        border: 1px solid transparent;
        padding: 0;
        min-width: 22px;
        min-height: 22px;
        border-radius: 4px;
        color: {TEXT_SECONDARY};
    }}
    QPushButton#ibtn:hover {{
        background-color: {BG_RAISED_HOVER};
        color: {TEXT_PRIMARY};
    }}
    QPushButton#ibtn[danger="true"]:hover {{
        background-color: {DANGER_SOFT_HOVER};
        color: {DANGER};
    }}
    """


def input_stylesheet():
    return f"""
    QLineEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {INPUT_BG};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        padding: 6px 10px;
        border-radius: 6px;
        font-size: 12px;
        selection-background-color: {ACCENT};
        selection-color: {ON_ACCENT};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {BORDER_FOCUS};
    }}
    /* The base rule sets an explicit color/background, so Qt's disabled
       palette never shows through -- an enable-gated input (e.g. Root margin
       under Server post-processing) would look identical while being
       non-interactive. Mirrors QPushButton:disabled above. */
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
        background-color: rgba(255,255,255,0.02);
        color: {TEXT_MUTED};
        border-color: {BORDER};
    }}
    QLineEdit[mono="true"], QSpinBox[mono="true"], QDoubleSpinBox[mono="true"] {{
        font-family: "Geist Mono", "JetBrains Mono", "Consolas", monospace;
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        background-color: transparent;
        border: none;
        width: 14px;
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-bottom: 4px solid {TEXT_MUTED};
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 4px solid {TEXT_MUTED};
    }}
    QComboBox {{
        background-color: {INPUT_BG};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        padding: 6px 10px;
        border-radius: 6px;
        font-size: 12px;
    }}
    QComboBox:focus {{
        border-color: {BORDER_FOCUS};
    }}
    QComboBox::drop-down {{
        border: none;
        background-color: transparent;
        width: 20px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {TEXT_SECONDARY};
    }}
    QComboBox QAbstractItemView {{
        background-color: {BG_MID};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        selection-background-color: {ACCENT};
        selection-color: {ON_ACCENT};
    }}

    QSlider::groove:horizontal {{
        height: 4px;
        background: rgba(255,255,255,0.08);
        border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {ACCENT};
        border-radius: 2px;
    }}
    QSlider::add-page:horizontal {{
        background: rgba(255,255,255,0.08);
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {TEXT_PRIMARY};
        border: 2px solid {ACCENT};
        width: 12px;
        height: 12px;
        margin: -6px 0;
        border-radius: 8px;
    }}
    QSlider::handle:horizontal:hover {{
        border-color: {ACCENT_HOVER};
    }}

    QLabel#duration_value {{
        font-family: "Geist Mono", "JetBrains Mono", "Consolas", monospace;
        font-size: 11px;
        color: {TEXT_PRIMARY};
        background: transparent;
    }}

    QCheckBox {{
        color: {TEXT_SECONDARY};
        spacing: 8px;
        font-size: 12px;
    }}
    QCheckBox::indicator {{
        width: 14px;
        height: 14px;
        background-color: {INPUT_BG};
        border: 1px solid {BORDER};
        border-radius: 3px;
    }}
    QCheckBox::indicator:hover {{
        border-color: {BORDER_LIGHT};
    }}
    QCheckBox::indicator:checked {{
        background-color: {ACCENT};
        border-color: {ACCENT};
    }}
    """


def label_stylesheet():
    return f"""
    QLabel {{
        color: {TEXT_PRIMARY};
        background: transparent;
    }}
    QLabel#section_title {{
        font-size: 11px;
        font-weight: 600;
        color: {TEXT_SECONDARY};
        letter-spacing: 1px;
        background: transparent;
    }}
    QLabel#section_step {{
        font-family: "Geist Mono", "JetBrains Mono", "Consolas", monospace;
        font-size: 10px;
        color: {TEXT_MUTED};
        background: transparent;
        padding-right: 2px;
    }}
    QLabel#field_label {{
        color: {TEXT_SECONDARY};
        font-size: 11px;
        font-weight: 500;
        background: transparent;
    }}
    /* Explicit color above wins over Qt's disabled palette, so a Field wrapped
       in setEnabled(False) needs this to read as greyed out. */
    QLabel#field_label:disabled {{
        color: {TEXT_MUTED};
    }}
    QLabel#field_hint {{
        color: {TEXT_MUTED};
        font-size: 10px;
        background: transparent;
    }}
    QLabel#header_title {{
        font-size: 15px;
        font-weight: 600;
        color: {TEXT_PRIMARY};
        background: transparent;
    }}
    QLabel#header_subtitle {{
        font-size: 11px;
        color: {TEXT_MUTED};
        background: transparent;
    }}
    QLabel#header_status_dot {{
        color: {TEXT_MUTED};
        font-size: 10px;
        background: transparent;
    }}
    QLabel#header_status_text {{
        color: {TEXT_MUTED};
        font-family: "Geist Mono", "JetBrains Mono", "Consolas", monospace;
        font-size: 10px;
        background: transparent;
    }}
    QLabel#timeline_caption {{
        color: {TEXT_MUTED};
        font-size: 11px;
        background: transparent;
    }}
    QLabel#timeline_sync_lbl {{
        color: {TEXT_MUTED};
        font-family: "Geist Mono", "JetBrains Mono", "Consolas", monospace;
        font-size: 10px;
        padding: 4px 2px 0 2px;
        background: transparent;
    }}
    """


def pill_stylesheet():
    return f"""
    QLabel.pill {{
        padding: 2px 8px;
        border-radius: 9px;
        font-size: 10px;
        font-weight: 500;
        background-color: {BG_RAISED};
        color: {TEXT_SECONDARY};
        border: 1px solid {BORDER};
    }}
    QLabel.pill[tone="success"] {{
        background-color: rgba(166,207,123,0.14);
        color: {SUCCESS};
        border-color: rgba(166,207,123,0.32);
    }}
    QLabel.pill[tone="info"] {{
        background-color: rgba(143,182,218,0.14);
        color: {INFO};
        border-color: rgba(143,182,218,0.32);
    }}
    QLabel.pill[tone="danger"] {{
        background-color: {DANGER_SOFT};
        color: {DANGER};
        border-color: {DANGER_BORDER};
    }}
    QLabel.pill[tone="accent"] {{
        background-color: {ACCENT_SOFT};
        color: {ACCENT};
        border-color: {ACCENT_SOFT_BORDER};
    }}
    QLabel.pill[tone="muted"] {{
        background-color: {BG_RAISED};
        color: {TEXT_MUTED};
        border-color: {BORDER};
    }}
    """


def scroll_field_stylesheet():
    return f"""
    QTextEdit, QPlainTextEdit {{
        background-color: {BG_SURFACE};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 6px;
        selection-background-color: {ACCENT};
        selection-color: {ON_ACCENT};
    }}
    QTextEdit:focus, QPlainTextEdit:focus {{
        border-color: {BORDER_FOCUS};
    }}
    """


def scrollbar_stylesheet():
    return f"""
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER_LIGHT};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {TEXT_MUTED};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {BORDER_LIGHT};
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {TEXT_MUTED};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
    """


def groupbox_stylesheet():
    return f"""
    QGroupBox#inner_group {{
        background-color: rgba(255,255,255,0.02);
        border: 1px solid {BORDER};
        border-radius: 8px;
        margin-top: 14px;
        padding: 14px 12px 10px 12px;
        color: {TEXT_SECONDARY};
        font-size: 11px;
        font-weight: 600;
    }}
    QGroupBox#inner_group::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 6px;
        color: {TEXT_SECONDARY};
        background-color: {BG_MID};
        text-transform: uppercase;
        letter-spacing: 1px;
    }}
    QGroupBox#inner_group::indicator {{
        width: 14px;
        height: 14px;
        background-color: {INPUT_BG};
        border: 1px solid {BORDER};
        border-radius: 3px;
    }}
    QGroupBox#inner_group::indicator:hover {{
        border-color: {BORDER_LIGHT};
    }}
    QGroupBox#inner_group::indicator:checked {{
        background-color: {ACCENT};
        border-color: {ACCENT};
    }}
    QGroupBox#inner_group:disabled {{
        color: {TEXT_MUTED};
    }}
    """


def section_frame_stylesheet():
    return f"""
    QFrame#section_frame {{
        background-color: {BG_MID};
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QFrame#section_header {{
        background-color: rgba(255,255,255,0.02);
        border: none;
        border-bottom: 1px solid {BORDER};
        border-top-left-radius: 10px;
        border-top-right-radius: 10px;
    }}
    QFrame#header_icon_chip {{
        background-color: {ACCENT_SOFT};
        border: 1px solid {ACCENT_SOFT_BORDER};
        border-radius: 7px;
        min-width: 30px;
        min-height: 30px;
        max-width: 30px;
        max-height: 30px;
    }}

    /* Section-header glyph badge (26px). Separate from header_icon_chip — that
       is the main-window brand chip and must stay 30px/accent. */
    QFrame#section_icon_chip {{
        border-radius: 7px;
        min-width: 26px;
        min-height: 26px;
        max-width: 26px;
        max-height: 26px;
    }}
    QFrame#section_icon_chip[tone="accent"] {{
        background-color: {ACCENT_SOFT};
        border: 1px solid {ACCENT_SOFT_BORDER};
    }}
    QFrame#section_icon_chip[tone="neutral"] {{
        background-color: rgba(255,255,255,0.05);
        border: 1px solid {BORDER};
    }}

    /* Nested subsection — same palette, smaller metrics. */
    QFrame#section_frame_sub {{
        background-color: {BG_SURFACE};
        border: 1px solid {BORDER};
        border-radius: 8px;
    }}
    QFrame#section_header_sub {{
        background-color: rgba(255,255,255,0.02);
        border: none;
        border-bottom: 1px solid {BORDER};
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
    }}
    QLabel#section_title_sub {{
        font-size: 10px;
        font-weight: 600;
        color: {TEXT_SECONDARY};
        letter-spacing: 1px;
        background: transparent;
    }}
    """


def status_stylesheet():
    return f"""
    QTextEdit#status_field, QPlainTextEdit#status_field {{
        background-color: {CONSOLE_BG};
        color: {TEXT_SECONDARY};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 10px 12px;
        font-family: "Geist Mono", "JetBrains Mono", "Consolas", monospace;
        font-size: 11px;
    }}
    """


def progress_stylesheet():
    return f"""
    QProgressBar#prompt_progress, QProgressBar#total_progress {{
        background-color: {INPUT_BG};
        border: 1px solid {BORDER};
        border-radius: 4px;
        text-align: center;
        color: {TEXT_SECONDARY};
        font-size: 10px;
        min-height: 14px;
        max-height: 14px;
    }}
    QProgressBar#prompt_progress::chunk {{
        background-color: {INFO};
        border-radius: 3px;
    }}
    QProgressBar#total_progress::chunk {{
        background-color: {ACCENT};
        border-radius: 3px;
    }}
    """


def toggle_stylesheet():
    return f"""
    QCheckBox#toggle::indicator {{
        width: 28px;
        height: 16px;
        border-radius: 8px;
        background-color: {INPUT_BG};
        border: 1px solid {BORDER};
    }}
    QCheckBox#toggle::indicator:checked {{
        background-color: {ACCENT};
        border-color: {ACCENT};
    }}
    """


def segment_stylesheet():
    return f"""
    QFrame#seg {{
        background-color: {INPUT_BG};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 2px;
    }}
    QPushButton#seg_btn {{
        background-color: transparent;
        color: {TEXT_SECONDARY};
        border: none;
        padding: 4px 10px;
        min-height: 22px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 500;
    }}
    QPushButton#seg_btn:hover {{
        color: {TEXT_PRIMARY};
    }}
    QPushButton#seg_btn:checked {{
        background-color: {ACCENT};
        color: {ON_ACCENT};
    }}
    """


def icon_grid_stylesheet():
    # Metrics only — IconGrid sets each chip's bg/border/text colour inline
    # (the tint is per constraint type, so it can't live in a shared rule).
    return f"""
    QPushButton#icon_grid_btn {{
        background-color: {INPUT_BG};
        border: 1px solid {BORDER};
        border-radius: 7px;
        color: {TEXT_SECONDARY};
        text-align: left;
        padding: 0 9px;
        min-height: 34px;
        font-size: 11px;
        font-weight: 500;
    }}
    """


def complete_stylesheet():
    return "\n".join([
        window_stylesheet(),
        button_stylesheet(),
        input_stylesheet(),
        label_stylesheet(),
        pill_stylesheet(),
        scroll_field_stylesheet(),
        scrollbar_stylesheet(),
        section_frame_stylesheet(),
        status_stylesheet(),
        progress_stylesheet(),
        toggle_stylesheet(),
        segment_stylesheet(),
        icon_grid_stylesheet(),
    ])
