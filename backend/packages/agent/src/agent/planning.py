"""Candidates, selection and plan assembly. The LLM picks moments by transcript line ids; code turns
them into times, fits the target length and builds a valid plan (architecture.md 11.1)."""

import uuid
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agent.intent import Intent, clamp_target
from agent.snapping import cue_range
from core.aspect import output_size
from core.crop import Detections, crop_path
from core.edl import EDL, Candidate, Meta, Output, RunInfo, Segment, Source, Transition, Transitions
from core.enums import Author, Framing
from core.transcript import Transcript, snap
from core.transitions import TransitionType
from core.validation import AGENT_TARGET_TOLERANCE, MAX_DUPLICATE_OVERLAP_S, MIN_SEGMENT_S, find_issues

MAX_CANDIDATES = 15
MAX_SEGMENT_S = 60.0


class CandidateOut(BaseModel):
    cue_from: str
    cue_to: str
    reason: str = Field(max_length=200)
    score: float = Field(ge=0, le=1)


class CandidatesOut(BaseModel):
    candidates: list[CandidateOut] = Field(max_length=MAX_CANDIDATES)


class SelectionOut(BaseModel):
    order: list[int] = Field(description="Candidate numbers in playing order")
    note: str = Field(default="", max_length=300)


class CriticOut(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Moment:
    start: float
    end: float
    reason: str
    score: float

    @property
    def length(self) -> float:
        return self.end - self.start


STYLE_TRANSITIONS: dict[str, Transition] = {
    "punchy": Transition(type=TransitionType.cut, duration_s=0.4),
    "energetic": Transition(type=TransitionType.cut, duration_s=0.4),
    "calm": Transition(type=TransitionType.fade, duration_s=0.5),
    "story": Transition(type=TransitionType.dissolve, duration_s=0.5),
    "educational": Transition(type=TransitionType.fade, duration_s=0.4),
    "cinematic": Transition(type=TransitionType.dip_black, duration_s=0.6),
}


def transition_for(style: str | None) -> Transition:
    return STYLE_TRANSITIONS.get((style or "").strip().lower(), Transition(type=TransitionType.fade, duration_s=0.4))


def _overlap(a: Moment, b: Moment) -> float:
    return min(a.end, b.end) - max(a.start, b.start)


def resolve_moments(transcript: Transcript, outs: list[CandidateOut], intent: Intent) -> list[Moment]:
    """Cue ids to times. Unknown ids, too short or too long ranges are dropped; overlaps keep the best."""
    raw: list[Moment] = []
    for a in intent.anchors:
        r = cue_range(transcript, a.cue_from, a.cue_to)
        if r is not None:
            raw.append(Moment(r.start, r.end, "From the idea you picked", 1.0))
    for c in outs:
        r = cue_range(transcript, c.cue_from, c.cue_to)
        if r is not None:
            raw.append(Moment(r.start, r.end, c.reason, c.score))
    kept: list[Moment] = []
    for m in sorted(raw, key=lambda m: m.score, reverse=True):
        if not MIN_SEGMENT_S <= m.length <= MAX_SEGMENT_S:
            continue
        if any(_overlap(m, k) > MAX_DUPLICATE_OVERLAP_S for k in kept):
            continue
        kept.append(m)
    return kept[:MAX_CANDIDATES]


def total_length(moments: list[Moment], d: float) -> float:
    return sum(m.length for m in moments) - d * max(0, len(moments) - 1)


def fit_to_target(
    chosen: list[Moment], pool: list[Moment], target: float, d: float, boundaries: list[float]
) -> list[Moment]:
    """Make the selection land within 10% of the target: drop the weakest, add the best unused,
    then trim the last clip at a word boundary."""
    lo, hi = target * (1 - AGENT_TARGET_TOLERANCE), target * (1 + AGENT_TARGET_TOLERANCE)
    out = list(chosen) or sorted(pool, key=lambda m: m.score, reverse=True)[:1]
    while total_length(out, d) > hi and len(out) > 1:
        weakest = min(out[1:], key=lambda m: m.score)  # keep the hook
        out.remove(weakest)
    unused = sorted((m for m in pool if m not in out), key=lambda m: m.score, reverse=True)
    for m in unused:
        if total_length(out, d) >= lo:
            break
        if all(_overlap(m, k) <= MAX_DUPLICATE_OVERLAP_S for k in out):
            out.append(m)
    excess = total_length(out, d) - target
    if excess > target * AGENT_TARGET_TOLERANCE:
        last = out[-1]
        want_end = last.end - excess
        new_end = snap(want_end, [b for b in boundaries if last.start + MIN_SEGMENT_S + 2 * d < b <= last.end], 3.0)
        if new_end is not None:
            out[-1] = Moment(last.start, new_end, last.reason, last.score)
    return out


def assemble(
    *,
    moments: list[Moment],
    pool: list[Moment],
    intent: Intent,
    source: Source,
    detections: Detections | None,
    model_id: str,
    prompt_version: str,
    short: dict[str, str] | None = None,
) -> EDL:
    """Build a plan. `short` ({id, title}) names the generated video; revisions pass the existing one on."""
    target = clamp_target(intent.target_duration_s, source.duration_s)
    aspect = intent.aspect or "9:16"
    try:
        width, height = output_size(aspect)
    except ValueError:
        aspect, (width, height) = "9:16", output_size("9:16")
    segments = [
        Segment(id=f"s{i}", src_start=m.start, src_end=m.end, reason=m.reason, score=round(m.score, 3))
        for i, m in enumerate(moments, start=1)
    ]
    segments = crop_path(detections, segments, source.width, source.height, width, height)
    unused = [m for m in pool if m not in moments]
    return EDL(
        version=0,
        source=source,
        output=Output(
            aspect=aspect,
            width=width,
            height=height,
            framing=Framing(intent.framing) if intent.framing else Framing.crop,
            target_duration_s=target,
        ),
        segments=segments,
        transitions=Transitions(default=transition_for(intent.style)),
        meta=Meta(
            candidates=[Candidate(src_start=m.start, src_end=m.end, score=m.score, reason=m.reason) for m in unused],
            run=RunInfo(prompt_version=prompt_version, model=model_id),
            **({"short": short} if short else {}),
        ),
    )


def new_short(title: str | None) -> dict[str, str]:
    """Identity of a freshly generated video. Kept in meta through revisions and user edits."""
    text = " ".join((title or "").split())
    return {"id": uuid.uuid4().hex[:10], "title": (text[:57] + "…") if len(text) > 58 else (text or "Short video")}


def plan_issues(edl: EDL) -> list[str]:
    return [f"{i.path}: {i.message}" for i in find_issues(edl, Author.agent)]


def moments_from_edl(edl: EDL) -> tuple[list[Moment], list[Moment]]:
    """Current clips and saved candidates, for re-running selection after set_target."""
    current = [Moment(s.src_start, s.src_end, s.reason or "", s.score or 0.5) for s in edl.segments]
    extra = [Moment(c.src_start, c.src_end, c.reason or "", c.score or 0.4) for c in edl.meta.candidates]
    return current, current + extra
