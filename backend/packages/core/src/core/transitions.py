"""Transition catalogue, defined once and used by the preview and the export (architecture.md section 7)."""

from dataclasses import dataclass
from enum import StrEnum


class TransitionType(StrEnum):
    cut = "cut"
    fade = "fade"
    dip_black = "dip_black"
    dip_white = "dip_white"
    dissolve = "dissolve"
    slide_left = "slide_left"
    slide_right = "slide_right"
    wipe_left = "wipe_left"
    wipe_right = "wipe_right"
    circle_open = "circle_open"
    zoom_in = "zoom_in"
    pixelize = "pixelize"


@dataclass(frozen=True)
class TransitionSpec:
    type: TransitionType
    xfade: str | None  # FFmpeg xfade name, None for a cut
    preview: str  # how the browser animates it
    approximate: bool  # preview differs visibly from the export


CATALOGUE: dict[TransitionType, TransitionSpec] = {
    s.type: s
    for s in (
        TransitionSpec(TransitionType.cut, None, "switch", False),
        TransitionSpec(TransitionType.fade, "fade", "opacity", False),
        TransitionSpec(TransitionType.dip_black, "fadeblack", "dip_black", False),
        TransitionSpec(TransitionType.dip_white, "fadewhite", "dip_white", False),
        TransitionSpec(TransitionType.dissolve, "dissolve", "opacity", True),
        TransitionSpec(TransitionType.slide_left, "slideleft", "translate_left", False),
        TransitionSpec(TransitionType.slide_right, "slideright", "translate_right", False),
        TransitionSpec(TransitionType.wipe_left, "wipeleft", "clip_left", False),
        TransitionSpec(TransitionType.wipe_right, "wiperight", "clip_right", False),
        TransitionSpec(TransitionType.circle_open, "circleopen", "clip_circle", True),
        TransitionSpec(TransitionType.zoom_in, "zoomin", "scale_fade", True),
        TransitionSpec(TransitionType.pixelize, "pixelize", "opacity", True),
    )
}

MIN_DURATION_S = 0.1
MAX_DURATION_S = 2.0
