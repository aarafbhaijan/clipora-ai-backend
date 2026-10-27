"""Feedback, transcript search, memories, the signed-in user, health."""

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, select, text

from api.access import own_project
from api.auth import Me
from api.deps import Tx
from api.schemas import FeedbackIn, MemoryOut, MemoryPatchIn, MeOut, MePatchIn, SearchHit, Usage
from api.services.learning import enqueue_distill
from api.services.quotas import limits_for, usage_for
from core.enums import Role
from core.errors import AppError, ErrorCode
from db.ids import uuid7
from db.models import AgentMemory, FeedbackEvent, TranscriptChunk, User

router = APIRouter(tags=["account"])


@router.post("/projects/{project_id}/feedback", status_code=204)
def feedback(project_id: uuid.UUID, body: FeedbackIn, user: Me, tx: Tx) -> None:
    p = own_project(tx, user, project_id)
    if not user.learn_enabled:
        return  # learning is off: log nothing
    tx.add(
        FeedbackEvent(
            id=uuid7(),
            user_id=user.id,
            project_id=p.id,
            target_kind=body.target_kind,
            target_ref=body.target_ref,
            signal=body.signal,
            reason=body.reason,
        )
    )
    enqueue_distill(tx, user.id)


@router.get("/projects/{project_id}/search", response_model=list[SearchHit])
def search(project_id: uuid.UUID, user: Me, tx: Tx, q: str = Query(min_length=1, max_length=200)) -> list[SearchHit]:
    """Full-text search only. Embedding search runs in the agent worker, never in the API."""
    p = own_project(tx, user, project_id)
    query = func.websearch_to_tsquery(text("'simple'::regconfig"), q)
    rows = tx.execute(
        select(TranscriptChunk)
        .where(TranscriptChunk.project_id == p.id, TranscriptChunk.tsv.op("@@")(query))
        .order_by(func.ts_rank(TranscriptChunk.tsv, query).desc())
        .limit(20)
    ).scalars()
    return [
        SearchHit(cue_from=r.cue_from, cue_to=r.cue_to, start_s=r.start_s, end_s=r.end_s, text=r.text) for r in rows
    ]


def _memory_out(m: AgentMemory) -> MemoryOut:
    return MemoryOut(
        id=m.id,
        kind=m.kind,
        text=m.text,
        status=m.status,
        evidence_count=m.evidence_count,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.get("/memories", response_model=list[MemoryOut])
def list_memories(user: Me, tx: Tx) -> list[MemoryOut]:
    rows = tx.execute(
        select(AgentMemory).where(AgentMemory.user_id == user.id).order_by(AgentMemory.updated_at.desc())
    ).scalars()
    return [_memory_out(m) for m in rows]


def _own_memory(tx: Tx, user_id: uuid.UUID, memory_id: uuid.UUID) -> AgentMemory:
    m = tx.execute(
        select(AgentMemory).where(AgentMemory.id == memory_id, AgentMemory.user_id == user_id)
    ).scalar_one_or_none()
    if m is None:
        raise AppError(ErrorCode.not_found, "That preference does not exist.")
    return m


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
def patch_memory(memory_id: uuid.UUID, body: MemoryPatchIn, user: Me, tx: Tx) -> MemoryOut:
    m = _own_memory(tx, user.id, memory_id)
    if body.text is not None:
        m.text = body.text
        m.embedding = None  # the agent worker re-embeds it on next use
    if body.status is not None:
        m.status = body.status
    tx.flush()
    tx.refresh(m)
    return _memory_out(m)


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: uuid.UUID, user: Me, tx: Tx) -> None:
    tx.delete(_own_memory(tx, user.id, memory_id))


def _me(tx: Tx, user_id: uuid.UUID) -> MeOut:
    u = tx.get(User, user_id)
    assert u is not None
    count, size = usage_for(tx, user_id)
    return MeOut(
        id=u.id,
        email=u.email,
        role=Role(u.role),
        learn_enabled=u.learn_enabled,
        limits=limits_for(tx, user_id),
        usage=Usage(active_videos=count, storage_bytes=size),
    )


@router.get("/me", response_model=MeOut)
def get_me(user: Me, tx: Tx) -> MeOut:
    return _me(tx, user.id)


@router.patch("/me", response_model=MeOut)
def patch_me(body: MePatchIn, user: Me, tx: Tx) -> MeOut:
    u = tx.get(User, user.id)
    assert u is not None
    u.learn_enabled = body.learn_enabled
    tx.flush()
    return _me(tx, user.id)


@router.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}
