from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from greyqueue.models import Attempt, Event, Job, Result, Worker
from greyqueue.tasks import validate


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def transition(session: Session, job: Job, state: str) -> None:
    job.status = state
    job.updated_at = now()
    session.add(Event(job_id=job.id, state=state))


def submit(session: Session, task: str, args: dict[str, Any]) -> Job:
    job = Job(task=task, args=validate(task, args))
    session.add(job)
    session.flush()
    session.add(Event(job_id=job.id, state="SUBMITTED"))
    transition(session, job, "QUEUED")
    return job


def get_job(session: Session, job_id: UUID, lock: bool = False) -> Job:
    query = select(Job).where(Job.id == job_id)
    if lock:
        query = query.with_for_update()
    job = session.scalar(query)
    if job is None:
        raise Missing("Job not found")
    return job


def claim(session: Session, worker_id: str) -> tuple[Job, Attempt] | None:
    worker = session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update())
    if worker is None:
        raise Missing("Register worker first")
    worker.last_seen = now()
    # Recover an ambiguous HTTP claim response without assigning another job.
    attempt = session.scalar(
        select(Attempt).where(Attempt.worker_id == worker_id, Attempt.finished_at.is_(None))
    )
    if attempt:
        return get_job(session, attempt.job_id), attempt
    job = session.scalar(
        select(Job)
        .where(Job.status == "QUEUED")
        .order_by(Job.created_at, Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    attempt = Attempt(job_id=job.id, worker_id=worker_id)
    session.add(attempt)
    transition(session, job, "LEASED")
    session.flush()
    return job, attempt


def owned(session: Session, job_id: UUID, worker_id: str, token: UUID) -> tuple[Job, Attempt]:
    job = get_job(session, job_id, lock=True)
    attempt = session.get(Attempt, token)
    if attempt is None or attempt.job_id != job_id or attempt.worker_id != worker_id:
        raise Conflict("Assignment does not belong to this worker")
    return job, attempt


def start(session: Session, job_id: UUID, worker_id: str, token: UUID) -> None:
    job, attempt = owned(session, job_id, worker_id, token)
    if job.status == "RUNNING":
        return
    if job.status != "LEASED":
        raise Conflict("Job is not assigned")
    attempt.started_at = now()
    transition(session, job, "RUNNING")


def finish(
    session: Session,
    job_id: UUID,
    worker_id: str,
    token: UUID,
    output: dict[str, Any] | None,
    error: str | None,
) -> None:
    job, attempt = owned(session, job_id, worker_id, token)
    state = "FAILED" if error is not None else "SUCCEEDED"
    if job.status in {"SUCCEEDED", "FAILED"}:
        result = session.get(Result, job_id)
        if job.status == state and result.output == output and result.error == error:
            return
        raise Conflict("Conflicting completion replay")
    if job.status != "RUNNING":
        raise Conflict("Job must be running before completion")
    session.add(Result(job_id=job.id, output=output, error=error))
    attempt.finished_at = now()
    transition(session, job, state)


def cancel(session: Session, job_id: UUID) -> None:
    job = get_job(session, job_id, lock=True)
    if job.status == "CANCELLED":
        return
    if job.status != "QUEUED":
        raise Conflict("Only queued jobs can be cancelled in v0.1")
    transition(session, job, "CANCELLED")


def serialize(session: Session, job: Job) -> dict[str, Any]:
    result = session.get(Result, job.id)
    return {
        "id": str(job.id),
        "task": job.task,
        "args": job.args,
        "status": job.status,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "result": result.output if result else None,
        "error": result.error if result else None,
    }
