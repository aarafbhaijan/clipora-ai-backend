"""One chat turn as a stream of Server-Sent Events.

    status  {"stage"}                          routing has started
    route   {"intent", "message", "job"?}      the user's message is saved; "job" when it became an edit job
    token   {"text"}                           a piece of the streamed answer
    done    {"message"}                        the saved assistant message (content without citation markers)
    error   {"code", "message"}                the answer failed; a short assistant message is saved instead

Edit requests are handed to the job queue exactly like POST /api/projects/{id}/messages, so the edit graph and
its progress are unchanged. Every database write happens in its own short transaction, never across the stream.
"""

import json
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select

from agent.config import agent_config
from agent.conversation import (
    HISTORY_MESSAGE_CHARS,
    Intent,
    build_messages,
    citations,
    clean,
    route,
    transcript_context,
)
from agent.llm import CallContext, LLMClient
from agent.project import load_project, load_transcript, plan_summary
from api.access import own_project
from api.auth import CurrentUser
from api.schemas import SpeakerOut, SpeakerTurnOut
from api.services.conversation import post_user_message
from api.services.plans import current_version_row
from api.services.speaker import speaker_for
from api.views import job_out, message_out
from core.edl import EDL
from core.enums import MessageKind, MessageRole, Thread
from core.transcript import Transcript
from db.ids import uuid7
from db.models import Message
from db.session import session_scope

log = logging.getLogger("chat.turn")

RATE_LIMITED_TEXT = "Clipora's AI has hit its usage limit for now, so I couldn't answer. Please try again later."
TOO_LARGE_TEXT = (
    "This question needs more of the transcript than the AI plan allows in one request. Try asking about one part "
    "of the video, or open the **Transcript** tab to read all of it."
)
FAILED_TEXT = "Sorry, I couldn't answer that just now. Please try again."
HISTORY_MESSAGES = 12

# A provider's per-request token cap (Groq's free tier: 8,000 tokens a minute, and one request can't exceed it).
# Learned from its 413 reply and kept for the life of the process, so later questions fit at the first try.
CHARS_PER_TOKEN = 2.8  # transcript lines with ids and timestamps, measured with gpt-oss's tokenizer
PROMPT_TOKENS = 1500  # instructions, short history, plan and question, besides the transcript
LIMITED_HISTORY_CHARS = 500
_request_limit: int | None = None


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@dataclass(frozen=True)
class TurnInput:
    user: CurrentUser
    project_id: uuid.UUID
    text: str
    waiting_for_answer: bool  # the edit graph asked a question; the reply belongs to it


def max_transcript_chars() -> int:
    """How much transcript fits the chat model: Ollama's context is small; Bedrock and Groq models take far more."""
    cfg = agent_config()
    if cfg.provider_for("chat") == "ollama":
        return max(4_000, cfg.ollama_num_ctx * 3 - 4_000)
    if _request_limit is not None:
        return max(4_000, int((_request_limit * 0.9 - PROMPT_TOKENS) * CHARS_PER_TOKEN))
    return 200_000


def request_limit(e: Exception) -> int | None:
    """The token cap named in a provider's "request too large" (HTTP 413) reply, if this is one."""
    if getattr(e, "status_code", None) != 413:
        return None
    m = re.search(r"\bLimit (\d+)", str(e))
    return int(m[1]) if m else None


def _save(project_id: uuid.UUID, role: str, content: str, payload: dict[str, Any], edl_version: int | None) -> Message:
    with session_scope() as tx:
        m = Message(
            id=uuid7(),
            project_id=project_id,
            thread=Thread.edit.value,
            role=role,
            kind=MessageKind.text.value,
            content=content,
            payload=payload,
            edl_version=edl_version,
        )
        tx.add(m)
        tx.flush()
        tx.refresh(m)
        return m


def _history(project_id: uuid.UUID) -> list[tuple[str, str]]:
    with session_scope() as tx:
        rows = tx.execute(
            select(Message)
            .where(Message.project_id == project_id, Message.thread == Thread.edit.value, Message.kind == "text")
            .order_by(Message.created_at.desc())
            .limit(HISTORY_MESSAGES)
        ).scalars()
        return [(m.role, m.content) for m in reversed(list(rows)) if m.content]


_AT_TIME = re.compile(
    r"\b(?:at|around|@)\s*(\d{1,2}):(\d{2})\b|\b(?:at|around|@)\s*(\d+)\s*(?:s|sec|secs|seconds)\b", re.I
)
MAX_TURN_ROWS = 12


def _clock(t: float) -> str:
    m, sec = divmod(int(t), 60)
    return f"{m}:{sec:02d}"


def asked_time(text: str) -> float | None:
    """ "who is speaking at 1:20" -> 80.0; None when the question names no moment."""
    m = _AT_TIME.search(text)
    if not m:
        return None
    return int(m[1]) * 60 + int(m[2]) if m[1] else float(m[3])


def turns_table(spk: SpeakerOut) -> str:
    rows = [f"| {_clock(t.start)}–{_clock(t.end)} | {t.label} |" for t in spk.turns[:MAX_TURN_ROWS]]
    more = len(spk.turns) - MAX_TURN_ROWS
    tail = f"\n\n…and {more} more turns: they are on the timeline's **Speakers** lane." if more > 0 else ""
    return "| When | Who is talking |\n|---|---|\n" + "\n".join(rows) + tail


def speaker_reply(spk: SpeakerOut, question: str, has_plan: bool) -> str:
    """What Clipora says about who is speaking. Markdown; facts come from the detections only."""
    if not spk.available:
        return (
            "I don't have speaker detection for this video yet. It is made while the video is processed, so try "
            "again once processing has finished, or upload the video again."
        )
    if spk.main_track_id is None or spk.coverage == 0:
        return (
            "I couldn't find a person in this video, so there is no speaker box to show. A portrait export keeps "
            "the frame centred instead."
        )
    at = asked_time(question)
    if at is not None and spk.analysed:
        turn = next((t for t in spk.turns if t.start <= at <= t.end), None)
        if turn is None:
            answer = (
                f"At **{_clock(at)}** I can't see anyone on screen whose lips match the voice: it may be a pause, "
                "a person off camera, or a face too small or turned away to read."
            )
        else:
            answer = (
                f"At **{_clock(at)}**, **{turn.label}** is talking ({_clock(turn.start)}–{_clock(turn.end)}). "
                "They have the **green box** on the video."
            )
        return answer + "\n\nThe boxes are only shown here, never in the exported video."
    if spk.turns:
        talkers = len({t.track_id for t in spk.turns})
        intro = (
            f"**Speakers found.** I matched each person's lip movement to the voice: **{talkers}** "
            f"{'person talks' if talkers == 1 else 'people talk'} on screen. The **green box** is on whoever is "
            "talking at that moment, the others are **red**.\n\n"
        )
        return (
            intro + turns_table(spk) + "\n\nWhen you export in portrait (or any other shape), the frame cuts to the "
            "person talking. The boxes are only shown here, never in the exported video. Use **Speaker** above the "
            "video to hide or show them."
        )
    where = "in your clips" if has_plan else "in the video"
    others = ""
    if spk.people > 1:
        who = "person was" if spk.people == 2 else "people were"
        others = f" {spk.people - 1} other {who} also detected (shown in **red**)."
    note = (
        "I couldn't match anyone's lips to the voice (faces may be too small or turned away), so the frame stays on "
        "the main person.\n\n"
        if spk.analysed
        else ""
    )
    return (
        f"**Speaker found.** I've marked the main person with a **green box** on the video; they are "
        f"visible in **{round(spk.coverage * 100)}%** of the frames {where}.{others}\n\n{note}"
        "When you export in portrait (or any other shape), the frame follows the green box. The boxes are only "
        "shown here to help you check the framing: they are never part of the exported video. Use **Speaker** above "
        "the video to hide or show them."
    )


MAX_TRANSCRIPT_REPLY_CHARS = 40_000
PARAGRAPH_S = 30.0  # without speakers, the transcript is shown in paragraphs of about half a minute
UNCLEAR = "Not clear"


def _talker(turns: list[SpeakerTurnOut], start: float, end: float) -> str | None:
    """Label of the turn that overlaps the cue most, if any."""
    best, label = 0.0, None
    for t in turns:
        overlap = min(end, t.end) - max(start, t.start)
        if overlap > best:
            best, label = overlap, t.label
    return label


def transcript_reply(transcript: Transcript, spk: SpeakerOut | None, by_speaker: bool) -> str:
    """The transcript as Markdown, grouped by who is talking when lip movement found speakers, else in short
    paragraphs. Facts come from the transcript and the speaker turns only."""
    if not transcript.cues:
        return "This video has no transcript yet. It is made while the video is processed."
    turns = spk.turns if spk is not None and spk.analysed else []
    blocks: list[tuple[str | None, float, float, list[str]]] = []
    for c in transcript.cues:
        who = (_talker(turns, c.start, c.end) or UNCLEAR) if turns else None
        last = blocks[-1] if blocks else None
        same = last is not None and (who == last[0] if turns else c.start - last[1] < PARAGRAPH_S)
        if last is not None and same:
            blocks[-1] = (last[0], last[1], c.end, [*last[3], c.text.strip()])
        else:
            blocks.append((who, c.start, c.end, [c.text.strip()]))

    head: list[str] = []
    if turns:
        talk: dict[str, float] = {}
        for t in turns:
            talk[t.label] = talk.get(t.label, 0.0) + t.end - t.start
        people = ", ".join(f"**{label}** about {_clock(s)}" for label, s in talk.items())
        head.append(
            f"**Who said what.** I matched each person's lip movement to the voice. Talking time: {people}. "
            f"**{UNCLEAR}** is speech nobody on screen clearly says (off camera, a cutaway, or a face too small "
            "to read)."
        )
    elif by_speaker:
        head.append(
            "I can't tell the speakers apart in this video (no one's lips clearly matched the voice), so here is "
            "the transcript by time."
        )
    else:
        head.append("**Transcript**")

    body: list[str] = []
    used = 0
    for who, start, end, texts in blocks:
        title = f"**{who}** · {_clock(start)}–{_clock(end)}" if who else f"**{_clock(start)}–{_clock(end)}**"
        block = f"{title}\n{' '.join(texts)}"
        if used + len(block) > MAX_TRANSCRIPT_REPLY_CHARS:
            body.append(f"…the rest continues from **{_clock(start)}**.")
            break
        body.append(block)
        used += len(block)
    tail = "The full transcript, with every line's time, is in the **Transcript** tab."
    return "\n\n".join([*head, *body, tail])


def _error_reply(e: Exception) -> tuple[str, str]:
    if isinstance(e, ClientError) and e.response.get("Error", {}).get("Code") == "ThrottlingException":
        return "rate_limited", RATE_LIMITED_TEXT
    status = getattr(e, "status_code", None)  # OpenAI-compatible providers (Groq, NVIDIA)
    if status == 429:
        return "rate_limited", RATE_LIMITED_TEXT
    if status == 413:
        return "too_large", TOO_LARGE_TEXT
    return "internal", FAILED_TEXT


def _stream_reply(text: str) -> Iterator[str]:
    """A ready reply sent line by line as tokens, like a streamed answer."""
    for piece in re.findall(r"[^\n]*\n?", text):
        if piece:
            yield sse("token", {"text": piece})


def run_turn(llm: LLMClient, turn: TurnInput) -> Iterator[str]:
    global _request_limit
    ctx = CallContext(user_id=turn.user.id, project_id=turn.project_id, job_id=None, job_kind="chat")
    yield sse("status", {"stage": "understanding"})

    # 1. Route. A reply to the edit graph's question always goes back to it.
    try:
        intent: Intent = "edit" if turn.waiting_for_answer else route(llm, ctx, turn.text).intent
    except Exception as e:
        log.warning("chat routing failed: %s: %s", type(e).__name__, e)
        code, text = _error_reply(e)
        user_msg = _save(turn.project_id, MessageRole.user.value, turn.text, {"intent": "unknown"}, None)
        reply = _save(turn.project_id, MessageRole.assistant.value, text, {"error": code}, None)
        yield sse("route", {"intent": "unknown", "message": message_out(user_msg).model_dump(mode="json")})
        yield sse("error", {"code": code, "message": message_out(reply).model_dump(mode="json")})
        return

    # 2. Edits become a background job, exactly as before.
    if intent == "edit":
        with session_scope() as tx:
            project = own_project(tx, turn.user, turn.project_id, lock=True)
            message, job = post_user_message(tx, turn.user.id, project, turn.text, None, None)
            job_json = job_out(job)
            payload = {
                "intent": "edit",
                "message": message_out(message).model_dump(mode="json"),
                "job": job_json.model_dump(mode="json") if job_json else None,
            }
        yield sse("route", payload)
        return

    # 3. "Who is speaking?": answered from the YOLO detections (no model call); the editor turns the boxes on.
    if intent == "speaker":
        with session_scope() as tx:
            spk = speaker_for(tx, own_project(tx, turn.user, turn.project_id))
            version = own_project(tx, turn.user, turn.project_id).current_version or None
        user_msg = _save(turn.project_id, MessageRole.user.value, turn.text, {"intent": intent}, version)
        yield sse("route", {"intent": intent, "message": message_out(user_msg).model_dump(mode="json")})
        found = spk.main_track_id is not None
        text = speaker_reply(spk, turn.text, version is not None)
        yield from _stream_reply(text)
        reply = _save(
            turn.project_id,
            MessageRole.assistant.value,
            text,
            {"intent": intent, "show_speaker": spk.available and found},
            version,
        )
        yield sse("done", {"message": message_out(reply).model_dump(mode="json")})
        return

    # 4. The transcript itself, or who said what: from the transcript and the speaker turns (no model call).
    if intent == "transcript":
        with session_scope() as tx:
            project = own_project(tx, turn.user, turn.project_id)
            info = load_project(tx, turn.project_id, turn.user.id)
            spk = speaker_for(tx, project)
            version = project.current_version or None
        user_msg = _save(turn.project_id, MessageRole.user.value, turn.text, {"intent": intent}, version)
        yield sse("route", {"intent": intent, "message": message_out(user_msg).model_dump(mode="json")})
        by_speaker = re.search(r"\b(speakers?|people|persons?|who)\b", turn.text, re.I) is not None
        text = transcript_reply(load_transcript(info), spk, by_speaker)
        yield from _stream_reply(text)
        reply = _save(turn.project_id, MessageRole.assistant.value, text, {"intent": intent}, version)
        yield sse("done", {"message": message_out(reply).model_dump(mode="json")})
        return

    # 5. Questions and small talk stream from here.
    with session_scope() as tx:
        info = load_project(tx, turn.project_id, turn.user.id)
        current = current_version_row(tx, own_project(tx, turn.user, turn.project_id))
        plan = plan_summary(EDL.model_validate(current.edl)) if current else None
    history = _history(turn.project_id)
    version = info.current_version or None
    user_msg = _save(turn.project_id, MessageRole.user.value, turn.text, {"intent": intent}, version)
    yield sse("route", {"intent": intent, "message": message_out(user_msg).model_dump(mode="json")})

    transcript = load_transcript(info)
    parts: list[str] = []
    for attempt in (1, 2):
        text, cut = transcript_context(transcript, max_transcript_chars())
        history_chars = LIMITED_HISTORY_CHARS if _request_limit is not None else HISTORY_MESSAGE_CHARS
        system, messages = build_messages(intent, turn.text, history, info.title, text, cut, plan, history_chars)
        try:
            for piece in llm.stream_text(ctx, f"answer_{intent}", system, messages, tier="chat"):
                parts.append(piece)
                yield sse("token", {"text": piece})
            break
        except Exception as e:
            limit = request_limit(e)
            if attempt == 1 and not parts and limit is not None and limit != _request_limit:
                log.warning("chat request too large for the provider (limit %s tokens); retrying smaller", limit)
                _request_limit = limit  # fits once: the transcript and the history are cut to this from now on
                continue
            log.warning("chat answer failed: %s: %s", type(e).__name__, e)
            code, fallback = _error_reply(e)
            reply = _save(turn.project_id, MessageRole.assistant.value, fallback, {"error": code}, None)
            yield sse("error", {"code": code, "message": message_out(reply).model_dump(mode="json")})
            return

    full = "".join(parts)
    cites = citations(full, transcript) if intent == "question" else []
    reply = _save(
        turn.project_id,
        MessageRole.assistant.value,
        clean(full) or FAILED_TEXT,
        {"intent": intent, "citations": cites},
        info.current_version or None,
    )
    yield sse("done", {"message": message_out(reply).model_dump(mode="json")})
