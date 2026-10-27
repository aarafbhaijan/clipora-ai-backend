"""Metadata graph (architecture.md 11.3). thread_id = "{project_id}:metadata".

route_message -> load_short -> understand_short -> choose_platform -> [post_question -> ask_user]
-> write_fields -> check -> save_and_reply -> await_feedback -> route_message -> revise_fields -> check ...
Each saved result is a new append-only metadata_sets row tied to an EDL version.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent import prompts, store
from agent.llm import CallContext, LLMClient, data_block
from agent.metadata_graph.fields import FieldOpsOut, apply_field_ops
from agent.metadata_graph.platforms import DOMAINS, PLATFORMS, limit_issues
from agent.project import current_edl, load_project, load_transcript
from core.enums import Author, MessageKind, Thread
from core.metadata import MetadataFields, PlatformPick, field_issues
from core.timemap import output_duration
from db.ids import uuid7
from db.models import Message, MetadataSet, Project

MAX_REWRITES = 2
DOMAIN_CONFIDENCE_MIN = 0.6

WRITE = f"""{prompts.ROLE}
Write posting text for this short: a title with 2 alternates, a thumbnail title of 2 to 5 words and at most
28 characters (text only) with 2 alternates, a description, the platform pick with a reason and up to 2
alternates, 3 to 8 hashtags, and a domain from the list with an optional subtopic. Use only what is said in
the short. Never claim anything the lines do not say."""

UNDERSTAND = f"""{prompts.ROLE}
Read the lines of this short. Summarise it, give its language as a BCP-47 code or "unknown", say whether
it has speech, and pick the closest domain from the list with your confidence from 0 to 1."""

CHOOSE = f"""{prompts.ROLE}
Recommend the best platform for this short given its aspect ratio, length and content, with a reason,
and up to 2 alternates with reasons."""

CHECK = f"""{prompts.ROLE}
Check the posting text against the lines of the short. List every claim that the lines do not support.
Hashtags and the domain are fine if they match the topic."""

REVISE = f"""{prompts.ROLE}
Turn the user's request into field operations: set_field(field, value), regenerate_field(field,
instruction), set_platform(platform), set_tone(tone), set_language(language). Use the fewest ops."""


class Understanding(BaseModel):
    summary: str = Field(max_length=600)
    language: str = Field(max_length=16)
    has_speech: bool
    domain: str
    subtopic: str | None = None
    domain_confidence: float = Field(ge=0, le=1)


class PlatformOut(BaseModel):
    platform: str
    reason: str = Field(max_length=200)
    alternates: list[dict[str, str]] = Field(default_factory=list, max_length=2)


class ClaimCheck(BaseModel):
    unsupported_claims: list[str] = Field(default_factory=list)


@dataclass
class MetaDeps:
    llm: LLMClient
    call: CallContext
    session: Callable[[], AbstractContextManager[Session]]
    progress: Callable[[str, float], None] = lambda stage, pct: None
    check: Callable[[], None] = lambda: None
    _lines: dict[str, str] = field(default_factory=dict)


class MetaState(TypedDict, total=False):
    project_id: str
    user_id: str
    message_id: str
    edl_version: int
    options: dict[str, str]
    understanding: dict[str, Any]
    platform: str
    platform_reason: str
    alternates: list[dict[str, str]]
    tone: str | None
    language: str | None
    fields: dict[str, Any] | None
    regenerate: dict[str, str]
    feedback: list[str]
    attempts: int
    applied: list[str]
    route: str


def _short_lines(rt: Runtime[MetaDeps], s: MetaState) -> str:
    """The short's lines in output order, with caption edits applied. Cached per EDL version."""
    key = f"{s['project_id']}:{s['edl_version']}"
    if key not in rt.context._lines:
        with rt.context.session() as tx:
            info = load_project(tx, uuid.UUID(s["project_id"]), uuid.UUID(s["user_id"]))
            edl = current_edl(tx, info.id, s["edl_version"])
        assert edl is not None
        transcript = load_transcript(info)
        lines = []
        for seg in edl.segments:
            for c in transcript.cues:
                if c.end > seg.src_start and c.start < seg.src_end:
                    lines.append(f"[{c.id}] {edl.captions.edits.get(c.id, c.text)}")
        rt.context._lines[key] = "\n".join(lines)
        rt.context._lines[key + ":facts"] = f"aspect {edl.output.aspect}, length {output_duration(edl):.0f} s"
    return rt.context._lines[key]


def _facts(rt: Runtime[MetaDeps], s: MetaState) -> str:
    _short_lines(rt, s)
    return rt.context._lines[f"{s['project_id']}:{s['edl_version']}:facts"]


def route_message(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    with runtime.context.session() as tx:
        msg = tx.get(Message, uuid.UUID(s["message_id"]))
        assert msg is not None
        options = msg.payload.get("options")
        edl_version = msg.edl_version or s.get("edl_version")
    if options is not None or not s.get("fields"):
        return {
            "route": "write",
            "options": options or s.get("options") or {},
            "edl_version": edl_version,
            "fields": None,
            "regenerate": {},
            "feedback": [],
            "attempts": 0,
            "applied": [],
        }
    return {"route": "revise", "feedback": [], "attempts": 0, "applied": []}


def understand_short(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.check()
    deps.progress("understanding", 10)
    u = deps.llm.structured(
        deps.call,
        "understand_short",
        Understanding,
        UNDERSTAND,
        data_block("short_lines", _short_lines(runtime, s)) + "\nDomains: " + ", ".join(DOMAINS),
        tier="small",
    )
    opts = s.get("options") or {}
    language = opts.get("language") or (None if u.language == "unknown" else u.language)
    return {"understanding": u.model_dump(), "language": language, "tone": opts.get("tone") or "neutral"}


def choose_platform(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    deps = runtime.context
    chosen = (s.get("options") or {}).get("platform")
    if chosen in PLATFORMS:
        return {"platform": chosen, "platform_reason": "Your choice", "alternates": []}
    out = deps.llm.structured(
        deps.call,
        "choose_platform",
        PlatformOut,
        CHOOSE,
        f"Facts: {_facts(runtime, s)}\nPlatforms: {', '.join(PLATFORMS)}\n"
        + data_block("summary", s["understanding"]["summary"]),
        tier="small",
    )
    platform = out.platform if out.platform in PLATFORMS else "youtube_shorts"
    alternates = [a for a in out.alternates if a.get("platform") in PLATFORMS]
    return {"platform": platform, "platform_reason": out.reason, "alternates": alternates}


def _needs_question(s: MetaState) -> str | None:
    u = s["understanding"]
    if not u.get("has_speech"):
        return "I could not hear speech in this short. What is it about?"
    if s.get("language") is None:
        return "Which language should the text be in?"
    if float(u.get("domain_confidence", 1)) < DOMAIN_CONFIDENCE_MIN:
        return "Which topic fits this short best?"
    return None


def post_question(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    question = _needs_question(s) or ""
    choices = (
        ["English", "Spanish", "Hindi", "French"]
        if "language" in question
        else [d.title() for d in DOMAINS[:4]]
        if "topic" in question
        else []
    )
    with runtime.context.session() as tx:
        pid, mid = uuid.UUID(s["project_id"]), uuid.UUID(s["message_id"])
        if store.existing_reply(tx, pid, mid, Thread.metadata) is None:
            store.post_message(
                tx,
                pid,
                question,
                kind=MessageKind.choices,
                thread=Thread.metadata,
                payload={"reply_to": s["message_id"], "choices": choices},
            )
    return {}


def ask_user(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    answer = interrupt({"kind": "ask_user", "message_id": s["message_id"]})
    answer_id = str(answer["message_id"])
    with runtime.context.session() as tx:
        msg = tx.get(Message, uuid.UUID(answer_id))
        text = (msg.content if msg else "").strip()
    u = dict(s["understanding"])
    update: dict[str, Any] = {"message_id": answer_id}
    question = _needs_question(s) or ""
    if "language" in question:
        update["language"] = text or "en"
    else:
        u.update({"has_speech": True, "domain_confidence": 1.0, "summary": f"{u.get('summary', '')} {text}".strip()})
        if text.lower() in DOMAINS:
            u["domain"] = text.lower()
    update["understanding"] = u
    return update


def write_fields(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.check()
    deps.progress("writing", 50)
    u = s["understanding"]
    opts = s.get("options") or {}
    parts = [
        data_block("short_lines", _short_lines(runtime, s)),
        f"Facts: {_facts(runtime, s)}. Platform: {s['platform']} ({s.get('platform_reason', '')}). "
        f"Tone: {s.get('tone') or 'neutral'}. Language: {s.get('language') or 'same as the lines'}. "
        f"Domain guess: {u.get('domain')}. Domains: {', '.join(DOMAINS)}.",
    ]
    if opts.get("notes"):
        parts.append(data_block("user_notes", opts["notes"]))
    previous = s.get("fields") or {}
    if previous and s.get("regenerate"):
        keep = {k: v for k, v in previous.items() if k not in s["regenerate"]}
        parts.append(data_block("keep_these_fields_unchanged", str(keep)))
        parts.append("Rewrite only: " + ", ".join(f"{k} ({v or 'fresh take'})" for k, v in s["regenerate"].items()))
    if s.get("feedback"):
        parts.append("Fix these problems:\n" + "\n".join(f"- {f}" for f in s["feedback"]))
    out = deps.llm.structured(deps.call, "write_fields", MetadataFields, WRITE, "\n".join(parts), temperature=0.2)
    if previous and s.get("regenerate"):
        merged = {**previous, **{k: getattr(out, k) for k in s["regenerate"] if hasattr(out, k)}}
        out = MetadataFields.model_validate(merged)
    if s.get("platform"):
        out = out.model_copy(update={"platform": out.platform.model_copy(update={"platform": s["platform"]})})
    return {"fields": out.model_dump(mode="json")}


def check(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.progress("checking", 80)
    fields = MetadataFields.model_validate(s["fields"])
    issues = field_issues(fields) + limit_issues(fields, s["platform"])
    if not issues:
        verdict = deps.llm.structured(
            deps.call,
            "check_claims",
            ClaimCheck,
            CHECK,
            data_block("short_lines", _short_lines(runtime, s))
            + "\n"
            + data_block("posting_text", fields.model_dump_json()),
        )
        issues = [f"Not supported by the video: {c}" for c in verdict.unsupported_claims]
    if issues and s.get("attempts", 0) < MAX_REWRITES:
        return {
            "route": "rewrite",
            "feedback": issues,
            "attempts": s.get("attempts", 0) + 1,
            "regenerate": s.get("regenerate") or {},
        }
    if issues:
        return {"route": "give_up", "feedback": issues}
    return {"route": "save"}


def save_and_reply(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    deps = runtime.context
    pid, mid = uuid.UUID(s["project_id"]), uuid.UUID(s["message_id"])
    with deps.session() as tx:
        if store.existing_reply(tx, pid, mid, Thread.metadata) is not None:
            return {"regenerate": {}}
        if s.get("route") == "give_up":
            store.post_message(
                tx,
                pid,
                "I could not write text I can stand behind. Try adding a note about the topic.",
                thread=Thread.metadata,
                payload={"reply_to": s["message_id"]},
            )
            return {"regenerate": {}}
        tx.execute(select(Project.id).where(Project.id == pid).with_for_update())  # serialise version numbers
        latest = tx.execute(select(func.max(MetadataSet.version)).where(MetadataSet.project_id == pid)).scalar()
        version = int(latest or 0) + 1
        fields = MetadataFields.model_validate(s["fields"])
        alternates = [PlatformPick(platform=a["platform"], reason=a.get("reason", "")) for a in s.get("alternates", [])]
        if alternates:
            fields = fields.model_copy(update={"platform_alternates": alternates[:2]})
        tx.add(
            MetadataSet(
                id=uuid7(),
                project_id=pid,
                version=version,
                edl_version=s["edl_version"],
                platform=s["platform"],
                language=s.get("language"),
                tone=s.get("tone"),
                fields=fields.model_dump(mode="json"),
                author=Author.agent.value,
                parent_version=version - 1 or None,
            )
        )
        text = " ".join(s.get("applied") or []) or "Here is your title, description and hashtags."
        store.post_message(
            tx,
            pid,
            text,
            kind=MessageKind.metadata,
            thread=Thread.metadata,
            payload={"reply_to": s["message_id"], "version": version},
            edl_version=s["edl_version"],
        )
    return {"regenerate": {}}


def revise_fields(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    deps = runtime.context
    with deps.session() as tx:
        msg = tx.get(Message, uuid.UUID(s["message_id"]))
        text = msg.content if msg else ""
    out = deps.llm.structured(
        deps.call,
        "revise_fields",
        FieldOpsOut,
        REVISE,
        data_block("user_message", text)
        + "\n"
        + data_block("current_text", str(s["fields"]))
        + f"\nPlatforms: {', '.join(PLATFORMS)}",
    )
    r = apply_field_ops(
        MetadataFields.model_validate(s["fields"]), s["platform"], s.get("tone"), s.get("language"), out.ops
    )
    route = "write" if r.regenerate else "check"
    return {
        "fields": r.fields.model_dump(mode="json"),
        "platform": r.platform,
        "tone": r.tone,
        "language": r.language,
        "regenerate": r.regenerate,
        "applied": r.applied + r.skipped,
        "route": route,
    }


def await_feedback(s: MetaState, runtime: Runtime[MetaDeps]) -> dict[str, Any]:
    nxt = interrupt({"kind": "await_feedback"})
    return {"message_id": str(nxt["message_id"])}


def build_graph() -> StateGraph[MetaState, MetaDeps, MetaState, MetaState]:
    g: StateGraph[MetaState, MetaDeps, MetaState, MetaState] = StateGraph(MetaState, context_schema=MetaDeps)
    for name, fn in [
        ("route_message", route_message),
        ("understand_short", understand_short),
        ("choose_platform", choose_platform),
        ("post_question", post_question),
        ("ask_user", ask_user),
        ("write_fields", write_fields),
        ("check", check),
        ("save_and_reply", save_and_reply),
        ("revise_fields", revise_fields),
        ("await_feedback", await_feedback),
    ]:
        g.add_node(name, fn)  # type: ignore[call-overload]
    g.add_edge(START, "route_message")
    g.add_conditional_edges("route_message", lambda s: "understand_short" if s["route"] == "write" else "revise_fields")
    g.add_edge("understand_short", "choose_platform")
    g.add_conditional_edges("choose_platform", lambda s: "post_question" if _needs_question(s) else "write_fields")
    g.add_edge("post_question", "ask_user")
    g.add_edge("ask_user", "write_fields")
    g.add_edge("write_fields", "check")
    g.add_conditional_edges("check", lambda s: "write_fields" if s["route"] == "rewrite" else "save_and_reply")
    g.add_edge("save_and_reply", "await_feedback")
    g.add_conditional_edges("revise_fields", lambda s: "write_fields" if s["route"] == "write" else "check")
    g.add_edge("await_feedback", "route_message")
    return g
