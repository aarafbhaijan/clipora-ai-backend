"""The edit graph (architecture.md 11.1). LangGraph, thread_id = project_id, Postgres checkpointer.

load_context -> parse_intent -> [post_question -> ask_user (interrupt)] -> retrieve -> find_candidates
-> select_and_order -> build_crop -> assemble_edl -> critic -> save_and_reply -> await_feedback (interrupt)
Corrections: parse_intent -> revise_plan -> save_and_reply. Questions: parse_intent -> answer_from_video.

State holds only small, serialisable values. Transcript and detections are read through the cached
loaders in agent.project, never stored in checkpoints. Job-specific things (LLM client, progress,
DB access) come in through the runtime context.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from agent import prompts, store
from agent.answer import NOT_FOUND, answer_question
from agent.crops import refresh_crops
from agent.intent import Intent, clamp_target, intent_from_suggestion
from agent.llm import CallContext, LLMClient, data_block
from agent.ops import apply_ops
from agent.planning import (
    CandidatesOut,
    CriticOut,
    Moment,
    SelectionOut,
    assemble,
    fit_to_target,
    moments_from_edl,
    new_short,
    plan_issues,
    resolve_moments,
    transition_for,
)
from agent.project import (
    ProjectInfo,
    cue_lines,
    current_edl,
    fmt_time,
    load_detections,
    load_project,
    load_transcript,
    plan_summary,
)
from core.edl import EDL, Source
from core.enums import Author, MessageKind
from core.ops import EditOp
from core.timemap import output_duration
from core.validation import find_issues
from db.models import Message

MAX_PLAN_RETRIES = 2
MAX_CONFLICT_RETRIES = 2


@dataclass
class EditDeps:
    """Runtime context for one job. Never checkpointed."""

    llm: LLMClient
    call: CallContext
    session: Callable[[], AbstractContextManager[Session]]
    progress: Callable[[str, float], None] = lambda stage, pct: None
    check: Callable[[], None] = lambda: None
    retrieve: Callable[[Session, uuid.UUID, list[str]], list[Any]] | None = None
    match_memories: Callable[[Session, uuid.UUID, str], list[Any]] | None = None
    embed: Callable[[str], list[float]] | None = None
    model_id: str = ""
    _info: dict[str, ProjectInfo] = field(default_factory=dict)


class EditState(TypedDict, total=False):
    project_id: str
    user_id: str
    message_id: str
    message: str
    suggestion: dict[str, Any] | None
    asked: bool
    base_version: int
    memories: list[str]
    intent: dict[str, Any]
    hits: list[dict[str, Any]]
    moments: list[dict[str, Any]]
    chosen: list[dict[str, Any]]
    feedback: list[str]
    attempts: int
    conflicts: int
    edl: dict[str, Any] | None
    ops: list[dict[str, Any]]
    applied: list[str]
    skipped: list[str]
    route: str
    done: bool


# Helpers


def _info(rt: Runtime[EditDeps], s: EditState) -> ProjectInfo:
    key = s["project_id"]
    if key not in rt.context._info:
        with rt.context.session() as tx:
            rt.context._info[key] = load_project(tx, uuid.UUID(key), uuid.UUID(s["user_id"]))
    return rt.context._info[key]


def _source(info: ProjectInfo) -> Source:
    return Source(key=info.source_key, duration_s=info.duration_s, width=info.width, height=info.height, fps=info.fps)


def _moments(items: list[dict[str, Any]]) -> list[Moment]:
    return [Moment(**m) for m in items]


def _dump(moments: list[Moment]) -> list[dict[str, Any]]:
    return [{"start": m.start, "end": m.end, "reason": m.reason, "score": m.score} for m in moments]


def _reply(
    rt: Runtime[EditDeps],
    s: EditState,
    text: str,
    payload: dict[str, Any] | None = None,
    kind: MessageKind = MessageKind.text,
) -> None:
    """Post a reply once per user message (a repeated run finds the first one)."""
    with rt.context.session() as tx:
        pid, mid = uuid.UUID(s["project_id"]), uuid.UUID(s["message_id"])
        if store.existing_reply(tx, pid, mid) is None:
            store.post_message(tx, pid, text, kind=kind, payload={"reply_to": s["message_id"], **(payload or {})})


# Nodes


def load_context(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.check()
    deps.progress("understanding", 5)
    deps._info.clear()
    info = _info(runtime, s)
    with deps.session() as tx:
        msg = tx.get(Message, uuid.UUID(s["message_id"]))
        assert msg is not None and msg.project_id == info.id
        text = msg.content
        memories: list[str] = []
        if deps.match_memories is not None and store.learn_enabled(tx, info.user_id):
            memories = [f"[{m.kind}] {m.text}" for m in deps.match_memories(tx, info.user_id, text)]
    return {
        "message": text,
        "suggestion": msg.payload.get("suggestion"),
        "asked": False,
        "base_version": info.current_version,
        "memories": memories,
        "attempts": 0,
        "conflicts": 0,
        "feedback": [],
        "ops": [],
        "applied": [],
        "skipped": [],
        "edl": None,
        "done": False,
    }


def parse_intent(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    info = _info(runtime, s)
    if s.get("suggestion"):
        return {"intent": intent_from_suggestion(s["suggestion"] or {}).model_dump(), "route": "plan"}
    with deps.session() as tx:
        plan = current_edl(tx, info.id, info.current_version)
    parts = [data_block("user_message", s["message"])]
    if s.get("memories"):
        parts.append(data_block("saved_preferences", "\n".join(s["memories"])))
    parts.append(data_block("current_plan", plan_summary(plan) if plan else "No plan yet."))
    if s.get("asked"):
        parts.append("You already asked a question this turn. Do not ask another; decide with what you have.")
    intent = deps.llm.structured(deps.call, "parse_intent", Intent, prompts.PARSE_INTENT, "\n".join(parts))
    if s.get("asked"):
        intent = intent.model_copy(update={"question": None, "choices": []})
    if intent.kind == "correction" and plan is None:
        intent = intent.model_copy(update={"kind": "plan"})
    with deps.session() as tx:
        if intent.standing_instruction and intent.standing_kind:
            emb = deps.embed(intent.standing_instruction) if deps.embed else None
            store.save_standing_instruction(tx, info.user_id, intent.standing_kind, intent.standing_instruction, emb)
        if intent.kind == "correction":
            store.record_feedback(
                tx,
                info.user_id,
                info.id,
                target_kind="plan",
                target_ref=str(info.current_version),
                signal="correction",
                reason=s["message"][:500],
            )
    route = "ask" if intent.question and intent.choices else intent.kind
    return {"intent": intent.model_dump(), "route": route}


def post_question(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    intent = Intent.model_validate(s["intent"])
    _reply(runtime, s, intent.question or "", {"choices": intent.choices}, kind=MessageKind.choices)
    return {}


def ask_user(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    """Interrupt: the job becomes waiting_input; the user's answer resumes this same job row."""
    answer = interrupt({"kind": "ask_user", "message_id": s["message_id"]})
    answer_id = str(answer["message_id"])
    with runtime.context.session() as tx:
        msg = tx.get(Message, uuid.UUID(answer_id))
        text = msg.content if msg else ""
    return {"message": f"{s['message']}\nAnswer to your question: {text}", "message_id": answer_id, "asked": True}


def retrieve(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    intent = Intent.model_validate(s["intent"])
    if not intent.focus or deps.retrieve is None:
        return {"hits": []}
    with deps.session() as tx:
        hits = deps.retrieve(tx, uuid.UUID(s["project_id"]), [intent.focus])
    return {"hits": [{"cue_from": h.cue_from, "cue_to": h.cue_to, "text": h.text} for h in hits]}


def find_candidates(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.check()
    deps.progress("finding moments", 25)
    info = _info(runtime, s)
    transcript = load_transcript(info)
    intent = Intent.model_validate(s["intent"])
    request = {
        "target_seconds": clamp_target(intent.target_duration_s, info.duration_s),
        "focus": intent.focus,
        "style": intent.style,
    }
    parts = [
        data_block("request", f"{s['message']}\n{request}"),
        data_block("transcript", cue_lines(transcript.cues)),
    ]
    if s.get("hits"):
        parts.append(
            data_block("relevant_passages", "\n".join(f"{h['cue_from']}-{h['cue_to']}: {h['text']}" for h in s["hits"]))
        )
    if s.get("memories"):
        parts.append(data_block("saved_preferences", "\n".join(s["memories"])))
    out = (
        deps.llm.structured(
            deps.call, "find_candidates", CandidatesOut, prompts.FIND_CANDIDATES, "\n".join(parts), temperature=0.2
        )
        if transcript.cues
        else CandidatesOut(candidates=[])
    )
    moments = resolve_moments(transcript, out.candidates, intent)
    return {"moments": _dump(moments)}


def select_and_order(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.check()
    deps.progress("building plan", 50)
    info = _info(runtime, s)
    intent = Intent.model_validate(s["intent"])
    pool = _moments(s["moments"])
    target = clamp_target(intent.target_duration_s, info.duration_s)
    listing = "\n".join(
        f"{i}: {fmt_time(m.start)}-{fmt_time(m.end)} ({m.length:.1f}s) score {m.score:.2f} {m.reason}"
        for i, m in enumerate(pool)
    )
    parts = [
        data_block("request", s["message"]),
        f"Target length: {target:.0f} seconds.",
        data_block("candidates", listing),
    ]
    if s.get("feedback"):
        parts.append("Fix these problems from the last attempt:\n" + "\n".join(f"- {f}" for f in s["feedback"]))
    out = deps.llm.structured(
        deps.call, "select_and_order", SelectionOut, prompts.SELECT_AND_ORDER, "\n".join(parts), temperature=0.2
    )
    chosen = [pool[i] for i in dict.fromkeys(out.order) if 0 <= i < len(pool)]
    d = transition_for(intent.style).effective_duration
    boundaries = load_transcript(info).word_boundaries()
    return {"chosen": _dump(fit_to_target(chosen, pool, target, d, boundaries))}


def build_crop(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.progress("framing the speaker", 65)
    info = _info(runtime, s)
    intent = Intent.model_validate(s["intent"])
    idea_title = (s.get("suggestion") or {}).get("title")
    edl = assemble(
        moments=_moments(s["chosen"]),
        pool=_moments(s["moments"]),
        intent=intent,
        source=_source(info),
        detections=load_detections(info),
        model_id=deps.model_id,
        prompt_version=prompts.PROMPT_VERSION,
        short=new_short(idea_title or intent.focus or s.get("message")),
    )
    return {"edl": edl.to_json()}


def assemble_edl(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    issues = plan_issues(EDL.model_validate(s["edl"]))
    if not issues:
        return {"route": "ok"}
    if s.get("attempts", 0) < MAX_PLAN_RETRIES:
        return {"route": "retry", "attempts": s.get("attempts", 0) + 1, "feedback": issues}
    _reply(runtime, s, "I could not build a plan that fits. Try a different length or topic.")
    return {"route": "give_up", "done": True}


def critic(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.check()
    deps.progress("checking", 80)
    edl = EDL.model_validate(s["edl"])
    info = _info(runtime, s)
    transcript = load_transcript(info)
    clips = "\n".join(
        f"{seg.id} ({fmt_time(seg.src_start)}-{fmt_time(seg.src_end)}): "
        + " ".join(c.text for c in transcript.cues if c.end > seg.src_start and c.start < seg.src_end)
        for seg in edl.segments
    )
    out = deps.llm.structured(
        deps.call,
        "critic",
        CriticOut,
        prompts.CRITIC,
        data_block("request", s["message"]) + "\n" + data_block("planned_clips", clips),
    )
    if out.ok or s.get("attempts", 0) >= MAX_PLAN_RETRIES:
        return {"route": "ok"}
    return {"route": "retry", "attempts": s.get("attempts", 0) + 1, "feedback": out.problems}


def _describe(edl: EDL) -> str:
    clips = ", ".join(f"{fmt_time(seg.src_start)}-{fmt_time(seg.src_end)}" for seg in edl.segments)
    return (
        f"Here is a {output_duration(edl):.0f}-second {edl.output.aspect} plan with {len(edl.segments)} clips "
        f"({clips}), joined with {edl.transitions.default.type.value.replace('_', ' ')}."
    )


def save_and_reply(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.progress("saving", 95)
    pid, mid = uuid.UUID(s["project_id"]), uuid.UUID(s["message_id"])
    edl = EDL.model_validate(s["edl"])
    revision = bool(s.get("ops"))
    text = (
        " ".join(s.get("applied", [])) + (" " + " ".join(s["skipped"]) if s.get("skipped") else "")
        if revision
        else _describe(edl)
    ).strip()
    try:
        with deps.session() as tx:
            if store.existing_reply(tx, pid, mid) is not None:
                return {"done": True}
            store.save_plan(
                tx,
                pid,
                edl,
                base_version=s["base_version"],
                reply=text,
                payload={
                    "reply_to": s["message_id"],
                    "kind": "plan",
                    "changes": s.get("applied", []),
                    "skipped": s.get("skipped", []),
                },
                allow_on_top=not revision,
            )
    except store.PlanConflict as e:
        if s.get("conflicts", 0) >= MAX_CONFLICT_RETRIES:
            _reply(runtime, s, "The plan kept changing while I worked. Please try again.")
            return {"done": True, "route": "stop"}
        return {"route": "conflict", "conflicts": s.get("conflicts", 0) + 1, "base_version": e.current_version}
    return {"done": True, "route": "saved"}


class FlatOp(BaseModel):
    """One edit op in a flat shape (Bedrock JSON schema); code converts it to core.ops."""

    op: Literal["remove", "trim", "add", "reorder", "set_target", "set_transition", "set_aspect"]
    id: str | None = None
    src_start: float | None = None
    src_end: float | None = None
    after_id: str | None = None
    ids: list[str] | None = None
    seconds: float | None = None
    from_id: str | None = None
    to_id: str | None = None
    all: bool | None = None
    type: str | None = None
    duration_s: float | None = None
    aspect: str | None = None
    width: int | None = None
    height: int | None = None
    framing: Literal["crop", "fit"] | None = None


class RevisionOut(BaseModel):
    ops: list[FlatOp] = Field(max_length=20)
    summary: str = Field(default="", max_length=300)


_EDIT_OP = TypeAdapter(EditOp)


def to_edit_ops(flat: list[FlatOp]) -> tuple[list[EditOp], list[str]]:
    ops: list[EditOp] = []
    bad: list[str] = []
    for f in flat:
        data = {k: v for k, v in f.model_dump().items() if v is not None and k not in ("from_id", "to_id")}
        if f.from_id is not None:
            data["from"] = f.from_id
        if f.to_id is not None:
            data["to"] = f.to_id
        try:
            ops.append(_EDIT_OP.validate_python(data))
        except ValidationError:
            bad.append(f"I could not apply a {f.op.replace('_', ' ')} change.")
    return ops, bad


def revise_plan(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.check()
    deps.progress("changing the plan", 40)
    info = _info(runtime, s)
    with deps.session() as tx:
        fresh = load_project(tx, info.id, info.user_id)
        current = current_edl(tx, info.id, fresh.current_version)
    assert current is not None
    transcript = load_transcript(info)
    if s.get("ops"):
        ops = [_EDIT_OP.validate_python(o) for o in s["ops"]]
        bad: list[str] = []
    else:
        parts = [
            data_block("user_message", s["message"]),
            data_block("current_plan", plan_summary(current)),
            data_block("transcript", cue_lines(transcript.cues)),
        ]
        out = deps.llm.structured(deps.call, "revise_plan", RevisionOut, prompts.REVISE, "\n".join(parts))
        ops, bad = to_edit_ops(out.ops)
    result = apply_ops(current, ops, transcript.word_boundaries())
    edl = refresh_crops(result.edl, current, load_detections(info))
    skipped = bad + result.skipped
    if not result.applied:
        _reply(runtime, s, " ".join(skipped) or "I did not find anything to change.")
        return {"route": "nothing", "done": True}
    if result.rerun_selection:
        now, pool = moments_from_edl(edl)
        intent = Intent(
            kind="plan",
            target_duration_s=edl.output.target_duration_s,
            aspect=edl.output.aspect,
            framing=edl.output.framing.value,
        )
        boundaries = transcript.word_boundaries()
        target = clamp_target(intent.target_duration_s, edl.source.duration_s)
        chosen = fit_to_target(now, pool, target, edl.transitions.default.effective_duration, boundaries)
        edl = assemble(
            moments=chosen,
            pool=pool,
            intent=intent,
            source=edl.source,
            detections=load_detections(info),
            model_id=deps.model_id,
            prompt_version=prompts.PROMPT_VERSION,
            short=(current.meta.model_extra or {}).get("short"),
        ).model_copy(update={"transitions": edl.transitions, "captions": edl.captions})
    issues = [f"{i.path}: {i.message}" for i in find_issues(edl, Author.user)]
    if issues:
        _reply(runtime, s, "That change would make the plan invalid, so I left it as it was. " + "; ".join(issues[:3]))
        return {"route": "nothing", "done": True}
    return {
        "edl": edl.to_json(),
        "ops": [o.model_dump(by_alias=True, mode="json") for o in ops],
        "applied": result.applied,
        "skipped": skipped,
        "base_version": fresh.current_version,
        "route": "save",
    }


def answer_from_video(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    deps = runtime.context
    deps.progress("searching the video", 40)
    info = _info(runtime, s)
    transcript = load_transcript(info)

    def search(queries: list[str]) -> list[Any]:
        if deps.retrieve is None:
            return []
        with deps.session() as tx:
            return deps.retrieve(tx, info.id, queries)

    ans = answer_question(deps.llm, deps.call, s["message"], transcript, search)
    _reply(runtime, s, ans.text, {"kind": "answer", "citations": ans.citations, "found": ans.text != NOT_FOUND})
    return {"done": True}


def reply_metadata(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    _reply(runtime, s, "Open the Metadata tab and press Write title and hashtags. I will write them from your plan.")
    return {"done": True}


def await_feedback(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    """Interrupt: the job succeeds here. The next message starts a new job that resumes at this point."""
    nxt = interrupt({"kind": "await_feedback"})
    return {"message_id": str(nxt["message_id"])}


# Routing


def _after_intent(s: EditState) -> str:
    return {
        "ask": "post_question",
        "question": "answer_from_video",
        "correction": "revise_plan",
        "metadata": "reply_metadata",
    }.get(s.get("route", ""), "retrieve")


def build_graph() -> StateGraph[EditState, EditDeps, EditState, EditState]:
    g: StateGraph[EditState, EditDeps, EditState, EditState] = StateGraph(EditState, context_schema=EditDeps)
    for name, fn in [
        ("load_context", load_context),
        ("parse_intent", parse_intent),
        ("post_question", post_question),
        ("ask_user", ask_user),
        ("retrieve", retrieve),
        ("find_candidates", find_candidates),
        ("select_and_order", select_and_order),
        ("build_crop", build_crop),
        ("assemble_edl", assemble_edl),
        ("critic", critic),
        ("save_and_reply", save_and_reply),
        ("revise_plan", revise_plan),
        ("answer_from_video", answer_from_video),
        ("reply_metadata", reply_metadata),
        ("await_feedback", await_feedback),
    ]:
        g.add_node(name, fn)  # type: ignore[call-overload]
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "parse_intent")
    g.add_conditional_edges("parse_intent", _after_intent)
    g.add_edge("post_question", "ask_user")
    g.add_edge("ask_user", "parse_intent")
    g.add_edge("retrieve", "find_candidates")
    g.add_conditional_edges(
        "find_candidates",
        lambda s: "select_and_order" if s.get("moments") else "no_moments",
        {"select_and_order": "select_and_order", "no_moments": "reply_no_moments"},
    )
    g.add_node("reply_no_moments", _no_moments)  # type: ignore[call-overload]
    g.add_edge("reply_no_moments", "await_feedback")
    g.add_edge("select_and_order", "build_crop")
    g.add_edge("build_crop", "assemble_edl")
    g.add_conditional_edges(
        "assemble_edl", lambda s: {"retry": "select_and_order", "give_up": "await_feedback"}.get(s["route"], "critic")
    )
    g.add_conditional_edges("critic", lambda s: "select_and_order" if s["route"] == "retry" else "save_and_reply")
    g.add_conditional_edges(
        "save_and_reply", lambda s: "revise_plan" if s.get("route") == "conflict" else "await_feedback"
    )
    g.add_conditional_edges("revise_plan", lambda s: "save_and_reply" if s["route"] == "save" else "await_feedback")
    g.add_edge("answer_from_video", "await_feedback")
    g.add_edge("reply_metadata", "await_feedback")
    g.add_edge("await_feedback", "load_context")
    return g


def _no_moments(s: EditState, runtime: Runtime[EditDeps]) -> dict[str, Any]:
    _reply(runtime, s, "I could not find moments that fit that request. Try another topic or length.")
    return {"done": True}
