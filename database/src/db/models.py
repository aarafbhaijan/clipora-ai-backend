"""SQLAlchemy models for the 13 tables in architecture.md section 6. Schema changes go through Alembic."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from db.ids import uuid7

EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _id()
    cognito_sub: Mapped[str] = mapped_column(String(128), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16), server_default="editor")
    learn_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_user_created", "user_id", text("created_at desc")),)
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))
    source_key: Mapped[str] = mapped_column(String(512))
    upload_id: Mapped[str | None] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_s: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    current_version: Mapped[int] = mapped_column(Integer, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_project_thread", "project_id", "thread", "created_at"),)
    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    thread: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, server_default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    edl_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class EdlVersion(Base):
    __tablename__ = "edl_versions"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    version: Mapped[int] = mapped_column(Integer)
    edl: Mapped[dict[str, Any]] = mapped_column(JSONB)
    author: Mapped[str] = mapped_column(String(16))
    parent_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_project_created", "project_id", text("created_at desc")),
        Index(
            "ix_jobs_queued",
            "queue",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_jobs_running_lease",
            "queue",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "ux_jobs_active_project_kind",
            "project_id",
            "kind",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL AND status IN ('queued','running','waiting_input')"),
        ),
        Index(
            "ux_jobs_active_user_kind",
            "user_id",
            "kind",
            unique=True,
            postgresql_where=text("project_id IS NULL AND status IN ('queued','running','waiting_input')"),
        ),
    )
    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))
    queue: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    stage: Mapped[str | None] = mapped_column(String(32))
    percent: Mapped[int] = mapped_column(Integer, server_default="0")
    stage_markers: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    timeout_s: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    edl_version: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    output_key: Mapped[str | None] = mapped_column(String(512))
    srt_key: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = _created()


class MetadataSet(Base):
    __tablename__ = "metadata_sets"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    version: Mapped[int] = mapped_column(Integer)
    edl_version: Mapped[int] = mapped_column(Integer)
    export_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("exports.id"))
    platform: Mapped[str | None] = mapped_column(String(32))
    language: Mapped[str | None] = mapped_column(String(16))
    tone: Mapped[str | None] = mapped_column(String(16))
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    author: Mapped[str] = mapped_column(String(16))
    parent_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"
    __table_args__ = (
        Index("ix_chunks_project", "project_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )
    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    cue_from: Mapped[str] = mapped_column(String(16))
    cue_to: Mapped[str] = mapped_column(String(16))
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[Any] = mapped_column(TSVECTOR)


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    target_kind: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str] = mapped_column(String(128))
    signal: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class AgentMemory(Base):
    __tablename__ = "agent_memories"
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    evidence_count: Mapped[int] = mapped_column(Integer, server_default="0")
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    source_event_ids: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserLimit(Base):
    __tablename__ = "user_limits"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_user_created", "user_id", "created_at"),)
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # no FK: kept after deletes
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_kind: Mapped[str] = mapped_column(String(16))
    node: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = _created()
