"""Worker process: `python -m worker.run --queue <name> --slots <n>` (architecture.md section 9).

Each slot is a thread that claims one job at a time. A heartbeat renews leases every 30 s, a watchdog
stops jobs past their timeout, and a sweep fails jobs whose lease expired with no attempts left.
"""

import argparse
import logging
import os
import random
import signal
import socket
import threading
import time
import traceback
import uuid

from botocore.exceptions import ClientError
from sqlalchemy.exc import OperationalError

import jobqueue
from core.enums import QueueName
from core.errors import ErrorCode
from core.jobs import HEARTBEAT_S, POLL_S
from db.session import engine, session_scope
from worker import handlers
from worker.context import Cancelled, JobContext, JobFailed, Outcome

log = logging.getLogger("worker")

SWEEP_EVERY_S = 60
DB_RETRY_S = 5


_THROTTLE_CODES = {"ThrottlingException", "ServiceQuotaExceededException", "TooManyRequestsException"}


def _classify_aws(e: ClientError) -> tuple[str, bool]:
    """(error_code, retryable). A daily quota will not recover within the retry backoff, so don't retry it."""
    code = str(e.response.get("Error", {}).get("Code", ""))
    if code in _THROTTLE_CODES:
        return ErrorCode.rate_limited.value, "per day" not in str(e).lower()
    return ErrorCode.internal.value, True


class Worker:
    def __init__(self, queue: QueueName, slots: int) -> None:
        self.queue = queue
        self.slots = slots
        self.id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stopping = threading.Event()
        self.running: dict[uuid.UUID, tuple[JobContext, float]] = {}  # job id -> (ctx, deadline)
        self.lock = threading.Lock()

    # Slots

    def slot_loop(self, n: int) -> None:
        while not self.stopping.is_set():
            try:
                ctx = self._claim()
            except OperationalError:
                log.warning("database unavailable, retrying", extra={"slot": n})
                self.stopping.wait(DB_RETRY_S)
                continue
            if ctx is None:
                self.stopping.wait(POLL_S + random.uniform(0, 0.3))
                continue
            self._run(ctx)

    def _claim(self) -> JobContext | None:
        with session_scope() as tx:
            job = jobqueue.claim(tx, self.queue.value, self.id)
            if job is None:
                return None
            ctx = JobContext.from_job(job, self.id)
            timeout = job.timeout_s
        with self.lock:
            self.running[ctx.job_id] = (ctx, time.monotonic() + timeout)
        log.info("claimed job", extra={"job_id": str(ctx.job_id), "kind": ctx.kind, "user_id": str(ctx.user_id)})
        return ctx

    def _run(self, ctx: JobContext) -> None:
        handler = handlers.handler_for(ctx.kind)
        try:
            if handler is None:
                raise JobFailed(ErrorCode.internal, f"no handler for {ctx.kind} on this queue")
            outcome = handler(ctx)
            ctx.check()
            with session_scope() as tx:
                if outcome == Outcome.waiting_input:
                    jobqueue.wait_for_input(tx, ctx.job_id, self.id)
                else:
                    jobqueue.succeed(tx, ctx.job_id, self.id)
        except jobqueue.LeaseLost:
            log.warning("lease lost, stopping", extra={"job_id": str(ctx.job_id)})
            ctx.kill_children("lease lost")
        except Cancelled as e:
            self._after_cancel(ctx, str(e))
        except JobFailed as e:
            self._fail(ctx, e.code.value, e.detail, e.retryable)
        except ClientError as e:
            code, retryable = _classify_aws(e)
            log.warning("aws error", extra={"job_id": str(ctx.job_id), "code": code})
            self._fail(ctx, code, f"{type(e).__name__}: {e}", retryable)
        except Exception as e:  # unexpected: retry while attempts remain
            log.error("job crashed", extra={"job_id": str(ctx.job_id), "trace": traceback.format_exc()})
            self._fail(ctx, ErrorCode.internal.value, f"{type(e).__name__}: {e}", True)
        finally:
            with self.lock:
                self.running.pop(ctx.job_id, None)

    def _after_cancel(self, ctx: JobContext, reason: str) -> None:
        # timeout: the watchdog already recorded the failed attempt. lease lost: write nothing.
        if reason == "shutdown":
            try:
                with session_scope() as tx:
                    jobqueue.release(tx, ctx.job_id, self.id)
            except jobqueue.LeaseLost:
                pass

    def _fail(self, ctx: JobContext, code: str, detail: str, retryable: bool) -> None:
        try:
            with session_scope() as tx:
                again = jobqueue.fail(tx, ctx.job_id, self.id, code, detail, retryable)
            log.info("job failed", extra={"job_id": str(ctx.job_id), "code": code, "retry": again})
        except jobqueue.LeaseLost:
            pass
        except OperationalError:
            # Can't record it now; the lease expires and the job is retried or swept as lease_lost.
            log.warning("database unavailable, failure not recorded", extra={"job_id": str(ctx.job_id)})

    # Housekeeping on the main thread

    def heartbeat_and_watchdog(self) -> None:
        last_sweep = 0.0
        while not self.stopping.wait(HEARTBEAT_S / 3):
            now = time.monotonic()
            with self.lock:
                items = list(self.running.items())
            for job_id, (ctx, deadline) in items:
                if ctx.cancel.is_set():
                    continue  # stopping; do not keep its lease alive
                if now > deadline:
                    log.warning("job timed out", extra={"job_id": str(job_id)})
                    ctx.kill_children("timeout")
                    self._fail(ctx, ErrorCode.timeout.value, "the task ran past its time limit", True)
                    continue
                try:
                    with session_scope() as tx:
                        jobqueue.renew(tx, job_id, self.id)
                except jobqueue.LeaseLost:
                    ctx.kill_children("lease lost")
                except OperationalError:
                    # Keep working; if the outage outlasts the lease, the next renew reports LeaseLost.
                    log.warning("database unavailable during heartbeat", extra={"job_id": str(job_id)})
            if now - last_sweep > SWEEP_EVERY_S:
                last_sweep = now
                try:
                    with session_scope() as tx:
                        jobqueue.sweep(tx)
                except OperationalError:
                    log.warning("database unavailable during sweep")

    def run(self) -> None:
        handlers.load(self.queue)
        threads = [
            threading.Thread(target=self.slot_loop, args=(n,), name=f"slot-{n}", daemon=True) for n in range(self.slots)
        ]
        for t in threads:
            t.start()
        log.info("worker started", extra={"queue": self.queue.value, "slots": self.slots, "worker": self.id})
        try:
            self.heartbeat_and_watchdog()
        finally:
            with self.lock:
                items = list(self.running.values())
            for ctx, _ in items:
                ctx.kill_children("shutdown")
            for t in threads:
                t.join(timeout=20)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True, choices=[q.value for q in QueueName])
    parser.add_argument("--slots", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    engine(pool_size=args.slots + 2)
    worker = Worker(QueueName(args.queue), args.slots)
    signal.signal(signal.SIGTERM, lambda *_: worker.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stopping.set())
    worker.run()


if __name__ == "__main__":
    main()
