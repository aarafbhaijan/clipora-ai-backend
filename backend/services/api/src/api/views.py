"""Turn rows into response bodies, and derive the history state chip (design.md section 6)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas import ExportBrief, HistoryState, JobOut, MessageOut
from core.enums import ACTIVE_JOB_STATUSES
from db.models import Export, Job, Message, Project


def job_out(job: Job | None) -> JobOut | None:
    if job is None:
        return None
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        stage=job.stage,
        percent=job.percent,
        error_code=job.error_code,
        error_detail=None,
        created_at=job.created_at,
    )


def message_out(m: Message) -> MessageOut:
    return MessageOut(
        id=m.id,
        thread=m.thread,
        role=m.role,
        kind=m.kind,
        content=m.content,
        payload=m.payload,
        edl_version=m.edl_version,
        created_at=m.created_at,
    )


def export_brief(e: Export | None) -> ExportBrief | None:
    return ExportBrief(id=e.id, status=e.status, created_at=e.created_at) if e else None


def jobs_by_project(tx: Session, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Job]]:
    """Active jobs and the latest failed job per project, newest first."""
    if not project_ids:
        return {}
    rows = tx.execute(
        select(Job)
        .where(Job.project_id.in_(project_ids))
        .where(Job.status.in_([*ACTIVE_JOB_STATUSES, "failed"]))
        .order_by(Job.created_at.desc())
    ).scalars()
    out: dict[uuid.UUID, list[Job]] = {}
    for j in rows:
        assert j.project_id is not None
        out.setdefault(j.project_id, []).append(j)
    return out


def last_exports(tx: Session, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, Export]:
    if not project_ids:
        return {}
    rows = tx.execute(
        select(Export)
        .where(Export.project_id.in_(project_ids))
        .distinct(Export.project_id)
        .order_by(Export.project_id, Export.created_at.desc())
    ).scalars()
    return {e.project_id: e for e in rows}


def ideas_posted(tx: Session, project_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not project_ids:
        return set()
    return set(
        tx.execute(
            select(Message.project_id)
            .where(Message.project_id.in_(project_ids), Message.kind == "suggestions")
            .distinct()
        ).scalars()
    )


# Kinds shown in the chip, most important first.
_PRIORITY = ("export", "agent", "metadata", "ingest", "suggest")


def split_jobs(jobs: list[Job]) -> tuple[Job | None, Job | None]:
    """(active job to show, latest failed job if it is newer than any success)."""
    active = [j for j in jobs if j.status in ACTIVE_JOB_STATUSES]
    active.sort(key=lambda j: _PRIORITY.index(j.kind) if j.kind in _PRIORITY else 99)
    failed = next((j for j in jobs if j.status == "failed" and j.kind in ("ingest", "export", "agent")), None)
    return (active[0] if active else None), failed


def history_state(
    p: Project, active: Job | None, failed: Job | None, has_ideas: bool, last_export: Export | None
) -> HistoryState:
    if p.status == "uploading":
        return "uploading"
    if active is not None:
        if active.status == "waiting_input":
            return "needs_answer"
        if active.status == "queued" and active.kind == "ingest":
            return "waiting"
        return {
            "ingest": "processing",
            "suggest": "finding_ideas",
            "agent": "planning",
            "metadata": "planning",
            "export": "exporting",
        }.get(active.kind, "processing")  # type: ignore[return-value]
    if p.status == "failed" or failed is not None and failed.kind == "ingest":
        return "failed"
    if p.current_version == 0:
        return "pick_idea" if has_ideas else "ready"
    if last_export is not None and last_export.status == "done":
        return "exported"
    return "ready"


def payload_of(job: Job) -> dict[str, Any]:
    return dict(job.payload or {})
