"""Ideas after ingest (architecture.md 11.2). One structured call; code resolves anchors to times."""

from typing import Any

from pydantic import BaseModel, Field

from agent import prompts
from agent.intent import CueAnchor, clamp_target
from agent.llm import CallContext, LLMClient, data_block
from agent.project import cue_lines
from agent.snapping import cue_range
from core.transcript import Transcript

MIN_IDEAS = 3
MAX_IDEAS = 6


class IdeaOut(BaseModel):
    title: str = Field(max_length=80)
    why: str = Field(max_length=200)
    format: str = Field(max_length=40, description="e.g. hook + payoff, list, story, quote")
    target_duration_s: float
    aspect: str
    style: str = Field(max_length=30)
    focus: str = Field(max_length=120)
    anchors: list[CueAnchor] = Field(max_length=6)


class SuggestOut(BaseModel):
    summary: str = Field(max_length=600)
    topics: list[str] = Field(max_length=6)
    ideas: list[IdeaOut] = Field(max_length=8)


ASPECTS = {"9:16", "1:1", "4:5", "16:9"}

GENERIC_IDEAS: list[dict[str, Any]] = [
    {
        "title": "Best highlights",
        "why": "The strongest moments in one short.",
        "format": "highlights",
        "target_duration_s": 60,
        "aspect": "9:16",
        "style": "punchy",
        "focus": "the most engaging moments",
    },
    {
        "title": "Quick hook",
        "why": "One strong idea, fast.",
        "format": "hook + payoff",
        "target_duration_s": 30,
        "aspect": "9:16",
        "style": "punchy",
        "focus": "the single strongest point",
    },
    {
        "title": "Square recap for feeds",
        "why": "A calm summary that works in a feed.",
        "format": "recap",
        "target_duration_s": 60,
        "aspect": "1:1",
        "style": "calm",
        "focus": "the main takeaway",
    },
]


def resolve_ideas(
    out: SuggestOut, transcript: Transcript, existing_titles: set[str] | None = None
) -> list[dict[str, Any]]:
    """Keep ideas whose anchors exist; resolve anchors to times; top up with generic ideas below 3."""
    source_s = transcript.cues[-1].end if transcript.cues else None
    taken = {t.lower() for t in existing_titles or set()}
    ideas: list[dict[str, Any]] = []
    for idea in out.ideas:
        if idea.title.lower() in taken:
            continue
        anchors = []
        for a in idea.anchors:
            r = cue_range(transcript, a.cue_from, a.cue_to)
            if r is not None:
                anchors.append({"cue_from": a.cue_from, "cue_to": a.cue_to, "start_s": r.start, "end_s": r.end})
        if not anchors:
            continue
        ideas.append(
            {
                "title": idea.title,
                "why": idea.why,
                "format": idea.format,
                "target_duration_s": clamp_target(idea.target_duration_s, source_s),
                "aspect": idea.aspect if idea.aspect in ASPECTS else "9:16",
                "style": idea.style,
                "focus": idea.focus,
                "anchors": anchors,
            }
        )
        taken.add(idea.title.lower())
        if len(ideas) == MAX_IDEAS:
            break
    for generic in GENERIC_IDEAS:
        if len(ideas) >= MIN_IDEAS:
            break
        if generic["title"].lower() not in taken:
            ideas.append(
                {**generic, "target_duration_s": clamp_target(generic["target_duration_s"], source_s), "anchors": []}
            )
    offset = len(existing_titles or ())
    return [{"id": f"i{offset + n}", **idea} for n, idea in enumerate(ideas, start=1)]


def suggest(
    llm: LLMClient, ctx: CallContext, transcript: Transcript, avoid_titles: list[str]
) -> tuple[SuggestOut, str]:
    parts = [data_block("transcript", cue_lines(transcript.cues))]
    if avoid_titles:
        parts.append(data_block("ideas_already_shown", "\n".join(avoid_titles)) + "\nPropose different ideas.")
    out = llm.structured(ctx, "suggest", SuggestOut, prompts.SUGGEST, "\n".join(parts), temperature=0.2)
    return out, prompts.SUGGEST_VERSION
