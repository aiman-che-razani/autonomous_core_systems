"""SQL helpers shared by the scheduler, admission and metrics queries."""

from collections.abc import Iterable

from sqlalchemy import BindParameter, bindparam


def statuses(values: Iterable[str]) -> BindParameter:
    """An IN-list rendered as SQL literals, not bind parameters.

    psycopg prepares a statement after five executions; a generic plan with parameters
    cannot prove a partial index's `IN (...)`/`=` predicate, so those plans would stop using
    ix_jobs_queue_*, ix_jobs_succeeded, ix_jobs_waiting_children, ix_workers_live_last_seen
    and ix_events_retries. Literals keep it provable. Other hot IN-lists use it too, for
    stable plans.
    """
    ordered = sorted(values)
    return bindparam(
        "statuses_" + "_".join(ordered).lower(), ordered, expanding=True, literal_execute=True
    )
