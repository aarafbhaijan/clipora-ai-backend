"""What the user wants this turn. Parsed by the LLM, or built by code from a chosen idea."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from core.enums import MemoryKind

DEFAULT_TARGET_S = 60.0
AGENT_MIN_S, AGENT_MAX_S = 30.0, 90.0


class CueAnchor(BaseModel):
    cue_from: str
    cue_to: str


class Intent(BaseModel):
    """parse_intent output (architecture.md 11.1)."""

    kind: Literal["plan", "question", "correction", "metadata"]
    target_duration_s: float | None = Field(default=None, description="Wanted length of the short in seconds")
    aspect: str | None = Field(default=None, description="Aspect like 9:16, 1:1, 16:9, or null")
    framing: Literal["crop", "fit"] | None = None
    style: str | None = Field(default=None, description="Short style word: punchy, calm, story, educational")
    focus: str | None = Field(default=None, description="Topic to focus on, in the user's words")
    targets: list[str] = Field(default_factory=list, description="Clip ids the message refers to, like s2")
    question: str | None = Field(default=None, description="Only if you cannot proceed: one short question")
    choices: list[str] = Field(default_factory=list, max_length=4, description="2-4 tappable answers")
    standing_instruction: str | None = Field(
        default=None, description="If the user states a lasting preference ('always...'), restate it in one sentence"
    )
    standing_kind: MemoryKind | None = None
    anchors: list[CueAnchor] = Field(default_factory=list, description="Only set by code from an idea")


SOURCE_SHARE = 0.9


def clamp_target(seconds: float | None, source_s: float | None = None) -> float:
    """Agent plans are 30 to 90 s (prd.md section 5), and never longer than the video allows.

    A source shorter than the range caps the target at 90% of its length, so a plan can still be built.
    """
    target = DEFAULT_TARGET_S if seconds is None else max(AGENT_MIN_S, min(AGENT_MAX_S, float(seconds)))
    if source_s and source_s > 0:
        target = min(target, round(source_s * SOURCE_SHARE, 1))
    return target


def intent_from_suggestion(idea: dict[str, Any]) -> Intent:
    """A chosen idea is already a plan request: no LLM call."""
    anchors = [
        CueAnchor(cue_from=str(a["cue_from"]), cue_to=str(a["cue_to"]))
        for a in idea.get("anchors", [])
        if isinstance(a, dict) and a.get("cue_from") and a.get("cue_to")
    ]
    target = idea.get("target_duration_s")
    return Intent(
        kind="plan",
        target_duration_s=float(target) if isinstance(target, int | float) else None,
        aspect=idea.get("aspect") or None,
        style=idea.get("style") or None,
        focus=idea.get("focus") or idea.get("title") or None,
        anchors=anchors,
    )
