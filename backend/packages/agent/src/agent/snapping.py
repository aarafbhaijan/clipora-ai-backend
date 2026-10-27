"""Code snaps timestamps. LLM times are hints (rules.md hard rule 4)."""

import re
from dataclasses import dataclass

from core.transcript import Cue, Transcript, snap

SNAP_WITHIN_S = 0.5


@dataclass(frozen=True)
class Snapped:
    start: float
    end: float


def snap_range(start: float, end: float, boundaries: list[float], within: float = SNAP_WITHIN_S) -> Snapped | None:
    """Snap both ends to the nearest word boundary within 0.5 s. None if either end does not snap."""
    s = snap(start, boundaries, within)
    e = snap(end, boundaries, within)
    if s is None or e is None or e <= s:
        return None
    return Snapped(s, e)


_TIME = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:[.,]\d+)?)$|^(\d+(?:\.\d+)?)s?$")


def _seconds(ref: str) -> float | None:
    """'01:02.5', '1:01:02', '62.5' or '62.5s' as seconds; None if it is not a time."""
    m = _TIME.match(ref)
    if not m:
        return None
    if m.group(4) is not None:
        return float(m.group(4))
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + float(m.group(3).replace(",", "."))


def _cue_for(transcript: Transcript, by_id: dict[str, Cue], ref: str, edge: str) -> Cue | None:
    """A cue by id ('c12', also '12' or 'C12'), or by a time that lands within 0.5 s of its start or end.

    Small models often answer with times instead of ids. A time is still only a hint: it must snap to a
    cue boundary, else the range is rejected (rules.md hard rule 4).
    """
    ref = ref.strip()
    norm = ref.lower().replace(" ", "")
    if norm.isdigit():
        norm = f"c{norm}"
    if norm in by_id:
        return by_id[norm]
    t = _seconds(ref)
    if t is None or not transcript.cues:
        return None
    if edge == "start":
        best = min(transcript.cues, key=lambda c: abs(c.start - t))
        return best if abs(best.start - t) <= SNAP_WITHIN_S else None
    best = min(transcript.cues, key=lambda c: abs(c.end - t))
    return best if abs(best.end - t) <= SNAP_WITHIN_S else None


def cue_range(transcript: Transcript, cue_from: str, cue_to: str) -> Snapped | None:
    """Times of a cue range by id (or by times that snap to cue edges). None if it does not resolve."""
    by_id: dict[str, Cue] = {c.id: c for c in transcript.cues}
    a = _cue_for(transcript, by_id, cue_from, "start")
    b = _cue_for(transcript, by_id, cue_to, "end")
    if a is None or b is None or b.end <= a.start:
        return None
    return Snapped(a.start, b.end)
