"""SQL helpers shared by the scheduler, admission and metrics queries."""

from collections.abc import Iterable

from sqlalchemy import BindParameter, bindparam


def statuses(values: Iterable[str]) -> BindParameter:
    """An IN-list rendered as SQL literals, not bind parameters.

    psycopg prepares a statement after five executions; a generic plan with parameters
    cannot prove a partial index's `status IN (...)` predicate, so those plans would stop
    using ix_jobs_queue_*, ix_jobs_status and ix_jobs_succeeded. Literals keep it provable.
    """
    ordered = sorted(values)
    return bindparam(
        "statuses_" + "_".join(ordered).lower(), ordered, expanding=True, literal_execute=True
    )
