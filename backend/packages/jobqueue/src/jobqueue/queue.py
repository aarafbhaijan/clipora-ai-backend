import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.enums import JobKind
from core.jobs import JOB_SPECS, LEASE_S, MAX_RUNNING_JOBS_PER_USER
from db.ids import uuid7
from db.models import Job

ERROR_DETAIL_MAX = 2000
CLAIM_CANDIDATES = 10


class JobBusy(Exception):
    """An active job of the same kind already exists for this project (or user)."""


class LeaseLost(Exception):
    """Another worker holds the job now. Stop and write nothing more."""


def enqueue(
    tx: Session,
    kind: JobKind,
    payload: dict[str, Any],
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
) -> Job:
    """Insert a queued job inside the caller's transaction. Never enqueue after the commit."""
    spec = JOB_SPECS[kind]
    job = Job(
        id=uuid7(),
        project_id=project_id,
        user_id=user_id,
        kind=kind.value,
        queue=spec.queue.value,
        payload=payload,
        status="queued",
        max_attempts=spec.max_attempts,
        timeout_s=spec.timeout_s,
    )
    try:
        with tx.begin_nested():
            tx.add(job)
            tx.flush()
    except IntegrityError as e:
        raise JobBusy(kind.value) from e
    return job


_CANDIDATES = text(
    """
    SELECT id, user_id FROM jobs
    WHERE queue = :queue AND attempts < max_attempts
      AND ((status = 'queued' AND run_after <= now())
        OR (status = 'running' AND lease_expires_at < now()))
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT :n
    """
)

_RUNNING_FOR_USER = text(
    """
    SELECT count(*) FROM jobs
    WHERE user_id = :user_id AND status = 'running' AND lease_expires_at >= now()
    """
)

_CLAIM = text(
    f"""
    UPDATE jobs
    SET status = 'running', locked_by = :worker, lease_expires_at = now() + interval '{LEASE_S} seconds',
        attempts = attempts + 1, started_at = coalesce(started_at, now())
    WHERE id = :id
    RETURNING id
    """
)


def claim(tx: Session, queue: str, worker: str) -> Job | None:
    """Claim one job for a free slot, skipping users who already run the maximum.

    Two workers could pass the per-user check together, so the claim first takes an advisory
    transaction lock on the user. Commit right after this call so the locks are released.
    """
    for job_id, user_id in tx.execute(_CANDIDATES, {"queue": queue, "n": CLAIM_CANDIDATES}).all():
        locked = tx.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:u, 0))"), {"u": str(user_id)}
        ).scalar_one()
        if not locked:
            continue
        running = tx.execute(_RUNNING_FOR_USER, {"user_id": user_id}).scalar_one()
        if running >= MAX_RUNNING_JOBS_PER_USER:
            continue
        tx.execute(_CLAIM, {"id": job_id, "worker": worker})
        job = tx.get(Job, job_id, populate_existing=True)
        return job
    return None


def _guarded(tx: Session, job_id: uuid.UUID, worker: str, sets: str, params: dict[str, Any]) -> None:
    row = tx.execute(
        text(f"UPDATE jobs SET {sets} WHERE id = :id AND locked_by = :worker AND status = 'running' RETURNING id"),
        {"id": job_id, "worker": worker, **params},
    ).first()
    if row is None:
        raise LeaseLost(str(job_id))


_RENEW = f"lease_expires_at = now() + interval '{LEASE_S} seconds'"


def renew(tx: Session, job_id: uuid.UUID, worker: str) -> None:
    _guarded(tx, job_id, worker, _RENEW, {})


def set_progress(tx: Session, job_id: uuid.UUID, worker: str, stage: str, percent: int) -> None:
    """Write stage and percent. Also renews the lease."""
    _guarded(
        tx,
        job_id,
        worker,
        f"stage = :stage, percent = :percent, {_RENEW}",
        {"stage": stage, "percent": max(0, min(100, percent))},
    )


def mark_stage(tx: Session, job_id: uuid.UUID, worker: str, stage: str, data: Any = True) -> None:
    """Record that a stage finished, so a repeat run resumes after it."""
    _guarded(
        tx,
        job_id,
        worker,
        f"stage_markers = stage_markers || jsonb_build_object(CAST(:stage AS text), CAST(:data AS jsonb)), {_RENEW}",
        {"stage": stage, "data": json.dumps(data)},
    )


def succeed(tx: Session, job_id: uuid.UUID, worker: str) -> None:
    _guarded(
        tx,
        job_id,
        worker,
        "status = 'succeeded', percent = 100, finished_at = now(), locked_by = NULL, lease_expires_at = NULL",
        {},
    )


def wait_for_input(tx: Session, job_id: uuid.UUID, worker: str) -> None:
    """The graph hit `ask_user`. A waiting job holds no lease."""
    _guarded(tx, job_id, worker, "status = 'waiting_input', locked_by = NULL, lease_expires_at = NULL", {})


def fail(
    tx: Session,
    job_id: uuid.UUID,
    worker: str,
    error_code: str,
    detail: str,
    retryable: bool = True,
) -> bool:
    """Retry with backoff while attempts remain, else fail. Returns True if the job will run again."""
    job = tx.get(Job, job_id, populate_existing=True)
    if job is None or job.locked_by != worker or job.status != "running":
        raise LeaseLost(str(job_id))
    backoff = JOB_SPECS[JobKind(job.kind)].backoff_s
    if retryable and job.attempts < job.max_attempts:
        _guarded(
            tx,
            job_id,
            worker,
            f"status = 'queued', run_after = now() + interval '{backoff} seconds', "
            "locked_by = NULL, lease_expires_at = NULL, error_code = :code, error_detail = :detail",
            {"code": error_code, "detail": detail[:ERROR_DETAIL_MAX]},
        )
        return True
    _guarded(
        tx,
        job_id,
        worker,
        "status = 'failed', finished_at = now(), locked_by = NULL, lease_expires_at = NULL, "
        "error_code = :code, error_detail = :detail",
        {"code": error_code, "detail": detail[:ERROR_DETAIL_MAX]},
    )
    return False


def release(tx: Session, job_id: uuid.UUID, worker: str) -> None:
    """Shutting down (ECS stop, Spot reclaim): give the job back without spending an attempt."""
    _guarded(
        tx,
        job_id,
        worker,
        "status = 'queued', run_after = now(), attempts = greatest(attempts - 1, 0), "
        "locked_by = NULL, lease_expires_at = NULL",
        {},
    )


def requeue_waiting(tx: Session, job_id: uuid.UUID, payload: dict[str, Any]) -> bool:
    """The user answered an `ask_user` question: the same row goes back to queued."""
    row = tx.execute(
        text(
            "UPDATE jobs SET status = 'queued', run_after = now(), payload = payload || CAST(:p AS jsonb) "
            "WHERE id = :id AND status = 'waiting_input' RETURNING id"
        ),
        {"id": job_id, "p": json.dumps(payload)},
    ).first()
    return row is not None


def retry_failed(tx: Session, job_id: uuid.UUID) -> bool:
    """The Retry button: back to queued, attempts reset, stage markers kept."""
    row = tx.execute(
        text(
            "UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), error_code = NULL, "
            "error_detail = NULL, finished_at = NULL WHERE id = :id AND status = 'failed' RETURNING id"
        ),
        {"id": job_id},
    ).first()
    return row is not None


def sweep(tx: Session) -> int:
    """Fail running jobs whose lease expired with no attempts left."""
    result = tx.execute(
        text(
            "UPDATE jobs SET status = 'failed', error_code = 'lease_lost', finished_at = now(), "
            "locked_by = NULL, lease_expires_at = NULL "
            "WHERE status = 'running' AND lease_expires_at < now() AND attempts >= max_attempts RETURNING id"
        )
    )
    return len(result.all())
