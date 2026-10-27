"""Queue tests against a real Postgres (architecture.md section 9, rules.md section 4).

Needs TEST_DATABASE_URL pointing at a disposable database: the schema is migrated to head and every
table is truncated between tests. Never falls back to DATABASE_URL, so a dev database is never wiped.
"""

import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs TEST_DATABASE_URL with a disposable Postgres + pgvector")

if URL:
    os.environ["DATABASE_URL"] = URL

from sqlalchemy import text  # noqa: E402

import jobqueue  # noqa: E402
from core.enums import JobKind  # noqa: E402
from db.ids import uuid7  # noqa: E402
from db.models import Job, Project, User  # noqa: E402
from db.session import session_scope  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def schema() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).parents[4] / "database" / "alembic.ini"))
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    with session_scope() as tx:
        tx.execute(text("TRUNCATE jobs, exports, messages, edl_versions, projects, users CASCADE"))
    yield


def make_user() -> uuid.UUID:
    uid = uuid7()
    with session_scope() as tx:
        tx.add(User(id=uid, cognito_sub=f"dev:{uid}", email=f"{uid}@test"))
    return uid


def make_project(user_id: uuid.UUID) -> uuid.UUID:
    pid = uuid7()
    with session_scope() as tx:
        tx.add(Project(id=pid, user_id=user_id, title="t", status="ready", source_key="k", size_bytes=1))
    return pid


def add_job(user_id: uuid.UUID, kind: JobKind = JobKind.export, project: bool = True) -> uuid.UUID:
    pid = make_project(user_id) if project else None
    with session_scope() as tx:
        return jobqueue.enqueue(tx, kind, {}, user_id, pid).id


def claim(queue: str, worker: str) -> uuid.UUID | None:
    with session_scope() as tx:
        job = jobqueue.claim(tx, queue, worker)
        return job.id if job else None


def test_enqueue_rejects_second_active_job_of_same_kind() -> None:
    uid = make_user()
    pid = make_project(uid)
    with session_scope() as tx:
        jobqueue.enqueue(tx, JobKind.agent, {}, uid, pid)
        with pytest.raises(jobqueue.JobBusy):
            jobqueue.enqueue(tx, JobKind.agent, {}, uid, pid)
        jobqueue.enqueue(tx, JobKind.suggest, {}, uid, pid)  # other kind is fine


def test_concurrent_claimers_never_take_the_same_job() -> None:
    users = [make_user() for _ in range(10)]
    ids = {add_job(u) for u in users for _ in range(2)}
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        while (job_id := claim("export", f"w{n}")) is not None:
            with lock:
                claimed.append(job_id)
            with session_scope() as tx:
                jobqueue.succeed(tx, job_id, f"w{n}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == len(set(claimed)) == len(ids)


def test_per_user_limit_of_two_holds_under_a_race() -> None:
    uid = make_user()
    for _ in range(6):
        add_job(uid)
    barrier = threading.Barrier(6)
    got: list[uuid.UUID | None] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        barrier.wait()
        job_id = claim("export", f"w{n}")
        with lock:
            got.append(job_id)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed = [g for g in got if g]
    with session_scope() as tx:
        running = tx.execute(text("SELECT count(*) FROM jobs WHERE status = 'running'")).scalar_one()
    assert 1 <= len(claimed) <= 2
    assert running == len(claimed)


def test_lease_reclaim_after_a_dead_worker() -> None:
    uid = make_user()
    job_id = add_job(uid)
    assert claim("export", "dead") == job_id
    assert claim("export", "other") is None
    with session_scope() as tx:  # simulate 2 minutes passing without a heartbeat
        tx.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": job_id}
        )
    assert claim("export", "other") == job_id
    with session_scope() as tx, pytest.raises(jobqueue.LeaseLost):
        jobqueue.set_progress(tx, job_id, "dead", "segments", 50)  # the dead worker can no longer write
    with session_scope() as tx:
        job = tx.get(Job, job_id)
        assert job is not None and job.locked_by == "other" and job.attempts == 2


def test_retry_with_backoff_then_fail() -> None:
    uid = make_user()
    job_id = add_job(uid)  # export: 2 attempts, 30 s backoff
    assert claim("export", "w") == job_id
    with session_scope() as tx:
        assert jobqueue.fail(tx, job_id, "w", "internal", "boom") is True
    assert claim("export", "w") is None  # backoff not over
    with session_scope() as tx:
        tx.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})
    assert claim("export", "w") == job_id
    with session_scope() as tx:
        assert jobqueue.fail(tx, job_id, "w", "internal", "boom again") is False
        job = tx.get(Job, job_id)
        assert job is not None and job.status == "failed" and job.error_code == "internal"
    with session_scope() as tx:
        assert jobqueue.retry_failed(tx, job_id)
        job = tx.get(Job, job_id, populate_existing=True)
        assert job is not None and job.status == "queued" and job.attempts == 0


def test_non_retryable_fails_at_once_and_markers_survive_retry() -> None:
    uid = make_user()
    job_id = add_job(uid, JobKind.ingest)
    assert claim("ingest", "w") == job_id
    with session_scope() as tx:
        jobqueue.mark_stage(tx, job_id, "w", "probe", {"duration_s": 3.0})
        assert jobqueue.fail(tx, job_id, "w", "duration_too_long", "too long", retryable=False) is False
    with session_scope() as tx:
        jobqueue.retry_failed(tx, job_id)
        job = tx.get(Job, job_id, populate_existing=True)
        assert job is not None and job.stage_markers == {"probe": {"duration_s": 3.0}}


def test_sweep_fails_expired_jobs_with_no_attempts_left() -> None:
    uid = make_user()
    job_id = add_job(uid)
    with session_scope() as tx:
        tx.execute(
            text(
                "UPDATE jobs SET status = 'running', attempts = max_attempts, locked_by = 'gone', "
                "lease_expires_at = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": job_id},
        )
    assert claim("export", "w") is None  # no attempts left, so it is not reclaimed
    with session_scope() as tx:
        assert jobqueue.sweep(tx) == 1
        job = tx.get(Job, job_id)
        assert job is not None and job.status == "failed" and job.error_code == "lease_lost"


def test_waiting_input_holds_no_lease_and_requeues() -> None:
    uid = make_user()
    job_id = add_job(uid, JobKind.agent)
    assert claim("agent", "w") == job_id
    with session_scope() as tx:
        jobqueue.wait_for_input(tx, job_id, "w")
    with session_scope() as tx:
        job = tx.get(Job, job_id)
        assert job is not None and job.status == "waiting_input" and job.locked_by is None
        assert jobqueue.requeue_waiting(tx, job_id, {"message_id": "m2"})
    assert claim("agent", "w2") == job_id


def test_release_on_shutdown_does_not_spend_an_attempt() -> None:
    uid = make_user()
    job_id = add_job(uid)
    assert claim("export", "w") == job_id
    with session_scope() as tx:
        jobqueue.release(tx, job_id, "w")
        job = tx.get(Job, job_id)
        assert job is not None and job.status == "queued" and job.attempts == 0
