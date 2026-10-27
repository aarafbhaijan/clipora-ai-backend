"""The edit graph end to end with LangGraph's in-memory checkpointer, a scripted model and a fake database layer.

Checks routing, the ask_user interrupt (job waits), the await_feedback interrupt (job done, next message
resumes), that an idea needs no parse call, and that revisions go through ops.
"""

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent import edit_graph, store
from agent.edit_graph import EditDeps, build_graph
from agent.llm import CallContext, LLMClient
from agent.project import ProjectInfo
from agent.runner import run_turn
from core.edl import EDL
from core.transcript import Transcript


@dataclass
class FakeMessage:
    id: uuid.UUID
    project_id: uuid.UUID
    content: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class World:
    project_id: uuid.UUID
    user_id: uuid.UUID
    messages: dict[uuid.UUID, FakeMessage] = field(default_factory=dict)
    replies: list[dict[str, Any]] = field(default_factory=list)
    versions: list[EDL] = field(default_factory=list)

    def say(self, text: str, **payload: Any) -> uuid.UUID:
        mid = uuid.uuid4()
        self.messages[mid] = FakeMessage(mid, self.project_id, text, payload)
        return mid


class FakeTx:
    def __init__(self, world: World) -> None:
        self.world = world

    def get(self, model: Any, key: uuid.UUID) -> Any:
        return self.world.messages.get(key)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, transcript: Transcript) -> World:
    w = World(uuid.uuid4(), uuid.uuid4())

    def info() -> ProjectInfo:
        return ProjectInfo(w.project_id, w.user_id, "t", 300.0, 1920, 1080, 30.0, "k", len(w.versions), "tk", None)

    monkeypatch.setattr(edit_graph, "load_project", lambda tx, pid, uid: info())
    monkeypatch.setattr(edit_graph, "current_edl", lambda tx, pid, v: w.versions[v - 1] if v else None)
    monkeypatch.setattr(edit_graph, "load_transcript", lambda i: transcript)
    monkeypatch.setattr(edit_graph, "load_detections", lambda i: None)

    def existing_reply(tx: Any, pid: uuid.UUID, mid: uuid.UUID, thread: Any = None) -> Any:
        return next((r for r in w.replies if r["payload"].get("reply_to") == str(mid)), None)

    def post_message(tx: Any, pid: uuid.UUID, content: str, **kw: Any) -> Any:
        w.replies.append({"content": content, "kind": kw.get("kind"), "payload": kw.get("payload") or {}})

    def save_plan(
        tx: Any, pid: uuid.UUID, edl: EDL, *, base_version: int, reply: str, payload: dict[str, Any], allow_on_top: bool
    ) -> Any:
        if len(w.versions) != base_version and not allow_on_top:
            raise store.PlanConflict(len(w.versions))
        w.versions.append(edl.model_copy(update={"version": len(w.versions) + 1}))
        w.replies.append({"content": reply, "kind": "text", "payload": payload})

    monkeypatch.setattr(store, "existing_reply", existing_reply)
    monkeypatch.setattr(store, "post_message", post_message)
    monkeypatch.setattr(store, "save_plan", save_plan)
    monkeypatch.setattr(store, "learn_enabled", lambda tx, uid: True)
    monkeypatch.setattr(store, "record_feedback", lambda *a, **k: None)
    monkeypatch.setattr(store, "save_standing_instruction", lambda *a, **k: None)
    return w


def _deps(world: World, llm: LLMClient, stages: list[str]) -> EditDeps:
    @contextmanager
    def session() -> Any:
        yield FakeTx(world)

    class Hit:
        def __init__(self) -> None:
            self.cue_from, self.cue_to, self.text, self.score = "c5", "c7", "hiring talk", 1.0

    return EditDeps(
        llm=llm,
        call=CallContext(world.user_id, world.project_id, uuid.uuid4(), "agent"),
        session=session,  # type: ignore[arg-type]
        progress=lambda stage, pct: stages.append(stage),
        retrieve=lambda tx, pid, qs: [Hit()],
        model_id="test-main",
    )


def _turn(app: Any, world: World, mid: uuid.UUID, deps: EditDeps) -> str:
    fresh = {"project_id": str(world.project_id), "user_id": str(world.user_id), "message_id": str(mid)}
    return run_turn(app, str(world.project_id), str(mid), deps, fresh)


def test_idea_plan_then_revision(world: World, llm: LLMClient, fake_model: Any) -> None:
    app = build_graph().compile(checkpointer=InMemorySaver())
    stages: list[str] = []
    deps = _deps(world, llm, stages)
    idea = {
        "title": "Pricing",
        "target_duration_s": 30,
        "aspect": "9:16",
        "style": "calm",
        "focus": None,
        "anchors": [{"cue_from": "c2", "cue_to": "c4"}],
    }
    fake_model.script["CandidatesOut"] = [
        {
            "candidates": [
                {"cue_from": "c10", "cue_to": "c11", "reason": "extra", "score": 0.6},
                {"cue_from": "c20", "cue_to": "c21", "reason": "more", "score": 0.5},
            ]
        }
    ]
    fake_model.script["SelectionOut"] = [{"order": [0, 1]}]
    fake_model.script["CriticOut"] = [{"ok": True, "problems": []}]

    first = world.say("Make this: Pricing", suggestion=idea)
    assert _turn(app, world, first, deps) == "done"
    assert "Intent" not in [name for name, _ in fake_model.calls]  # the idea is a ready intent
    assert len(world.versions) == 1 and world.replies[-1]["payload"]["reply_to"] == str(first)
    plan = world.versions[0]
    assert 27 <= sum(s.length for s in plan.segments) - 0.5 * (len(plan.segments) - 1) <= 33
    assert {"understanding", "finding moments", "building plan", "checking", "saving"} <= set(stages)

    # The next message resumes at await_feedback and goes through edit ops, not a new plan.
    drop = plan.segments[-1].id
    fake_model.script["Intent"] = [{"kind": "correction", "targets": [drop]}]
    fake_model.script["RevisionOut"] = [{"ops": [{"op": "remove", "id": drop}], "summary": "removed"}]
    second = world.say(f"drop {drop}")
    assert _turn(app, world, second, deps) == "done"
    assert len(world.versions) == 2 and drop not in [s.id for s in world.versions[1].segments]
    assert world.replies[-1]["payload"]["changes"] == [f"Removed clip {drop}."]


def test_question_asks_then_answers_with_citations(world: World, llm: LLMClient, fake_model: Any) -> None:
    app = build_graph().compile(checkpointer=InMemorySaver())
    deps = _deps(world, llm, [])
    fake_model.script["Intent"] = [
        {"kind": "question", "question": "Which part do you mean?", "choices": ["Hiring", "Pricing"]},
        {"kind": "question", "question": "Still unsure?", "choices": ["a", "b"]},  # cleared: already asked once
    ]
    first = world.say("where do I talk about it?")
    assert _turn(app, world, first, deps) == "waiting_input"
    assert world.replies[-1]["payload"]["choices"] == ["Hiring", "Pricing"]

    fake_model.script["Queries"] = [{"queries": ["hiring"]}]
    fake_model.script["AnswerOut"] = [{"found": True, "answer": "Around 0:36.", "cue_ids": ["c7"]}]
    fake_model.script["VerifyOut"] = [{"supported": True}]
    answer = world.say("Hiring")
    assert _turn(app, world, answer, deps) == "done"
    last = world.replies[-1]
    assert last["content"] == "Around 0:36." and last["payload"]["citations"][0]["cue_id"] == "c7"
    assert last["payload"]["reply_to"] == str(answer)


def test_repeated_run_does_not_post_twice(world: World, llm: LLMClient, fake_model: Any) -> None:
    app = build_graph().compile(checkpointer=InMemorySaver())
    deps = _deps(world, llm, [])
    fake_model.script["Intent"] = [{"kind": "metadata"}]
    mid = world.say("write me a title")
    _turn(app, world, mid, deps)
    # The same message again (a reclaimed job) resumes at await_feedback; the reply guard keeps one reply.
    fake_model.script["Intent"] = [{"kind": "metadata"}]
    _turn(app, world, mid, deps)
    assert sum(1 for r in world.replies if r["payload"].get("reply_to") == str(mid)) == 1
