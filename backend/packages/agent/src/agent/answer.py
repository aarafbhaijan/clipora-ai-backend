"""Answers about the video. Every answer cites transcript line ids; code checks the ids, an LLM verifier
checks support, otherwise the agent says it could not find it (architecture.md 11.4, prd FR-13)."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from agent import prompts
from agent.llm import CallContext, LLMClient, data_block
from agent.project import cue_lines
from core.transcript import Cue, Transcript

NOT_FOUND = "I could not find that in this video."


class Queries(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=3)


class AnswerOut(BaseModel):
    found: bool
    answer: str = Field(max_length=1500)
    cue_ids: list[str] = Field(default_factory=list, max_length=12)


class VerifyOut(BaseModel):
    supported: bool
    reason: str = Field(default="", max_length=300)


@dataclass(frozen=True)
class Answer:
    text: str
    citations: list[dict[str, Any]]


def check_citations(cue_ids: list[str], allowed: set[str], transcript: Transcript) -> list[Cue]:
    """Keep only ids that exist in the transcript and were in the retrieved excerpts, in order."""
    by_id = {c.id: c for c in transcript.cues}
    seen: list[Cue] = []
    for cid in cue_ids:
        if cid in allowed and cid in by_id and by_id[cid] not in seen:
            seen.append(by_id[cid])
    return seen


def excerpt_cues(transcript: Transcript, ranges: list[tuple[str, str]]) -> list[Cue]:
    """All cues covered by the retrieved chunk ranges (cue_from..cue_to), in transcript order."""
    index = {c.id: i for i, c in enumerate(transcript.cues)}
    picked: set[int] = set()
    for a, b in ranges:
        if a in index and b in index:
            picked.update(range(index[a], index[b] + 1))
    return [transcript.cues[i] for i in sorted(picked)]


def answer_question(
    llm: LLMClient,
    ctx: CallContext,
    question: str,
    transcript: Transcript,
    retrieve: Any,
) -> Answer:
    """`retrieve(queries) -> list[rag.Hit]` is injected so tests need no database."""
    q = llm.structured(
        ctx, "rewrite_query", Queries, prompts.REWRITE_QUERY, data_block("user_message", question), tier="small"
    )
    hits = retrieve(q.queries)
    cues = excerpt_cues(transcript, [(h.cue_from, h.cue_to) for h in hits])
    if not cues:
        return Answer(NOT_FOUND, [])
    out = llm.structured(
        ctx,
        "answer_from_video",
        AnswerOut,
        prompts.ANSWER,
        data_block("question", question) + "\n" + data_block("transcript_excerpts", cue_lines(cues)),
    )
    cited = check_citations(out.cue_ids, {c.id for c in cues}, transcript)
    if not out.found or not cited:
        return Answer(NOT_FOUND, [])
    verdict = llm.structured(
        ctx,
        "verify_answer",
        VerifyOut,
        prompts.VERIFY,
        data_block("answer", out.answer) + "\n" + data_block("cited_lines", cue_lines(cited)),
        tier="small",
    )
    if not verdict.supported:
        return Answer(NOT_FOUND, [])
    return Answer(
        out.answer,
        [{"cue_id": c.id, "start_s": c.start, "end_s": c.end, "text": c.text} for c in cited],
    )
