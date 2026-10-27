"""Upload, history list, project detail, rename, delete, retry."""

import base64
import math
import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select, update

import storage
from api.access import own_project
from api.assets import assets_from, ingest_markers, poster_url
from api.auth import Me
from api.deps import Tx
from api.schemas import (
    CreateProjectIn,
    CreateProjectOut,
    JobOut,
    PartUrlsIn,
    PartUrlsOut,
    ProjectDetail,
    ProjectListItem,
    ProjectPage,
    RenameIn,
)
from api.services.quotas import MB, check_new_upload, file_too_large, limits_for
from api.views import (
    export_brief,
    history_state,
    ideas_posted,
    job_out,
    jobs_by_project,
    last_exports,
    message_out,
    split_jobs,
)
from core.edl import EDL
from core.enums import JobKind, ProjectStatus
from core.errors import AppError, ErrorCode
from core.jobs import IngestPayload
from core.settings import LimitKey
from db.ids import uuid7
from db.models import EdlVersion, Job, Message, Project
from jobqueue import enqueue, retry_failed

router = APIRouter(tags=["projects"])

PAGE_SIZE = 30


@router.post("/projects", response_model=CreateProjectOut, status_code=201)
def create_project(body: CreateProjectIn, user: Me, tx: Tx) -> CreateProjectOut:
    if not body.content_type.startswith("video/"):
        raise AppError(ErrorCode.unsupported_media, "Please choose a video file.")
    check_new_upload(tx, user.id, body.size, body.duration_s)

    project_id = uuid7()
    key = storage.source_key(user.id, project_id, body.filename)
    upload_id = storage.start_multipart(key, body.content_type)
    if body.srt_text:
        storage.put_bytes(storage.user_srt_key(user.id, project_id), body.srt_text.encode(), "application/x-subrip")
    tx.add(
        Project(
            id=project_id,
            user_id=user.id,
            title=body.title,
            status=ProjectStatus.uploading.value,
            source_key=key,
            upload_id=upload_id,
            size_bytes=body.size,
            duration_s=body.duration_s,
        )
    )
    part_bytes = storage.PART_SIZE_MB * MB
    return CreateProjectOut(
        project_id=project_id,
        upload_id=upload_id,
        part_size_mb=storage.PART_SIZE_MB,
        part_count=max(1, math.ceil(body.size / part_bytes)),
    )


@router.post("/projects/{project_id}/upload/part-urls", response_model=PartUrlsOut)
def part_urls(project_id: uuid.UUID, body: PartUrlsIn, user: Me, tx: Tx) -> PartUrlsOut:
    p = own_project(tx, user, project_id)
    if p.status != ProjectStatus.uploading or not p.upload_id:
        raise AppError(ErrorCode.bad_request, "This upload has already finished.")
    if any(n < 1 or n > 10000 for n in body.part_numbers):
        raise AppError(ErrorCode.bad_request, "Part numbers must be 1 to 10000.")
    return PartUrlsOut(urls=storage.part_urls(p.source_key, p.upload_id, body.part_numbers))


@router.post("/projects/{project_id}/uploaded", response_model=JobOut)
def uploaded(project_id: uuid.UUID, user: Me, tx: Tx) -> JobOut:
    p = own_project(tx, user, project_id, lock=True)
    if p.status != ProjectStatus.uploading:
        job = (
            tx.execute(select(Job).where(Job.project_id == p.id, Job.kind == "ingest").order_by(Job.created_at.desc()))
            .scalars()
            .first()
        )
        if job is None:
            raise AppError(ErrorCode.bad_request, "This upload was not started.")
        out = job_out(job)
        assert out is not None
        return out
    assert p.upload_id is not None
    size = storage.complete_multipart(p.source_key, p.upload_id)
    max_file = limits_for(tx, user.id)[LimitKey.max_file_size_mb]
    if size > max_file * MB:
        # Return instead of raising so the failed state commits with this transaction.
        storage.delete_prefix(p.source_key)
        p.status = ProjectStatus.failed.value
        p.upload_id = None
        err = file_too_large(max_file, size)
        return JSONResponse(err.envelope(), status_code=err.http_status)  # type: ignore[return-value]
    p.size_bytes = size
    p.upload_id = None
    p.status = ProjectStatus.ingesting.value
    user_srt = storage.exists(storage.user_srt_key(user.id, p.id))
    job = enqueue(
        tx,
        JobKind.ingest,
        IngestPayload(project_id=p.id, user_srt=user_srt).model_dump(mode="json"),
        user.id,
        p.id,
    )
    out = job_out(job)
    assert out is not None
    return out


def _encode_cursor(created_at: datetime, project_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{project_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created, pid = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(created), uuid.UUID(pid)
    except (ValueError, UnicodeDecodeError) as e:
        raise AppError(ErrorCode.bad_request, "Bad cursor.") from e


@router.get("/projects", response_model=ProjectPage)
def list_projects(user: Me, tx: Tx, cursor: str | None = Query(default=None)) -> ProjectPage:
    q = select(Project).where(Project.user_id == user.id, Project.deleted_at.is_(None))
    if cursor:
        created, pid = _decode_cursor(cursor)
        q = q.where(or_(Project.created_at < created, and_(Project.created_at == created, Project.id < pid)))
    rows = list(tx.execute(q.order_by(Project.created_at.desc(), Project.id.desc()).limit(PAGE_SIZE + 1)).scalars())
    page, more = rows[:PAGE_SIZE], len(rows) > PAGE_SIZE
    ids = [p.id for p in page]
    jobs = jobs_by_project(tx, ids)
    exports = last_exports(tx, ids)
    ideas = ideas_posted(tx, ids)
    items = []
    for p in page:
        active, failed = split_jobs(jobs.get(p.id, []))
        items.append(
            ProjectListItem(
                id=p.id,
                title=p.title,
                status=p.status,
                state=history_state(p, active, failed, p.id in ideas, exports.get(p.id)),
                created_at=p.created_at,
                updated_at=p.updated_at,
                duration_s=p.duration_s,
                thumbnail_url=poster_url(ingest_markers(tx, p.id)) if p.status == "ready" else None,
                active_job=job_out(active),
                failed_job=job_out(failed),
                last_export=export_brief(exports.get(p.id)),
            )
        )
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if more and page else None
    return ProjectPage(items=items, next_cursor=next_cursor)


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: uuid.UUID, user: Me, tx: Tx) -> ProjectDetail:
    p = own_project(tx, user, project_id)
    active, failed = split_jobs(jobs_by_project(tx, [p.id]).get(p.id, []))
    last_export = last_exports(tx, [p.id]).get(p.id)
    messages = list(
        tx.execute(
            select(Message).where(Message.project_id == p.id, Message.thread == "edit").order_by(Message.created_at)
        ).scalars()
    )
    edl = None
    if p.current_version:
        row = tx.execute(
            select(EdlVersion).where(EdlVersion.project_id == p.id, EdlVersion.version == p.current_version)
        ).scalar_one()
        edl = EDL.model_validate(row.edl)
    has_ideas = any(m.kind == "suggestions" for m in messages)
    return ProjectDetail(
        id=p.id,
        title=p.title,
        status=p.status,
        state=history_state(p, active, failed, has_ideas, last_export),
        duration_s=p.duration_s,
        width=p.width,
        height=p.height,
        fps=p.fps,
        current_version=p.current_version,
        created_at=p.created_at,
        updated_at=p.updated_at,
        active_job=job_out(active),
        failed_job=job_out(failed),
        last_export=export_brief(last_export),
        messages=[message_out(m) for m in messages],
        edl=edl,
        assets=assets_from(ingest_markers(tx, p.id), p.source_key) if p.status == "ready" else assets_from({}),
    )


@router.patch("/projects/{project_id}", status_code=204)
def rename_project(project_id: uuid.UUID, body: RenameIn, user: Me, tx: Tx) -> None:
    own_project(tx, user, project_id).title = body.title


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, user: Me, tx: Tx) -> None:
    """Soft delete. The slot and storage are free at once; S3 is purged later, never in this request."""
    p = own_project(tx, user, project_id, lock=True)
    p.deleted_at = datetime.now().astimezone()


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: uuid.UUID, user: Me, tx: Tx) -> JobOut:
    job = tx.execute(select(Job).where(Job.id == job_id, Job.user_id == user.id)).scalar_one_or_none()
    if job is None:
        raise AppError(ErrorCode.not_found, "Nothing to retry.")
    if job.error_code == ErrorCode.duration_too_long:
        raise AppError(ErrorCode.bad_request, "This video is too long to process. Delete it instead.")
    if not retry_failed(tx, job.id):
        raise AppError(ErrorCode.bad_request, "This task is not failed.")
    if job.kind == "ingest" and job.project_id:
        tx.execute(
            update(Project)
            .where(Project.id == job.project_id, Project.user_id == user.id)
            .values(status=ProjectStatus.ingesting.value)
        )
    tx.refresh(job)
    out = job_out(job)
    assert out is not None
    return out
