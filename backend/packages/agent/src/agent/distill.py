"""Learning (architecture.md 11.4): feedback events -> short preference statements of fixed kinds.

An inferred statement needs 3 events across 2 projects and becomes `pending`; the UI asks once
before it is active. Standing instructions are saved active by the edit graph, not here.
Nothing happens when the user has switched learning off.
"""

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent import prompts, store
from agent.llm import CallContext, LLMClient, data_block
from core.enums import MemoryKind, MemoryStatus
from db.ids import uuid7
from db.models import AgentMemory, FeedbackEvent

MIN_EVENTS = 3
MIN_PROJECTS = 2
EVENT_WINDOW = 200


class Statement(BaseModel):
    kind: MemoryKind
    text: str = Field(max_length=200)
    event_ids: list[str] = Field(max_length=50)
    merge_into: str | None = None


class DistillOut(BaseModel):
    statements: list[Statement] = Field(max_length=10)


@dataclass(frozen=True)
class EventRef:
    id: str
    project_id: str | None


@dataclass
class Decision:
    create: list[tuple[Statement, list[str]]] = field(default_factory=list)
    merge: list[tuple[str, list[str]]] = field(default_factory=list)  # memory id, new event ids


def decide(statements: list[Statement], events: dict[str, EventRef], memories: dict[str, set[str]]) -> Decision:
    """Pure rules. `memories` maps memory id -> event ids it already counts."""
    d = Decision()
    for st in statements:
        ids = [i for i in dict.fromkeys(st.event_ids) if i in events]
        if st.merge_into and st.merge_into in memories:
            new = [i for i in ids if i not in memories[st.merge_into]]
            if new:
                d.merge.append((st.merge_into, new))
            continue
        projects = {events[i].project_id for i in ids if events[i].project_id}
        if len(ids) >= MIN_EVENTS and len(projects) >= MIN_PROJECTS:
            d.create.append((st, ids))
    return d


def _event_line(e: FeedbackEvent) -> str:
    diff = json.dumps(e.diff, default=str) if e.diff else ""
    return f"{e.id} | {e.target_kind} | {e.signal} | {e.reason or ''} | {diff}"


def distill(
    session: Callable[[], AbstractContextManager[Session]],
    llm: LLMClient,
    ctx: CallContext,
    user_id: uuid.UUID,
    embed: Callable[[str], list[float]] | None,
) -> int:
    """Returns how many memories were created or updated."""
    with session() as tx:
        if not store.learn_enabled(tx, user_id):
            return 0
        events = list(
            tx.execute(
                select(FeedbackEvent)
                .where(FeedbackEvent.user_id == user_id)
                .order_by(FeedbackEvent.created_at.desc())
                .limit(EVENT_WINDOW)
            ).scalars()
        )
        memories = list(tx.execute(select(AgentMemory).where(AgentMemory.user_id == user_id)).scalars())
        event_refs = {str(e.id): EventRef(str(e.id), str(e.project_id) if e.project_id else None) for e in events}
        lines = "\n".join(_event_line(e) for e in events)
        known = "\n".join(f"{m.id} | {m.kind} | {m.status} | {m.text}" for m in memories)
        consumed = {str(m.id): set(m.source_event_ids or []) for m in memories}
    if len(events) < MIN_EVENTS:
        return 0
    out = llm.structured(
        ctx,
        "distill",
        DistillOut,
        prompts.DISTILL,
        data_block("feedback_events", lines) + "\n" + data_block("existing_preferences", known or "none"),
        tier="small",
    )
    decision = decide(out.statements, event_refs, consumed)
    changed = 0
    with session() as tx:
        for memory_id, new_ids in decision.merge:
            m = tx.get(AgentMemory, uuid.UUID(memory_id))
            if m is None or m.user_id != user_id:
                continue
            m.evidence_count += len(new_ids)
            m.source_event_ids = [*(m.source_event_ids or []), *new_ids]
            changed += 1
        for st, ids in decision.create:
            tx.add(
                AgentMemory(
                    id=uuid7(),
                    user_id=user_id,
                    kind=st.kind.value,
                    text=st.text.strip(),
                    embedding=embed(st.text) if embed else None,
                    evidence_count=len(ids),
                    status=MemoryStatus.pending.value,
                    source_event_ids=ids,
                )
            )
            changed += 1
    return changed
