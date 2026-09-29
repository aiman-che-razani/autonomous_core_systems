from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from greyqueue.config import settings

TIMEOUTS = (
    "-cstatement_timeout=10000 -clock_timeout=5000 -cidle_in_transaction_session_timeout=15000"
)


def make_sessions(url: str | None = None):
    parsed = make_url(url or settings().database_url)
    existing = parsed.query.get("options", "")
    if isinstance(existing, tuple):
        existing = " ".join(existing)
    # Later -c settings win, so options already in the URL override these defaults.
    parsed = parsed.update_query_dict({"options": f"{TIMEOUTS} {existing}".strip()})
    engine = create_engine(
        parsed,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        pool_timeout=5,
        connect_args={"connect_timeout": 3},
        # Tracebacks must not carry job args, metadata or results into logs.
        hide_parameters=True,
    )
    return engine, sessionmaker(engine, expire_on_commit=False)
