"""Prompt-timeline package — Section 06 of the Animatica tool window.

Ported from ``maya_kimodo/maya_kimodo/gui/{timeline_widget, timeline_container,
prompt_store}.py``. The widget and container are DCC-agnostic Qt; the JSON
store does file IO only. MoBu-specific time bridge lives in
``bridge.time_bridge`` (Phase 4).
"""

from .widget import PromptTimeline
from .container import TimelineContainer

__all__ = ["PromptTimeline", "TimelineContainer"]
