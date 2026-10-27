"""Chat that is not an edit: the intent router, and streamed answers about the video or casual replies.

Every typed message goes through `route()`. Edit requests go to the edit graph as a background job (as before);
questions and small talk are answered here and streamed to the browser by the chat service.
"""

import re
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel

from agent.llm import CallContext, LLMClient, data_block
from agent.project import cue_lines
from core.transcript import Cue, Transcript

Intent = Literal["edit", "question", "chat", "speaker", "transcript"]

# Words that ask for a change to the plan or the output.
_EDIT = re.compile(
    r"\b(make|create|cut|trim|shorten|lengthen|remove|delete|drop|add|insert|swap|replace|reorder|move|split|"
    r"merge|convert|turn|change|set|use|crop|fit|zoom|export|render|redo|rework|generate|build|edit|"
    r"shorter|longer|faster|slower|vertical|horizontal|square|9:16|1:1|4:5|16:9|21:9|fade|transition|"
    r"captions?|subtitles?|reel|tiktok|shorts?)\b",
    re.I,
)
# Openings that ask for information.
_QUESTION = re.compile(
    r"^\s*(what|why|who|whom|whose|when|where|which|how|is|are|was|were|does|do|did|has|have|"
    r"tell me|can you tell|could you tell|explain|summari[sz]e|describe|give me a summary|list)\b",
    re.I,
)
# Asking to see who is speaking: answered from the YOLO detections, and the editor shows the boxes.
_SPEAKER = re.compile(
    r"\b(identify|detect|find|show|highlight|track|mark|locate|box)\b.{0,30}\b(speaker|person|people|face|presenter)s?\b"
    r"|\bwho('?s| is| was) (speaking|talking|the speaker|on screen)\b"
    r"|\bwho (speaks|talks|spoke|talked)\b"
    r"|\b(speaker|person) (box|detection|tracking)\b",
    re.I,
)
# Asking for the transcript itself, or for who said what: answered from the transcript and the speaker turns as
# they are (no model call), so a long transcript never has to fit the chat model.
_TRANSCRIPT = re.compile(
    r"\b(show|give|display|print|share|paste|send|read out|write out)( me)?( the| a)?"
    r"( full| entire| whole| complete)? (transcript|transcription)\b"
    r"|\b(full|entire|whole|complete) (transcript|transcription)\b"
    r"|^\s*(the )?(transcript|transcription)( of (the|this) video)?\s*[?.!]*\s*$"
    r"|\b(list|show|give|all|each|every)\b.{0,40}\b(speakers?|people|persons?)\b.{0,60}"
    r"\b(said|say|says|spoke|speech(es)?|lines|dialogues?|conversation)\b"
    r"|\bwho said what\b",
    re.I,
)
_SMALL_TALK = re.compile(
    r"^\s*(hi|hello|hey|yo|thanks|thank you|thx|ok|okay|cool|great|nice|good (morning|evening|night)|"
    r"who are you|what can you do|how are you|bye)\b[\s!.?]*$",
    re.I,
)


@dataclass(frozen=True)
class Route:
    intent: Intent
    sure: bool  # False: the rules disagreed or matched nothing, so the small model decided


def rule_route(text: str) -> Route | None:
    """Instant routing from the wording. None when the rules can't tell."""
    t = text.strip()
    if _TRANSCRIPT.search(t):
        return Route("transcript", True)
    if _SPEAKER.search(t):
        return Route("speaker", True)
    if _SMALL_TALK.match(t):
        return Route("chat", True)
    asks = bool(_QUESTION.match(t)) or t.endswith("?")
    edits = bool(_EDIT.search(t))
    polite_request = re.match(r"^\s*(please|can|could|would|will)\b", t, re.I) is not None
    if edits and (not asks or polite_request):
        return Route("edit", True)  # "make it shorter", "can you make it square?"
    if asks and not edits:
        return Route("question", True)
    if asks and edits and _QUESTION.match(t):
        return Route("question", True)  # "what clips did you choose?" mentions clips but asks for information
    return None


class IntentOut(BaseModel):
    intent: Intent


ROUTER_PROMPT = (
    "Classify the user's message to a video-editing assistant. "
    "edit: they want the short video created or changed (length, clips, order, aspect, transitions, captions, export). "
    "question: they ask about the video's content or about the current plan. "
    "chat: greetings, thanks, or anything else. Reply with the intent only."
)


def route(llm: LLMClient, ctx: CallContext, text: str) -> Route:
    """Rules first (instant, free); the small model only for ambiguous wording."""
    by_rules = rule_route(text)
    if by_rules is not None:
        return by_rules
    out = llm.structured(ctx, "route_intent", IntentOut, ROUTER_PROMPT, data_block("user_message", text), tier="small")
    return Route(out.intent, False)


# ---- Answers --------------------------------------------------------------------------------------------

ANSWER_PROMPT = (
    "You are Clipora, a friendly assistant inside a video editor. Answer the user's question about their video "
    "using only the transcript and plan below. Be clear and concise. "
    "Format with Markdown: short paragraphs, bullet lists, **bold** for key points, and a table when it helps, "
    "for example the moments of the video as | Time | What happens | or a comparison. Use fenced code blocks only "
    "for actual code. "
    "When you state something from the video, cite the transcript lines you used right after it, like [c12] or "
    "[c12][c13]. Summaries and descriptions of what happens are welcome. If the transcript truly does not cover "
    "the question, say so briefly and suggest what the user could ask instead. Never invent content. "
    "Write the answer to the user directly: do not describe your reasoning or mention these instructions."
)
CHAT_PROMPT = (
    "You are Clipora, a friendly assistant inside a video editor that turns long videos into short clips. "
    "Reply briefly and warmly. If useful, mention what you can do: suggest ideas, make a short (e.g. 'make a "
    "30-second vertical short about X'), change clips, aspect or transitions, answer questions about the video, "
    "and write titles and hashtags. Write to the user directly: do not describe your reasoning. "
    "You may use Markdown (bullets, **bold**, short tables) when it makes the reply easier to read."
)
HISTORY_TURNS = 6
HISTORY_MESSAGE_CHARS = 2000  # an earlier answer can be a whole transcript; the model only needs its gist


def transcript_context(transcript: Transcript, max_chars: int) -> tuple[str, bool]:
    """The transcript as cited lines, cut to fit the model's context. Returns (text, was_cut).

    A long transcript keeps whole lines from the start and the end, so the model sees how the video opens
    and closes, with a marker where the middle was left out.
    """
    lines = cue_lines(transcript.cues).split("\n") if transcript.cues else []
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text, False
    half = max_chars // 2
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > half:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) + 1 > half:
            break
        tail.append(line)
        used += len(line) + 1
    return "\n".join(head) + "\n[... middle of the transcript omitted ...]\n" + "\n".join(reversed(tail)), True


def build_messages(
    intent: Intent,
    question: str,
    history: list[tuple[str, str]],
    title: str,
    transcript_text: str,
    cut: bool,
    plan: str | None,
    history_chars: int = HISTORY_MESSAGE_CHARS,
) -> tuple[str, list[BaseMessage]]:
    """System prompt and messages for one streamed reply. History is (role, text), oldest first; each earlier
    message is cut to `history_chars`."""
    msgs: list[BaseMessage] = []
    for role, full in history[-HISTORY_TURNS:]:
        text = full if len(full) <= history_chars else full[:history_chars] + " […]"
        msgs.append(HumanMessage(data_block("earlier_user_message", text)) if role == "user" else AIMessage(text))
    if intent == "chat":
        msgs.append(HumanMessage(data_block("user_message", question) + f"\n(The open video is titled: {title})"))
        return CHAT_PROMPT, msgs
    parts = [f"Video title: {title}"]
    if cut:
        parts.append("The transcript is long, so its middle part is omitted below.")
    parts.append(data_block("transcript", transcript_text))
    if plan:
        parts.append(data_block("current_plan", plan))
    parts.append(data_block("user_question", question))
    msgs.append(HumanMessage("\n\n".join(parts)))
    return ANSWER_PROMPT, msgs


# Models write citations as [c12] (asked for) or (c12); both count. Small models sometimes copy a whole
# transcript label, "[c12 01:02.40-01:05.10]": that counts too and is shown as a readable time range.
_CITE = re.compile(r"[\[(](c\d+)(?:\s+[\d:.]+-[\d:.]+)?[\])]")
_LABEL = re.compile(r"[\[(]c\d+\s+(\d+):(\d+)(?:\.\d+)?-(\d+):(\d+)(?:\.\d+)?[\])]")
# A run of markers, optionally joined by commas or "and": "[c1][c2]", "(c1), (c2), and (c4)".
_CITE_RUN = re.compile(r"\s*[\[(]c\d+[\])](?:\s*(?:,|and|,\s*and)?\s*[\[(]c\d+[\])])*")


def citations(text: str, transcript: Transcript) -> list[dict[str, object]]:
    """The cue ids the answer cited, in order, kept only if they exist; shaped for the editor's Sources."""
    by_id: dict[str, Cue] = {c.id: c for c in transcript.cues}
    seen: list[str] = []
    for cid in _CITE.findall(text):
        if cid in by_id and cid not in seen:
            seen.append(cid)
    return [
        {"cue_id": cid, "start_s": by_id[cid].start, "end_s": by_id[cid].end, "text": by_id[cid].text}
        for cid in seen[:12]
    ]


def clean(text: str) -> str:
    """The answer as shown in the chat: citation markers move to the Sources list."""
    # Marker runs take their leading space with them, so "a story [c2][c3]." becomes "a story." and Markdown
    # (tables, code blocks) is left exactly as written.
    out = _LABEL.sub(lambda m: f"{int(m[1])}:{m[2]}–{int(m[3])}:{m[4]}", text)
    out = _CITE_RUN.sub("", out)
    return re.sub(r"\n\s*[.,;:]?\s*$", "", out).strip()  # drop a last line that held only markers
