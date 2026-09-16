import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), server_default="HEALTHY")
    capacity: Mapped[int] = mapped_column(Integer, server_default="1")
    capabilities: Mapped[list[str]] = mapped_column(
        JSONB, server_default='["sleep","calculate_pi","hash_text","flaky"]'
    )
    session_hash: Mapped[str | None] = mapped_column(String(64))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','LEASED','RUNNING','SUCCEEDED','FAILED','CANCELLED','RETRY_WAIT','DEAD_LETTER')"
        ),
        Index(
            "ix_jobs_queue",
            "priority",
            "available_at",
            "created_at",
            postgresql_where=text("status IN ('QUEUED','RETRY_WAIT')"),
        ),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_created", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    timeout: Mapped[float] = mapped_column(Float, server_default="15")
    max_retries: Mapped[int] = mapped_column(Integer, server_default="0")
    retry_delay: Mapped[float] = mapped_column(Float, server_default="1")
    retry_jitter: Mapped[bool] = mapped_column(Boolean, server_default="true")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    depends_on: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    task: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        Index(
            "ix_attempt_active_slot",
            "worker_id",
            "slot",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
        Index(
            "ix_attempt_active_job",
            "job_id",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    slot: Mapped[int] = mapped_column(Integer, server_default="0")
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, default=uuid.uuid4)
    fence: Mapped[int] = mapped_column(Integer, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    outcome: Mapped[str | None] = mapped_column(String(32))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Result(Base):
    __tablename__ = "results"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    state: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    worker_id: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
