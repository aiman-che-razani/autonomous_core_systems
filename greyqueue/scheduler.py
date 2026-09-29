"""SQL policies share the same locked claim path and capacity checks.

Each ORDER BY matches a partial index (ix_jobs_queue_fifo / ix_jobs_queue_priority), so a
claim reads the head of the queue instead of sorting every eligible row.
"""

from datetime import datetime
from typing import Protocol

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import aliased

from greyqueue.models import Job, Worker
from greyqueue.sql import statuses


class Scheduler(Protocol):
    def query(self, worker: Worker, timestamp: datetime) -> Select: ...


class FIFOScheduler:
    def query(self, worker: Worker, timestamp: datetime) -> Select:
        parent = aliased(Job)
        ready_parent = (
            select(parent.id)
            .where(parent.id == Job.depends_on, parent.status == "SUCCEEDED")
            .exists()
        )
        return (
            select(Job)
            .where(
                Job.status.in_(statuses(["QUEUED", "RETRY_WAIT"])),
                Job.available_at <= timestamp,
                Job.task.in_(worker.capabilities),
                or_(Job.depends_on.is_(None), ready_parent),
            )
            .order_by(Job.created_at, Job.id)
        )


class PriorityScheduler(FIFOScheduler):
    def query(self, worker: Worker, timestamp: datetime) -> Select:
        return (
            super()
            .query(worker, timestamp)
            .order_by(None)
            .order_by(Job.priority.desc(), Job.created_at, Job.id)
        )


class CapacityAwareScheduler(PriorityScheduler):
    """Pull-based priority placement: caller advertises a free slot and task capabilities.

    Every policy applies the same capacity bound; this policy names that placement
    contract rather than pretending to estimate remote CPU availability.
    """


def scheduler(name: str) -> Scheduler:
    return {
        "fifo": FIFOScheduler,
        "priority": PriorityScheduler,
        "capacity": CapacityAwareScheduler,
    }[name]()
