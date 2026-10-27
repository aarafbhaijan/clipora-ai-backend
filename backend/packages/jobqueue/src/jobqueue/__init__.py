"""The `jobs` table is the queue (architecture.md section 9).

Named `jobqueue`, not `queue`, so it does not shadow the standard library module.
Every write by a worker is guarded by `locked_by = :worker`, so a worker that lost its lease writes nothing.
"""

from jobqueue.queue import (
    JobBusy,
    LeaseLost,
    claim,
    enqueue,
    fail,
    mark_stage,
    release,
    renew,
    requeue_waiting,
    retry_failed,
    set_progress,
    succeed,
    sweep,
    wait_for_input,
)

__all__ = [
    "JobBusy",
    "LeaseLost",
    "claim",
    "enqueue",
    "fail",
    "mark_stage",
    "release",
    "renew",
    "requeue_waiting",
    "retry_failed",
    "set_progress",
    "succeed",
    "sweep",
    "wait_for_input",
]
