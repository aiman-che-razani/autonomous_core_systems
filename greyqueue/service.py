"""Transactional operations; callers own commit boundaries."""

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from greyqueue.models import Attempt, Event, Job, Result, Worker
from greyqueue.scheduler import scheduler
from greyqueue.tasks import validate

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER"}
TRANSITIONS = {
    "QUEUED": {"LEASED", "CANCELLED"},
    "RETRY_WAIT": {"LEASED", "CANCELLED"},
    "LEASED": {"RUNNING", "RETRY_WAIT", "DEAD_LETTER"},
    "RUNNING": {"SUCCEEDED", "FAILED", "RETRY_WAIT", "DEAD_LETTER"},
}


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class Saturated(Exception):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def database_time(session: Session) -> datetime:
    # Read after acquiring the row lock; transaction-start now() can predate a lock wait.
    return session.scalar(select(func.clock_timestamp()))


def transition(session: Session, job: Job, state: str) -> None:
    if state not in TRANSITIONS.get(job.status, set()):
        raise Conflict(f"Illegal transition {job.status} -> {state}")
    job.status = state
    job.updated_at = database_time(session)
    session.add(Event(job_id=job.id, state=state))


def submit(
    session: Session,
    task: str,
    args: dict[str, Any],
    *,
    priority: int = 0,
    timeout: float = 15,
    max_retries: int = 0,
    retry_delay: float = 1,
    retry_jitter: bool = True,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
    scheduled_at: datetime | None = None,
    depends_on: UUID | None = None,
    queue_limit: int = 10000,
    submissions_per_minute: int = 20000,
) -> Job:
    args = validate(task, args)
    definition = {
        "task": task,
        "args": args,
        "priority": priority,
        "timeout": float(timeout),
        "max_retries": max_retries,
        "retry_delay": float(retry_delay),
        "retry_jitter": retry_jitter,
        "metadata": metadata or {},
        "scheduled_at": scheduled_at.astimezone(UTC).isoformat() if scheduled_at else None,
        "depends_on": str(depends_on) if depends_on else None,
    }
    digest = hashlib.sha256(
        json.dumps(definition, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    # Atomic admission across coordinators. A measured scalability tradeoff.
    session.execute(text("SELECT pg_advisory_xact_lock(741901)"))
    if idempotency_key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            if existing.request_hash != digest:
                raise Conflict("Idempotency key already used for a different definition")
            return existing
    timestamp = database_time(session)
    count = session.scalar(select(func.count()).select_from(Job).where(Job.status.not_in(TERMINAL)))
    rate = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.created_at >= timestamp - timedelta(minutes=1))
    )
    if count >= queue_limit or rate >= submissions_per_minute:
        raise Saturated("Admission limit reached; retry later with the same idempotency key")
    if depends_on:
        parent = get_job(session, depends_on)
        if parent.status in TERMINAL - {"SUCCEEDED"}:
            raise Conflict("Dependency already failed or was cancelled")
    job = Job(
        task=task,
        args=args,
        priority=priority,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        retry_jitter=retry_jitter,
        idempotency_key=idempotency_key,
        request_hash=digest,
        metadata_json=metadata or {},
        depends_on=depends_on,
        available_at=scheduled_at or timestamp,
    )
    session.add(job)
    session.flush()
    session.add_all([Event(job_id=job.id, state="SUBMITTED"), Event(job_id=job.id, state="QUEUED")])
    return job


def get_job(session: Session, job_id: UUID, lock: bool = False) -> Job:
    query = select(Job).where(Job.id == job_id)
    if lock:
        query = query.with_for_update()
    job = session.scalar(query.execution_options(populate_existing=True))
    if job is None:
        raise Missing("Job not found")
    return job


def claim(
    session: Session,
    worker_id: str,
    slot: int = 0,
    claim_id: UUID | None = None,
    policy: str = "fifo",
    lease_seconds: float = 10,
) -> tuple[Job, Attempt] | None:
    worker = session.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if worker is None:
        raise Missing("Register worker first")
    if worker.state != "HEALTHY":
        return None
    if slot < 0 or slot >= worker.capacity:
        raise Conflict("Worker capacity exceeded")
    if claim_id:
        existing = session.scalar(select(Attempt).where(Attempt.claim_id == claim_id))
        if existing:
            if existing.worker_id != worker_id or existing.slot != slot:
                raise Conflict("Claim ID belongs to another slot")
            if existing.finished_at is not None:
                raise Conflict("Claim is already finished")
            return get_job(session, existing.job_id), existing
    attempt = session.scalar(
        select(Attempt).where(
            Attempt.worker_id == worker_id, Attempt.slot == slot, Attempt.finished_at.is_(None)
        )
    )
    if attempt:
        if claim_id and attempt.claim_id != claim_id:
            raise Conflict("Slot is occupied")
        return get_job(session, attempt.job_id), attempt
    timestamp = database_time(session)
    job = session.scalar(
        scheduler(policy).query(worker, timestamp).with_for_update(skip_locked=True).limit(1)
    )
    if job is None:
        return None
    job.attempt_count += 1
    attempt = Attempt(
        job_id=job.id,
        worker_id=worker_id,
        slot=slot,
        claim_id=claim_id or uuid4(),
        fence=job.attempt_count,
        expires_at=timestamp + timedelta(seconds=lease_seconds),
    )
    session.add(attempt)
    transition(session, job, "LEASED")
    session.flush()
    return job, attempt


def owned(
    session: Session, job_id: UUID, worker_id: str, token: UUID, allow_finished: bool = False
) -> tuple[Job, Attempt]:
    job = get_job(session, job_id, lock=True)
    attempt = session.get(Attempt, token, populate_existing=True)
    if attempt is None or attempt.job_id != job_id or attempt.worker_id != worker_id:
        raise Conflict("Assignment does not belong to this worker")
    if allow_finished and attempt.finished_at is not None:
        return job, attempt
    if (
        attempt.finished_at is not None
        or attempt.fence != job.attempt_count
        or attempt.expires_at <= database_time(session)
    ):
        raise Conflict("Expired or fenced assignment")
    return job, attempt


def start(session: Session, job_id: UUID, worker_id: str, token: UUID) -> None:
    job, attempt = owned(session, job_id, worker_id, token)
    if job.status == "RUNNING":
        return
    if job.status != "LEASED":
        raise Conflict("Job is not assigned")
    attempt.started_at = database_time(session)
    transition(session, job, "RUNNING")


def renew(
    session: Session, job_id: UUID, worker_id: str, token: UUID, lease_seconds: float
) -> datetime:
    job, attempt = owned(session, job_id, worker_id, token)
    timestamp = database_time(session)
    expiry = timestamp + timedelta(seconds=lease_seconds)
    if attempt.started_at:
        expiry = min(expiry, attempt.started_at + timedelta(seconds=job.timeout + 5))
    if expiry <= timestamp:
        raise Conflict("Execution deadline passed")
    attempt.expires_at = expiry
    return expiry


def fail_attempt(
    session: Session,
    job: Job,
    attempt: Attempt,
    error: str,
    retryable: bool,
    outcome: str = "FAILED",
) -> None:
    timestamp = database_time(session)
    attempt.finished_at, attempt.outcome, attempt.error = timestamp, outcome, error
    if retryable and job.attempt_count <= job.max_retries:
        delay = min(job.retry_delay * (2 ** (job.attempt_count - 1)), 3600)
        if job.retry_jitter:
            delay *= random.uniform(0.5, 1.5)
        job.available_at = timestamp + timedelta(seconds=delay)
        transition(session, job, "RETRY_WAIT")
    else:
        transition(session, job, "DEAD_LETTER" if retryable else "FAILED")
        session.add(Result(job_id=job.id, output=None, error=error))


def finish(
    session: Session,
    job_id: UUID,
    worker_id: str,
    token: UUID,
    output: dict[str, Any] | None,
    error: str | None,
    retryable: bool = False,
) -> None:
    job, attempt = owned(session, job_id, worker_id, token, allow_finished=True)
    if attempt.finished_at is not None:
        if (
            attempt.outcome in {"SUCCEEDED", "FAILED"}
            and attempt.output == output
            and attempt.error == error
        ):
            return
        raise Conflict("Conflicting or expired completion replay")
    if attempt.fence != job.attempt_count or attempt.expires_at <= database_time(session):
        raise Conflict("Expired or fenced assignment")
    if job.status != "RUNNING":
        raise Conflict("Job must be running before completion")
    if error is not None:
        fail_attempt(session, job, attempt, error, retryable)
    else:
        attempt.output, attempt.outcome = output, "SUCCEEDED"
        attempt.finished_at = database_time(session)
        session.add(Result(job_id=job.id, output=output, error=None))
        transition(session, job, "SUCCEEDED")


def cancel(session: Session, job_id: UUID) -> None:
    job = get_job(session, job_id, lock=True)
    if job.status == "CANCELLED":
        return
    if job.status not in {"QUEUED", "RETRY_WAIT"}:
        raise Conflict("Only waiting jobs can be cancelled")
    transition(session, job, "CANCELLED")


def serialize(session: Session, job: Job, results: dict | None = None) -> dict[str, Any]:
    result = results.get(job.id) if results is not None else session.get(Result, job.id)
    return {
        "id": str(job.id),
        "task": job.task,
        "args": job.args,
        "status": job.status,
        "priority": job.priority,
        "timeout": job.timeout,
        "max_retries": job.max_retries,
        "attempt_count": job.attempt_count,
        "metadata": job.metadata_json,
        "available_at": job.available_at.isoformat(),
        "depends_on": str(job.depends_on) if job.depends_on else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "result": result.output if result else None,
        "error": result.error if result else None,
    }
