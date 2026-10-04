"""Schema migrations.

Databases created before migrations existed (by ``create_all``) are stamped
with the baseline revision the first time, then upgraded like any other.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

BASELINE = "0001"
SCRIPTS = Path(__file__).with_name("migrations")


def _config(connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPTS))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade(engine: Engine) -> None:
    # The API and the worker both upgrade at startup; serialise them.
    lock_path = None
    if engine.url.get_backend_name() == "sqlite" and engine.url.database:
        lock_path = Path(engine.url.database).with_suffix(".migrate.lock")
    with _locked(lock_path):
        _upgrade(engine)


@contextmanager
def _locked(path: Path | None) -> Iterator[None]:
    if path is None:
        yield
        return
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _upgrade(engine: Engine) -> None:
    with engine.begin() as conn:
        cfg = _config(conn)
        tables = set(inspect(conn).get_table_names())
        if "alembic_version" not in tables and "users" in tables:
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")


def revision(engine: Engine, message: str) -> None:
    """Developer helper: autogenerate a new revision against ``engine``."""
    with engine.begin() as conn:
        command.revision(_config(conn), message=message, autogenerate=True)
