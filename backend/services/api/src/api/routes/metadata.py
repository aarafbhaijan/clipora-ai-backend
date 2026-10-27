"""Posting metadata: start the graph, chat with it, read versions, save user edits (architecture.md 11.3)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import ValidationError
from sqlalchemy import select

from api.access import own_project
from api.auth import Me
from api.deps import Tx
from api.schemas import (
    MessageOut,
    MetadataMessageIn,
    MetadataOut,
    MetadataPatchIn,
    MetadataSetOut,
    MetadataStartIn,
)
from api.services.jobs import active_job
from api.views import job_out, message_out
from core.enums import ACTIVE_JOB_STATUSES, Author, JobKind, MessageKind, MessageRole, Thread
from core.errors import AppError, ErrorCode
from core.jobs import MetadataPayload
from core.metadata import MetadataFields, field_issues
from db.ids import uuid7
from db.models import Job, Message, MetadataSet
from jobqueue import enqueue, requeue_waiting

router = APIRouter(tags=["metadata"])


def _set_out(m: MetadataSet) -> MetadataSetOut:
    return MetadataSetOut(
        version=m.version,
        edl_version=m.edl_version,
        platform=m.platform,
        language=m.language,
        tone=m.tone,
        fields=m.fields,
        author=m.author,
        created_at=m.created_at,
    )


@router.post("/projects/{project_id}/metadata", status_code=202)
def start_metadata(project_id: uuid.UUID, body: MetadataStartIn, user: Me, tx: Tx) -> MessageOut:
    p = own_project(tx, user, project_id, lock=True)
    if not p.current_version:
        raise AppError(ErrorCode.no_plan, "Make a plan before writing the title and hashtags.")
    if active_job(tx, p.id, JobKind.metadata.value) is not None:
        raise AppError(ErrorCode.metadata_busy, "Clipora is already writing the text for this video.")
    options = {k: v for k, v in body.model_dump().items() if v}
    message = Message(
        id=uuid7(),
        project_id=p.id,
        thread=Thread.metadata.value,
        role=MessageRole.user.value,
        kind=MessageKind.text.value,
        content="Write title and hashtags",
        payload={"options": options},
        edl_version=p.current_version,
    )
    tx.add(message)
    tx.flush()
    enqueue(
        tx,
        JobKind.metadata,
        MetadataPayload(
            project_id=p.id, message_id=message.id, edl_version=p.current_version, options=options
        ).model_dump(mode="json"),
        user.id,
        p.id,
    )
    return message_out(message)


@router.post("/projects/{project_id}/metadata/messages", status_code=202)
def metadata_message(project_id: uuid.UUID, body: MetadataMessageIn, user: Me, tx: Tx) -> MessageOut:
    p = own_project(tx, user, project_id, lock=True)
    active = active_job(tx, p.id, JobKind.metadata.value)
    if active is not None and active.status != "waiting_input":
        raise AppError(ErrorCode.metadata_busy, "Clipora is still working on the text.")
    latest = tx.execute(
        select(MetadataSet).where(MetadataSet.project_id == p.id).order_by(MetadataSet.version.desc()).limit(1)
    ).scalar_one_or_none()
    if active is None and latest is None:
        raise AppError(ErrorCode.no_plan, "Start with Write title and hashtags.")
    message = Message(
        id=uuid7(),
        project_id=p.id,
        thread=Thread.metadata.value,
        role=MessageRole.user.value,
        kind=MessageKind.text.value,
        content=body.content,
        payload={},
        edl_version=p.current_version or None,
    )
    tx.add(message)
    tx.flush()
    if active is not None:
        requeue_waiting(tx, active.id, {"message_id": str(message.id)})
    else:
        enqueue(
            tx,
            JobKind.metadata,
            MetadataPayload(project_id=p.id, message_id=message.id, edl_version=p.current_version).model_dump(
                mode="json"
            ),
            user.id,
            p.id,
        )
    return message_out(message)


@router.get("/projects/{project_id}/metadata", response_model=MetadataOut)
def get_metadata(project_id: uuid.UUID, user: Me, tx: Tx) -> MetadataOut:
    p = own_project(tx, user, project_id)
    sets = list(
        tx.execute(
            select(MetadataSet).where(MetadataSet.project_id == p.id).order_by(MetadataSet.version.desc()).limit(50)
        ).scalars()
    )
    messages = tx.execute(
        select(Message)
        .where(Message.project_id == p.id, Message.thread == Thread.metadata.value)
        .order_by(Message.created_at)
    ).scalars()
    active = tx.execute(
        select(Job).where(
            Job.project_id == p.id, Job.kind == JobKind.metadata.value, Job.status.in_(ACTIVE_JOB_STATUSES)
        )
    ).scalar_one_or_none()
    current = sets[0] if sets else None
    return MetadataOut(
        current=_set_out(current) if current else None,
        versions=[_set_out(s) for s in sets],
        messages=[message_out(m) for m in messages],
        active_job=job_out(active),
        stale=bool(current and current.edl_version != p.current_version),
    )


@router.patch("/projects/{project_id}/metadata", response_model=MetadataSetOut)
def save_metadata(
    project_id: uuid.UUID,
    body: MetadataPatchIn,
    user: Me,
    tx: Tx,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> MetadataSetOut:
    p = own_project(tx, user, project_id, lock=True)
    current = tx.execute(
        select(MetadataSet).where(MetadataSet.project_id == p.id).order_by(MetadataSet.version.desc()).limit(1)
    ).scalar_one_or_none()
    if current is None or current.version != if_match:
        raise AppError(
            ErrorCode.version_conflict,
            "The text changed while you were editing.",
            {
                "version": current.version if current else 0,
                "latest": _set_out(current).model_dump(mode="json") if current else None,
            },
        )
    try:
        fields = MetadataFields.model_validate(body.fields)
    except ValidationError as e:
        raise AppError(ErrorCode.fields_invalid, "Some fields are not valid.", {"issues": e.errors()}) from e
    issues = field_issues(fields)
    if issues:
        raise AppError(ErrorCode.fields_invalid, "Some fields are not valid.", {"issues": issues})
    new = MetadataSet(
        id=uuid7(),
        project_id=p.id,
        version=current.version + 1,
        edl_version=current.edl_version,
        export_id=current.export_id,
        platform=body.platform or fields.platform.platform,
        language=current.language,
        tone=current.tone,
        fields=fields.model_dump(mode="json"),
        author=Author.user.value,
        parent_version=current.version,
    )
    tx.add(new)
    tx.flush()
    return _set_out(new)
