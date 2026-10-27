"""Database writes made by the agent. Plans and the reply that announces them commit together."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.edl import EDL
from core.enums import Author, MemoryKind, MemoryStatus, MessageKind, MessageRole, Thread
from db.ids import uuid7
from db.models import AgentMemory, EdlVersion, FeedbackEvent, Message, Project, User

MAX_ACTIVE_MEMORIES = 50


class PlanConflict(Exception):
    """The plan changed (a user edit) after the agent read it."""

    def __init__(self, current_version: int) -> None:
        super().__init__(f"plan is now version {current_version}")
        self.current_version = current_version


def existing_reply(
    tx: Session, project_id: uuid.UUID, reply_to: uuid.UUID, thread: Thread = Thread.edit
) -> Message | None:
    """The assistant reply already written for this user message (a repeated run must not post twice)."""
    return tx.execute(
        select(Message)
        .where(
            Message.project_id == project_id,
            Message.thread == thread.value,
            Message.role == MessageRole.assistant.value,
            Message.payload["reply_to"].astext == str(reply_to),
        )
        .limit(1)
    ).scalar_one_or_none()


def post_message(
    tx: Session,
    project_id: uuid.UUID,
    content: str,
    *,
    kind: MessageKind = MessageKind.text,
    thread: Thread = Thread.edit,
    payload: dict[str, Any] | None = None,
    edl_version: int | None = None,
) -> Message:
    m = Message(
        id=uuid7(),
        project_id=project_id,
        thread=thread.value,
        role=MessageRole.assistant.value,
        kind=kind.value,
        content=content,
        payload=payload or {},
        edl_version=edl_version,
    )
    tx.add(m)
    tx.flush()
    return m


@dataclass(frozen=True)
class SavedPlan:
    version: int
    message_id: uuid.UUID


def save_plan(
    tx: Session,
    project_id: uuid.UUID,
    edl: EDL,
    *,
    base_version: int,
    reply: str,
    payload: dict[str, Any],
    allow_on_top: bool,
) -> SavedPlan:
    """New EDL version, projects.current_version and the reply in one transaction.

    If the plan moved since `base_version`: a fresh plan (the user asked for a new one) is saved on top,
    the previous version stays in history; a revision raises PlanConflict so ops are reapplied.
    """
    p = tx.execute(select(Project).where(Project.id == project_id).with_for_update()).scalar_one()
    if p.current_version != base_version and not allow_on_top:
        raise PlanConflict(p.current_version)
    version = p.current_version + 1
    saved = edl.model_copy(update={"version": version})
    tx.add(
        EdlVersion(
            id=uuid7(),
            project_id=project_id,
            version=version,
            edl=saved.to_json(),
            author=Author.agent.value,
            parent_version=p.current_version or None,
        )
    )
    p.current_version = version
    message = post_message(
        tx,
        project_id,
        reply,
        payload={**payload, "previous_version": base_version or None},
        edl_version=version,
    )
    return SavedPlan(version=version, message_id=message.id)


def learn_enabled(tx: Session, user_id: uuid.UUID) -> bool:
    u = tx.get(User, user_id)
    return bool(u and u.learn_enabled)


def record_feedback(
    tx: Session,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    *,
    target_kind: str,
    target_ref: str,
    signal: str,
    reason: str | None = None,
) -> None:
    """Learning signals are written only when the user allows learning."""
    if not learn_enabled(tx, user_id):
        return
    tx.add(
        FeedbackEvent(
            id=uuid7(),
            user_id=user_id,
            project_id=project_id,
            target_kind=target_kind,
            target_ref=target_ref,
            signal=signal,
            reason=reason,
        )
    )


def save_standing_instruction(
    tx: Session, user_id: uuid.UUID, kind: MemoryKind, text: str, embedding: list[float] | None
) -> AgentMemory | None:
    """A stated preference is active at once. Over the cap, the least recently used one is disabled."""
    if not learn_enabled(tx, user_id):
        return None
    active = list(
        tx.execute(
            select(AgentMemory)
            .where(AgentMemory.user_id == user_id, AgentMemory.status == MemoryStatus.active.value)
            .with_for_update()
        ).scalars()
    )
    same = next((m for m in active if m.kind == kind.value and m.text.strip().lower() == text.strip().lower()), None)
    if same is not None:
        same.evidence_count += 1
        return same
    if len(active) >= MAX_ACTIVE_MEMORIES:
        oldest = min(active, key=lambda m: m.last_used_at or m.updated_at)
        oldest.status = MemoryStatus.disabled.value
    memory = AgentMemory(
        id=uuid7(),
        user_id=user_id,
        kind=kind.value,
        text=text.strip()[:300],
        embedding=embedding,
        evidence_count=1,
        status=MemoryStatus.active.value,
        source_event_ids=[],
    )
    tx.add(memory)
    return memory
