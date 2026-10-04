from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from openbackup.db import migrate
from openbackup.db.models import Base


def _engine(tmp_path, name="db.sqlite"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def test_fresh_database_matches_models(tmp_path):
    eng = _engine(tmp_path)
    migrate.upgrade(eng)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                Base.metadata)
    assert diff == []


def test_database_from_before_migrations_is_stamped_and_upgraded(tmp_path):
    from alembic import command

    eng = _engine(tmp_path)
    # Recreate what create_all produced before migrations existed: the
    # baseline schema with no alembic_version table, holding real data.
    with eng.begin() as conn:
        command.upgrade(migrate._config(conn), migrate.BASELINE)
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("INSERT INTO vcenters (name, host, port, username, password_enc, "
                          "thumbprint, created_at) VALUES ('vc', 'h', 443, 'u', 'x', 't', "
                          "'2026-10-01 00:00:00')"))
        conn.execute(text("INSERT INTO repositories (name, kind, path, nfs_server, nfs_export, "
                          "nfs_options, encrypted, created_at) VALUES ('r', 'local', '/x', '', "
                          "'', '', 0, '2026-10-01 00:00:00')"))
        conn.execute(text("INSERT INTO jobs (name, description, vcenter_id, repository_id, vms, "
                          "enabled, retention_points, retention_days, quiesce, active_full_days,"
                          " created_at) VALUES ('j', '', 1, 1, '[]', 1, 14, 0, 1, 0, "
                          "'2026-10-01 00:00:00')"))
    migrate.upgrade(eng)
    with eng.connect() as conn:
        assert "kube_clusters" in inspect(conn).get_table_names()
        kind, selection = conn.execute(text("SELECT kind, selection FROM jobs")).one()
    assert kind == "vsphere" and selection == "{}"
    migrate.upgrade(eng)  # idempotent


def test_downgrade_to_baseline(tmp_path):
    from alembic import command

    eng = _engine(tmp_path)
    migrate.upgrade(eng)
    with eng.begin() as conn:
        command.downgrade(migrate._config(conn), migrate.BASELINE)
        assert "kube_clusters" not in inspect(conn).get_table_names()
