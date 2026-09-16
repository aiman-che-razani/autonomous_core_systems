from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from greyqueue.config import settings


def make_sessions(url: str | None = None):
    parsed = make_url(url or settings().database_url)
    options = parsed.query.get("options", "") + " -cstatement_timeout=10000 -clock_timeout=5000"
    parsed = parsed.update_query_dict({"options": options})
    engine = create_engine(
        parsed,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        pool_timeout=5,
        connect_args={"connect_timeout": 3},
    )
    return engine, sessionmaker(engine, expire_on_commit=False)
