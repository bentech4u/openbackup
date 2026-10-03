from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models import Base


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    if settings.db_url.startswith("sqlite:///"):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    return make_engine(settings.db_url)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def init_db(engine: Engine | None = None) -> None:
    Base.metadata.create_all(engine or get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
