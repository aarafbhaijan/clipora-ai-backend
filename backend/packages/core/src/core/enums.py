"""Enums shared by the API, workers and the frontend (through OpenAPI)."""

from enum import StrEnum


class Role(StrEnum):
    admin = "admin"
    editor = "editor"


class ProjectStatus(StrEnum):
    uploading = "uploading"
    ingesting = "ingesting"
    ready = "ready"
    failed = "failed"


class JobKind(StrEnum):
    ingest = "ingest"
    agent = "agent"
    suggest = "suggest"
    metadata = "metadata"
    distill = "distill"
    export = "export"


class QueueName(StrEnum):
    ingest = "ingest"
    agent = "agent"
    export = "export"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    waiting_input = "waiting_input"
    succeeded = "succeeded"
    failed = "failed"


ACTIVE_JOB_STATUSES = (JobStatus.queued, JobStatus.running, JobStatus.waiting_input)


class ExportStatus(StrEnum):
    queued = "queued"
    rendering = "rendering"
    done = "done"
    failed = "failed"


class Thread(StrEnum):
    edit = "edit"
    metadata = "metadata"


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"


class MessageKind(StrEnum):
    text = "text"
    suggestions = "suggestions"
    choices = "choices"
    metadata = "metadata"


class Author(StrEnum):
    agent = "agent"
    user = "user"


class Framing(StrEnum):
    crop = "crop"
    fit = "fit"


class MemoryStatus(StrEnum):
    active = "active"
    pending = "pending"
    disabled = "disabled"


class MemoryKind(StrEnum):
    length = "length"
    aspect = "aspect"
    style = "style"
    transitions = "transitions"
    titles = "titles"
    avoid = "avoid"
