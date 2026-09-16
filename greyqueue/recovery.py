"""Reconstruct recovery decisions from PostgreSQL on every coordinator."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from greyqueue.models import Attempt, Job, SystemEvent, Worker
from greyqueue.service import database_time, fail_attempt, transition

log = logging.getLogger("greyqueue.recovery")


def recover_jobs(session, limit: int = 100) -> int:
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(741902)")):
        return 0
    timestamp = database_time(session)
    jobs = list(
        session.scalars(
            select(Job)
            .join(Attempt, Attempt.job_id == Job.id)
            .where(
                Attempt.finished_at.is_(None),
                Attempt.expires_at <= timestamp,
                Job.status.in_(["LEASED", "RUNNING"]),
            )
            .with_for_update(of=Job, skip_locked=True)
            .limit(limit)
        )
    )
    for job in jobs:
        attempt = session.scalar(
            select(Attempt).where(Attempt.job_id == job.id, Attempt.finished_at.is_(None))
        )
        if attempt and attempt.expires_at <= database_time(session):
            fail_attempt(
                session,
                job,
                attempt,
                "Lease expired; execution outcome unknown",
                True,
                "LEASE_EXPIRED",
            )
            session.add(
                SystemEvent(
                    kind="lease_expired",
                    worker_id=attempt.worker_id,
                    detail={"job_id": str(job.id), "attempt_id": str(attempt.id)},
                )
            )
    parent = aliased(Job)
    blocked = list(
        session.scalars(
            select(Job)
            .where(
                Job.status.in_(["QUEUED", "RETRY_WAIT"]),
                select(parent.id)
                .where(
                    parent.id == Job.depends_on,
                    parent.status.in_(["FAILED", "DEAD_LETTER", "CANCELLED"]),
                )
                .exists(),
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    )
    for job in blocked:
        transition(session, job, "CANCELLED")
    return len(jobs)


def detect_workers(session, suspect_after: float, dead_after: float) -> None:
    timestamp = database_time(session)
    workers = session.scalars(
        select(Worker)
        .where(
            Worker.state != "DEAD", Worker.last_seen < timestamp - timedelta(seconds=suspect_after)
        )
        .with_for_update(skip_locked=True)
    )
    for worker in workers:
        state = (
            "DEAD" if (timestamp - worker.last_seen).total_seconds() >= dead_after else "SUSPECT"
        )
        if worker.state == "DRAINING" and state == "SUSPECT":
            continue
        if worker.state != state:
            worker.state = state
            session.add(SystemEvent(kind=f"worker_{state.lower()}", worker_id=worker.id))


def tick(sessions, config) -> None:
    with sessions.begin() as session:
        recover_jobs(session)
    with sessions.begin() as session:
        detect_workers(session, config.suspect_after, config.dead_after)


async def maintain(sessions, config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick, sessions, config)
        except SQLAlchemyError as exc:
            log.warning('{"event":"recovery_database_unavailable","type":"%s"}', type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.maintenance_interval)
        except TimeoutError:
            pass
