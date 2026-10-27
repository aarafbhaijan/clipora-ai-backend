"""Job kinds, their queues and limits, and job payloads (architecture.md section 9)."""

import os
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel

from core.enums import JobKind, QueueName

MAX_RUNNING_JOBS_PER_USER = 2
LEASE_S = 120
HEARTBEAT_S = 30
POLL_S = 1.0


@dataclass(frozen=True)
class JobSpec:
    kind: JobKind
    queue: QueueName
    timeout_s: int
    max_attempts: int
    backoff_s: int


# Local testing with a slow local model (Ollama) only: JOB_TIMEOUT_SCALE=4 makes every timeout 4x longer.
# Leave it unset in staging and production.
_SCALE = max(1.0, float(os.environ.get("JOB_TIMEOUT_SCALE", "1") or 1))

JOB_SPECS: dict[JobKind, JobSpec] = {
    s.kind: JobSpec(s.kind, s.queue, int(s.timeout_s * _SCALE), s.max_attempts, s.backoff_s)
    for s in (
        JobSpec(JobKind.ingest, QueueName.ingest, 60 * 60, 3, 60),
        JobSpec(JobKind.agent, QueueName.agent, 5 * 60, 3, 5),
        JobSpec(JobKind.suggest, QueueName.agent, 2 * 60, 3, 5),
        JobSpec(JobKind.metadata, QueueName.agent, 3 * 60, 3, 5),
        JobSpec(JobKind.distill, QueueName.agent, 2 * 60, 3, 5),
        JobSpec(JobKind.export, QueueName.export, 30 * 60, 2, 30),
    )
}


class IngestPayload(BaseModel):
    project_id: UUID
    user_srt: bool = False


class AgentPayload(BaseModel):
    project_id: UUID
    message_id: UUID
    resume: bool = False  # True when answering an ask_user interrupt


class SuggestPayload(BaseModel):
    project_id: UUID
    more: bool = False


class MetadataPayload(BaseModel):
    project_id: UUID
    message_id: UUID | None = None
    edl_version: int
    options: dict[str, str] = {}


class DistillPayload(BaseModel):
    user_id: UUID


class ExportPayload(BaseModel):
    project_id: UUID
    export_id: UUID
    edl_version: int


INGEST_STAGES: tuple[str, ...] = (
    "probe",
    "transcribe",
    "proxy",
    "thumbnails",
    "waveform",
    "detect",
    "speakers",
    "index",
)
EXPORT_STAGES: tuple[str, ...] = ("prepare", "segments", "finish", "upload")
