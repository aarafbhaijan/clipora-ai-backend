"""Speaker-following crop path (architecture.md sections 12 and 12.1).

Pure functions on plain data, no numpy, so the API can recompute crops without importing vision.
YOLO detection lives in `backend/packages/vision`; this file only turns its detections into keyframes.
The frame follows whoever is talking (`Detections.turns`, from lip movement) and cuts when the talker changes;
before anyone talks, or when lip data is missing, it follows the main person (`main_track`).
Filter values are starting points: tune them on real videos.
"""

import math
from collections import defaultdict
from statistics import median

from pydantic import BaseModel

from core.aspect import crop_window
from core.edl import CropKeyframe, Segment
from core.speakers import SpeakerTurn

HOLD_S = 1.0
MEDIAN_WINDOW = 5
EMA_ALPHA = 0.2
DEAD_ZONE = 0.04
MAX_SPEED = 0.15  # fraction of the frame per second
KEYFRAME_STEP_S = 0.5
CUT_MIN_S = 1.5  # a turn shorter than this doesn't move the frame (a quick "yeah" from the other person)
CUT_GAP_S = 0.02  # the frame jumps to the new talker within one frame


class Box(BaseModel):
    """One tracked person box, coordinates as fractions of the frame."""

    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


class DetectionFrame(BaseModel):
    t: float
    boxes: list[Box]


class Detections(BaseModel):
    """Contents of `detections.json`, written by ingest stage 6."""

    fps: float
    frames: list[DetectionFrame]
    turns: list[SpeakerTurn] = []  # who talks when; added by the ingest "speakers" stage, empty before it


def track_sizes(detections: Detections) -> dict[int, float]:
    """Each track's mean box area (fraction of the frame)."""
    total: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    for f in detections.frames:
        for b in f.boxes:
            total[b.track_id] += b.area
            count[b.track_id] += 1
    return {k: total[k] / count[k] for k in total}


def main_track(detections: Detections, segments: list[Segment]) -> int | None:
    """Track with the largest sum of box area times confidence inside the selected segments."""
    score: dict[int, float] = defaultdict(float)
    for f in detections.frames:
        if any(s.src_start <= f.t <= s.src_end for s in segments):
            for b in f.boxes:
                score[b.track_id] += b.area * b.conf
    return max(score, key=lambda k: score[k]) if score else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _centre(v: float, half: float) -> float:
    """Window centre rounded to 4 decimals without leaving the frame: plain rounding of an edge position
    (0.28125 for a square window on 16:9) would move the window 0.00005 outside it."""
    lo = math.ceil(half * 10_000 - 1e-9) / 10_000
    hi = math.floor((1 - half) * 10_000 + 1e-9) / 10_000
    return 0.5 if lo > hi else _clamp(round(v, 4), lo, hi)


def _raw_series(frames: list[DetectionFrame], track: int | None, axis: str) -> list[tuple[float, float]]:
    """Centre of the track on one axis per frame; hold a lost track up to 1 s, then the frame centre."""
    series: list[tuple[float, float]] = []
    last_seen: tuple[float, float] | None = None
    for f in frames:
        box = next((b for b in f.boxes if b.track_id == track), None)
        if box is not None:
            v = (box.x1 + box.x2) / 2 if axis == "x" else (box.y1 + box.y2) / 2
            last_seen = (f.t, v)
            series.append((f.t, v))
        elif last_seen is not None and f.t - last_seen[0] <= HOLD_S:
            series.append((f.t, last_seen[1]))
        else:
            series.append((f.t, 0.5))
    return series


def _smooth(series: list[tuple[float, float]], half_window: float) -> list[tuple[float, float]]:
    """Median over 5 samples, EMA alpha 0.2, then dead zone and speed limit, clamped inside the frame."""
    if not series:
        return series
    values = [v for _, v in series]
    k = MEDIAN_WINDOW // 2
    med = [median(values[max(0, i - k) : i + k + 1]) for i in range(len(values))]
    ema: list[float] = []
    for v in med:
        ema.append(v if not ema else EMA_ALPHA * v + (1 - EMA_ALPHA) * ema[-1])
    lo, hi = half_window, 1 - half_window
    pos = _clamp(ema[0], lo, hi)
    out = [(series[0][0], pos)]
    for i in range(1, len(series)):
        dt = series[i][0] - series[i - 1][0]
        target = _clamp(ema[i], lo, hi)
        delta = target - pos
        if abs(delta) > DEAD_ZONE:
            step = MAX_SPEED * dt
            pos += _clamp(delta, -step, step)
        out.append((series[i][0], _clamp(pos, lo, hi)))
    return out


def _interp(series: list[tuple[float, float]], t: float, fallback: float) -> float:
    if not series:
        return fallback
    if t <= series[0][0]:
        return series[0][1]
    for (t0, v0), (t1, v1) in zip(series, series[1:], strict=False):
        if t0 <= t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return series[-1][1]


def _sample_times(start: float, end: float) -> list[float]:
    times = [start]
    t = start + KEYFRAME_STEP_S
    while t < end - 1e-6:
        times.append(round(t, 3))
        t += KEYFRAME_STEP_S
    times.append(end)
    return times


def focus_spans(
    detections: Detections | None, segment: Segment, track: int | None
) -> list[tuple[float, float, int | None]]:
    """(start, end, track) ranges of the segment and who the frame follows in each: the talker of each turn long
    enough to cut to, holding on them until the next talker; the main person before the first turn."""
    spans: list[tuple[float, float, int | None]] = []
    t, who = segment.src_start, track
    for turn in detections.turns if detections else []:
        if turn.end - turn.start < CUT_MIN_S or turn.track_id == who:
            continue
        if turn.end <= segment.src_start:
            who = turn.track_id  # talking before the clip starts: still on them at the start
            continue
        if turn.start >= segment.src_end:
            break
        cut = max(turn.start, segment.src_start)
        if cut > t:
            spans.append((t, cut, who))
        t, who = cut, turn.track_id
    spans.append((t, segment.src_end, who))
    return spans


def segment_crop(
    detections: Detections | None,
    segment: Segment,
    track: int | None,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> list[CropKeyframe]:
    """Keyframes for one segment in source time. Smoothing restarts at each segment and at each cut."""
    w0, h0 = crop_window(src_w, src_h, out_w, out_h)
    spans = focus_spans(detections, segment, track)
    keyframes: list[CropKeyframe] = []
    for i, (start, end, who) in enumerate(spans):
        frames = [f for f in detections.frames if start - HOLD_S <= f.t <= end] if detections else []
        sx = _smooth(_raw_series(frames, who, "x"), w0 / 2) if w0 < 1 else []
        sy = _smooth(_raw_series(frames, who, "y"), h0 / 2) if h0 < 1 else []
        last = i == len(spans) - 1
        times = _sample_times(start, end if last else max(start, end - CUT_GAP_S))
        keyframes.extend(
            CropKeyframe(
                t=t,
                cx=_centre(_interp(sx, t, 0.5), w0 / 2),
                cy=_centre(_interp(sy, t, 0.5), h0 / 2),
                zoom=1.0,
            )
            for t in times
        )
    return keyframes


def crop_path(
    detections: Detections | None,
    segments: list[Segment],
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> list[Segment]:
    """Return the segments with fresh crop keyframes. Without detections the window stays centred."""
    track = main_track(detections, segments) if detections else None
    return [
        s.model_copy(update={"crop": segment_crop(detections, s, track, src_w, src_h, out_w, out_h)}) for s in segments
    ]
