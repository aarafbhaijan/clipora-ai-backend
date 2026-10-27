"""The edit chat: posting a user message and starting (or resuming) the agent, and asking for more ideas."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.services.jobs import active_job
from core.enums import JobKind, MessageKind, MessageRole, Thread
from core.errors import AppError, ErrorCode
from core.jobs import AgentPayload, SuggestPayload
from db.ids import uuid7
from db.models import Job, Message, Project
from jobqueue import JobBusy, enqueue, requeue_waiting


def find_suggestion(tx: Session, project_id: uuid.UUID, suggestion_id: str) -> dict[str, object]:
    for m in tx.execute(
        select(Message).where(Message.project_id == project_id, Message.kind == MessageKind.suggestions.value)
    ).scalars():
        for idea in m.payload.get("ideas", []):
            if idea.get("id") == suggestion_id:
                return dict(idea)
    raise AppError(ErrorCode.not_found, "That idea is no longer available.")


def _require_ready(project: Project) -> None:
    if project.status != "ready":
        raise AppError(ErrorCode.bad_request, "This video is still being prepared.")


def post_user_message(
    tx: Session,
    user_id: uuid.UUID,
    project: Project,
    content: str,
    suggestion_id: str | None,
    choice: str | None,
) -> tuple[Message, Job]:
    """Save the message and, in the same transaction, start the agent or resume the one waiting for an answer."""
    _require_ready(project)
    text = content.strip() or (choice or "").strip()
    payload: dict[str, object] = {}
    if suggestion_id:
        idea = find_suggestion(tx, project.id, suggestion_id)
        payload["suggestion"] = idea
        text = text or f"Make this: {idea.get('title', '')}"
    if choice:
        payload["choice"] = choice
    if not text:
        raise AppError(ErrorCode.bad_request, "Type a message or pick an idea.")

    waiting = active_job(tx, project.id, JobKind.agent.value)
    if waiting is not None and waiting.status != "waiting_input":
        raise AppError(ErrorCode.agent_busy, "Clipora is still working on your last message.")

    message = Message(
        id=uuid7(),
        project_id=project.id,
        thread=Thread.edit.value,
        role=MessageRole.user.value,
        kind=MessageKind.text.value,
        content=text,
        payload=payload,
        edl_version=project.current_version or None,
    )
    tx.add(message)
    tx.flush()
    if waiting is not None:
        requeue_waiting(tx, waiting.id, {"message_id": str(message.id), "resume": True})
        tx.refresh(waiting)
        return message, waiting
    job = enqueue(
        tx,
        JobKind.agent,
        AgentPayload(project_id=project.id, message_id=message.id).model_dump(mode="json"),
        user_id,
        project.id,
    )
    return message, job


def request_more_ideas(tx: Session, user_id: uuid.UUID, project: Project) -> Job:
    """A suggest job that always posts, even after the user has chatted."""
    _require_ready(project)
    try:
        payload = SuggestPayload(project_id=project.id, more=True).model_dump(mode="json")
        return enqueue(tx, JobKind.suggest, payload, user_id, project.id)
    except JobBusy as e:
        raise AppError(ErrorCode.agent_busy, "Clipora is already finding ideas for this video.") from e
