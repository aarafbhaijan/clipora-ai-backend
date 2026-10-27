"""EDL validation. The API runs it on every write, workers again before use (architecture.md section 7)."""

from dataclasses import dataclass

from core.aspect import MAX_SIDE, MIN_SIDE, crop_window
from core.edl import EDL, CropKeyframe
from core.enums import Author, Framing
from core.errors import AppError, ErrorCode
from core.timemap import boundary_durations, output_duration
from core.transitions import MAX_DURATION_S, MIN_DURATION_S, TransitionType

MIN_SEGMENT_S = 1.0
TRANSITION_MARGIN_S = 0.5
MAX_DUPLICATE_OVERLAP_S = 0.25
MIN_SEGMENTS = 1
MAX_SEGMENTS = 30
USER_MIN_OUTPUT_S = 3.0
USER_MAX_OUTPUT_S = 180.0
AGENT_TARGET_TOLERANCE = 0.10
_EPS = 1e-6


@dataclass(frozen=True)
class Issue:
    path: str
    message: str


def find_issues(edl: EDL, author: Author) -> list[Issue]:
    issues: list[Issue] = []
    segs = edl.segments

    def add(path: str, msg: str) -> None:
        issues.append(Issue(path, msg))

    if not MIN_SEGMENTS <= len(segs) <= MAX_SEGMENTS:
        add("segments", f"needs {MIN_SEGMENTS} to {MAX_SEGMENTS} segments, has {len(segs)}")

    ids = [s.id for s in segs]
    if len(set(ids)) != len(ids):
        add("segments", "segment ids must be unique")

    out = edl.output
    for name, value in (("width", out.width), ("height", out.height)):
        if value % 2 or not MIN_SIDE <= value <= MAX_SIDE:
            add(f"output.{name}", f"must be even and {MIN_SIDE} to {MAX_SIDE}, is {value}")

    for t in [edl.transitions.default, *edl.transitions.overrides]:
        if t.type != TransitionType.cut and not MIN_DURATION_S <= t.duration_s <= MAX_DURATION_S:
            add("transitions", f"duration must be {MIN_DURATION_S} to {MAX_DURATION_S} s, is {t.duration_s}")
    adjacent = {(segs[i].id, segs[i + 1].id) for i in range(len(segs) - 1)}
    for i, o in enumerate(edl.transitions.overrides):
        if (o.from_, o.to) not in adjacent:
            add(f"transitions.overrides[{i}]", f"{o.from_} -> {o.to} are not adjacent segments")

    durations = boundary_durations(edl) if len(segs) > 1 else []
    w0, h0 = crop_window(edl.source.width, edl.source.height, out.width, out.height)
    for i, s in enumerate(segs):
        p = f"segments[{i}]"
        if not 0 <= s.src_start < s.src_end <= edl.source.duration_s + _EPS:
            add(p, f"range {s.src_start}-{s.src_end} is outside 0-{edl.source.duration_s}")
            continue
        d_in = durations[i - 1] if i > 0 else 0.0
        d_out = durations[i] if i < len(durations) else 0.0
        if s.length < MIN_SEGMENT_S - _EPS:
            add(p, f"must be at least {MIN_SEGMENT_S} s long")
        elif s.length < d_in + d_out + TRANSITION_MARGIN_S - _EPS:
            add(p, f"must be at least {TRANSITION_MARGIN_S} s longer than its transitions")
        if out.framing == Framing.crop or s.crop:
            issues.extend(_crop_issues(p, s.src_start, s.src_end, s.crop, w0, h0))

    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            a, b = segs[i], segs[j]
            overlap = min(a.src_end, b.src_end) - max(a.src_start, b.src_start)
            if overlap > MAX_DUPLICATE_OVERLAP_S + _EPS:
                add(f"segments[{j}]", f"repeats {overlap:.2f} s of {a.id}")

    if segs and not issues:
        total = output_duration(edl)
        if author == Author.agent and out.target_duration_s:
            lo = out.target_duration_s * (1 - AGENT_TARGET_TOLERANCE)
            hi = out.target_duration_s * (1 + AGENT_TARGET_TOLERANCE)
            if not lo - _EPS <= total <= hi + _EPS:
                add("output", f"length {total:.1f} s is not within 10% of target {out.target_duration_s} s")
        elif author == Author.user and not USER_MIN_OUTPUT_S - _EPS <= total <= USER_MAX_OUTPUT_S + _EPS:
            add("output", f"length {total:.1f} s must be {USER_MIN_OUTPUT_S} to {USER_MAX_OUTPUT_S} s")
    return issues


def _crop_issues(p: str, start: float, end: float, crop: list[CropKeyframe], w0: float, h0: float) -> list[Issue]:
    if not crop:
        return [Issue(f"{p}.crop", "needs crop keyframes")]
    issues: list[Issue] = []
    times = [k.t for k in crop]
    if times != sorted(times):
        issues.append(Issue(f"{p}.crop", "keyframes must be sorted by t"))
    if times[0] > start + _EPS or times[-1] < end - _EPS:
        issues.append(Issue(f"{p}.crop", "keyframes must cover the whole segment"))
    for k in crop:
        w, h = w0 / k.zoom, h0 / k.zoom
        if k.cx - w / 2 < -_EPS or k.cx + w / 2 > 1 + _EPS or k.cy - h / 2 < -_EPS or k.cy + h / 2 > 1 + _EPS:
            issues.append(Issue(f"{p}.crop", f"window at t={k.t} leaves the frame"))
            break
    return issues


def validate(edl: EDL, author: Author) -> EDL:
    issues = find_issues(edl, author)
    if issues:
        raise AppError(
            ErrorCode.edl_invalid,
            "The plan is not valid.",
            {"issues": [{"path": i.path, "message": i.message} for i in issues]},
        )
    return edl
