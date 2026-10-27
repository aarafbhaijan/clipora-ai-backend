"""Exports: starting a render of a plan version, and reporting its state with download links."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

import storage
from api.schemas import ExportOut
from api.services.jobs import active_job
from api.services.plans import version_row
from core.enums import JobKind
from core.errors import AppError, ErrorCode
from core.jobs import ExportPayload
from db.ids import uuid7
from db.models import Export, Job, Project
from jobqueue import enqueue


def export_out(tx: Session, e: Export) -> ExportOut:
    job = tx.get(Job, e.job_id) if e.job_id else None
    done = e.status == "done"
    # A timeout or lease sweep fails the job without the handler updating this row.
    status = "failed" if not done and job is not None and job.status == "failed" else e.status
    return ExportOut(
        id=e.id,
        project_id=e.project_id,
        edl_version=e.edl_version,
        status=status,
        percent=100 if done else (job.percent if job else 0),
        stage=job.stage if job else None,
        error_code=job.error_code if job and status == "failed" else None,
        download_url=storage.asset_url(e.output_key, "clipora-short.mp4") if done and e.output_key else None,
        srt_url=storage.asset_url(e.srt_key, "clipora-short.srt") if done and e.srt_key else None,
        created_at=e.created_at,
    )


def start_export(tx: Session, user_id: uuid.UUID, project: Project, edl_version: int | None) -> Export:
    """Create the export row and its job in one transaction. One export at a time per video."""
    if not project.current_version:
        raise AppError(ErrorCode.no_plan, "Make a plan before exporting.")
    version = edl_version or project.current_version
    if version_row(tx, project.id, version) is None:
        raise AppError(ErrorCode.not_found, "That version does not exist.")
    if active_job(tx, project.id, JobKind.export.value) is not None:
        raise AppError(ErrorCode.export_in_progress, "An export is already running for this video.")
    export = Export(id=uuid7(), project_id=project.id, edl_version=version, status="queued")
    tx.add(export)
    tx.flush()
    job = enqueue(
        tx,
        JobKind.export,
        ExportPayload(project_id=project.id, export_id=export.id, edl_version=version).model_dump(mode="json"),
        user_id,
        project.id,
    )
    export.job_id = job.id
    return export


def owned_export(tx: Session, user_id: uuid.UUID, export_id: uuid.UUID) -> Export:
    e = tx.execute(
        select(Export)
        .join(Project, Project.id == Export.project_id)
        .where(Export.id == export_id, Project.user_id == user_id, Project.deleted_at.is_(None))
    ).scalar_one_or_none()
    if e is None:
        raise AppError(ErrorCode.not_found, "This export does not exist.")
    return e
