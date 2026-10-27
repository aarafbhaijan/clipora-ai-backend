"""Recompute crop keyframes only where they changed (same rule as the API's PATCH /edl)."""

from core.crop import Detections, crop_path
from core.edl import EDL


def refresh_crops(new: EDL, old: EDL | None, detections: Detections | None) -> EDL:
    """New keyframes for new segments and changed ranges, or for all when the output size changed."""
    size_changed = old is None or (old.output.width, old.output.height) != (new.output.width, new.output.height)
    old_ranges = {s.id: (s.src_start, s.src_end) for s in old.segments} if old else {}
    changed = [
        s for s in new.segments if size_changed or not s.crop or old_ranges.get(s.id) != (s.src_start, s.src_end)
    ]
    if not changed:
        return new
    fresh = {
        s.id: s
        for s in crop_path(
            detections, changed, new.source.width, new.source.height, new.output.width, new.output.height
        )
    }
    return new.model_copy(update={"segments": [fresh.get(s.id, s) for s in new.segments]})
