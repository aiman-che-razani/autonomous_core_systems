from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from greyqueue.config import settings


def make_sessions(url: str | None = None):
    engine = create_engine(url or settings().database_url, pool_pre_ping=True)
    return engine, sessionmaker(engine, expire_on_commit=False)
