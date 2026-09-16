"""Durable operational metrics, derived from SQL rather than process-local counters."""

from datetime import timedelta

from sqlalchemy import func, select

from greyqueue.models import Attempt, Event, Job, SystemEvent, Worker
from greyqueue.service import database_time


def worker_rows(session, limit=200):
    active = dict(
        session.execute(
            select(Attempt.worker_id, func.count())
            .where(Attempt.finished_at.is_(None))
            .group_by(Attempt.worker_id)
        ).all()
    )
    timestamp = database_time(session)
    return [
        {
            "id": w.id,
            "state": w.state,
            "capacity": w.capacity,
            "running": active.get(w.id, 0),
            "capabilities": w.capabilities,
            "last_seen": w.last_seen.isoformat(),
            "heartbeat_age_seconds": max(0, (timestamp - w.last_seen).total_seconds()),
        }
        for w in session.scalars(select(Worker).order_by(Worker.last_seen.desc()).limit(limit))
    ]


def snapshot(session, queue_limit: int) -> dict:
    states = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    counts = dict(session.execute(select(Event.state, func.count()).group_by(Event.state)).all())
    workers = worker_rows(session)
    worker_states = dict(
        session.execute(select(Worker.state, func.count()).group_by(Worker.state)).all()
    )
    duration = func.extract("epoch", Attempt.finished_at - Attempt.started_at)
    queue_wait = func.extract("epoch", Attempt.created_at - Job.created_at)
    avg, p50, p95, p99 = session.execute(
        select(
            func.avg(duration),
            func.percentile_cont(0.5).within_group(duration),
            func.percentile_cont(0.95).within_group(duration),
            func.percentile_cont(0.99).within_group(duration),
        ).where(Attempt.started_at.is_not(None), Attempt.finished_at.is_not(None))
    ).one()
    wait = session.scalar(
        select(func.avg(queue_wait)).select_from(Attempt).join(Job, Job.id == Attempt.job_id)
    )
    throughput = (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status == "SUCCEEDED",
                Job.updated_at >= func.clock_timestamp() - timedelta(seconds=60),
            )
        )
        / 60
    )
    depth = states.get("QUEUED", 0) + states.get("RETRY_WAIT", 0)
    active = depth + states.get("LEASED", 0) + states.get("RUNNING", 0)
    return {
        "version": "1.0.0",
        "states": states,
        "queue_depth": depth,
        "admitted_active": active,
        "queue_limit": queue_limit,
        "saturation": active / queue_limit,
        "submitted": counts.get("SUBMITTED", 0),
        "completed": counts.get("SUCCEEDED", 0),
        "failed": counts.get("FAILED", 0) + counts.get("DEAD_LETTER", 0),
        "retries": counts.get("RETRY_WAIT", 0),
        "throughput_60s": throughput,
        "duration": {
            "average": float(avg or 0),
            "p50": float(p50 or 0),
            "p95": float(p95 or 0),
            "p99": float(p99 or 0),
        },
        "average_queue_wait": float(wait or 0),
        "workers": workers,
        "worker_states": worker_states,
        "system_events": [
            {
                "id": e.id,
                "kind": e.kind,
                "worker_id": e.worker_id,
                "detail": e.detail,
                "at": e.created_at.isoformat(),
            }
            for e in session.scalars(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(30))
        ],
        "job_events": [
            {"id": e.id, "job_id": str(e.job_id), "state": e.state, "at": e.created_at.isoformat()}
            for e in session.scalars(select(Event).order_by(Event.id.desc()).limit(30))
        ],
    }


def prometheus(data: dict) -> str:
    values = {
        "jobs_submitted_total": data["submitted"],
        "jobs_completed_total": data["completed"],
        "jobs_failed_total": data["failed"],
        "jobs_retried_total": data["retries"],
        "queue_depth": data["queue_depth"],
        "queue_saturation_ratio": data["saturation"],
        "throughput_per_second": data["throughput_60s"],
        "queue_wait_seconds": data["average_queue_wait"],
    }
    lines = []
    for key, value in values.items():
        kind = "counter" if key.endswith("_total") else "gauge"
        lines.extend([f"# TYPE greyqueue_{key} {kind}", f"greyqueue_{key} {value}"])
    for key, value in data["duration"].items():
        lines.append(f'greyqueue_job_duration_seconds{{statistic="{key}"}} {value}')
    for state in ("HEALTHY", "SUSPECT", "DEAD", "DRAINING"):
        lines.append(f'greyqueue_workers{{state="{state}"}} {data["worker_states"].get(state, 0)}')
    for worker in data["workers"]:
        lines.append(
            f'greyqueue_worker_utilization{{worker="{worker["id"]}"}} {worker["running"] / worker["capacity"]}'
        )
        lines.append(
            f'greyqueue_worker_heartbeat_age_seconds{{worker="{worker["id"]}"}} {worker["heartbeat_age_seconds"]}'
        )
    return "\n".join(lines) + "\n"
