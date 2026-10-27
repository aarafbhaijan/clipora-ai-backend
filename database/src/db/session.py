"""Engine and session factory. The URL comes from DATABASE_URL (postgresql+psycopg://...)."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def engine(pool_size: int = 5) -> Engine:
    """One engine per process. Workers pass pool_size = slots + 2 (architecture.md section 9)."""
    global _engine, _factory
    if _engine is None:
        _engine = create_engine(database_url(), pool_size=pool_size, max_overflow=0, pool_pre_ping=True)
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction: commits on success, rolls back on error."""
    engine()
    assert _factory is not None
    with _factory() as session, session.begin():
        yield session
