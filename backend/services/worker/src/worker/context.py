"""What a job handler gets: the job, guarded progress writes, stage markers and cancellation.

Every write goes through `jobqueue` with `locked_by = worker`, so after a lost lease or a timeout
the handler's next write raises LeaseLost and it stops. Long steps must call `check()` often and
register child processes so they are killed on cancel.
"""

import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

import jobqueue
from core.errors import ErrorCode
from db.models import Job
from db.session import session_scope

T = TypeVar("T")


class Outcome(Enum):
    done = "done"
    waiting_input = "waiting_input"


class Cancelled(Exception):
    """The job timed out, lost its lease, or the worker is shutting down."""


class JobFailed(Exception):
    """An expected failure with a stable error code. Not retried unless retryable is True."""

    def __init__(self, code: ErrorCode, detail: str, retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable


@dataclass
class JobContext:
    job_id: uuid.UUID
    kind: str
    user_id: uuid.UUID
    project_id: uuid.UUID | None
    payload: dict[str, Any]
    markers: dict[str, Any]
    attempts: int
    worker: str
    cancel: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str = ""
    _procs: list[subprocess.Popen[bytes]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_job(cls, job: Job, worker: str) -> "JobContext":
        return cls(
            job_id=job.id,
            kind=job.kind,
            user_id=job.user_id,
            project_id=job.project_id,
            payload=dict(job.payload or {}),
            markers=dict(job.stage_markers or {}),
            attempts=job.attempts,
            worker=worker,
        )

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled(self.cancel_reason or "cancelled")

    def progress(self, stage: str, percent: float) -> None:
        self.check()
        with session_scope() as tx:
            jobqueue.set_progress(tx, self.job_id, self.worker, stage, int(percent))

    def done(self, stage: str) -> bool:
        """True if a previous run already finished this stage."""
        return stage in self.markers

    def mark(self, stage: str, data: Any = True) -> None:
        self.check()
        with session_scope() as tx:
            jobqueue.mark_stage(tx, self.job_id, self.worker, stage, data)
        self.markers[stage] = data

    def run_stage(self, stage: str, fn: Callable[[], T]) -> T | Any:
        """Run a stage once: skip it if its marker exists, else run it and record what it returned."""
        if self.done(stage):
            return self.markers[stage]
        result = fn()
        self.mark(stage, result if result is not None else True)
        return result

    def register(self, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._procs.append(proc)
        if self.cancel.is_set():
            proc.kill()

    def kill_children(self, reason: str) -> None:
        self.cancel_reason = reason
        self.cancel.set()
        with self._lock:
            for p in self._procs:
                if p.poll() is None:
                    p.kill()
