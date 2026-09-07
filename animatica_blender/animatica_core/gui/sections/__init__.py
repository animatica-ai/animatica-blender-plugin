"""Numbered workflow sections for the Animatica tool window.

Each module exposes one ``QWidget`` subclass that wraps a
``CollapsibleSection``. Sections consume ``AppState`` read-only and emit
patches via an ``on_patch(callable)`` callback supplied by the host.
"""

from .settings_section import SettingsSection
from .model_section import ModelSection
from .skeleton_section import SkeletonSection
from .constraints_section import ConstraintsSection
from .import_section import ImportSection
from .capture_section import CaptureSection
from .generate_section import GenerateSection
from .pose_section import PoseSection
from .console_section import ConsoleSection
from .live_section import LiveSection

__all__ = [
    "SettingsSection", "ModelSection",
    "SkeletonSection", "ConstraintsSection", "ImportSection", "CaptureSection",
    "GenerateSection",
    "PoseSection", "ConsoleSection", "LiveSection",
]
