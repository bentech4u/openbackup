"""Alembic environment. Run through ``openbackup db upgrade`` (or
``openbackup.db.migrate.upgrade``), which supplies the connection."""

from __future__ import annotations

from alembic import context

from openbackup.db.models import Base

target_metadata = Base.metadata


def run() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("run migrations through openbackup.db.migrate")
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


run()
