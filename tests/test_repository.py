"""Tests for choosing and opening a repository destination."""

import json
import os

import pytest

from openbackup.repo.repository import (
    CONFIG_PATH, Destination, Repository, RepositoryConfig, RepositoryError,
    open_repository,
)


@pytest.fixture
def local(tmp_path):
    def _open(create=False, key=None):
        return open_repository(
            Destination(kind="local", path=str(tmp_path / "repo")),
            create=create, key=key, index_dir=tmp_path / "idx",
        )
    return _open


# -- destinations -----------------------------------------------------------

def test_local_destination_needs_a_path():
    with pytest.raises(ValueError, match="needs a path"):
        Destination(kind="local")


def test_nfs_destination_needs_server_and_export():
    with pytest.raises(ValueError, match="needs server and export"):
        Destination(kind="nfs")
    with pytest.raises(ValueError, match="needs export"):
        Destination(kind="nfs", server="10.0.0.5")


def test_unknown_destination_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown destination kind"):
        Destination(kind="s3", path="bucket")


def test_nfs_mountpoint_is_derived_and_stable():
    """Repeat runs must reuse the same mountpoint rather than piling up."""
    a = Destination(kind="nfs", server="10.0.0.5", export="/vol/backup")
    b = Destination(kind="nfs", server="10.0.0.5", export="/vol/backup")
    assert a.mountpoint == b.mountpoint
    assert a.mountpoint.startswith("/mnt/openbackup/")


def test_explicit_mountpoint_is_respected():
    d = Destination(kind="nfs", server="10.0.0.5", export="/vol",
                    mountpoint="/srv/mine")
    assert d.mountpoint == "/srv/mine"


def test_unknown_config_fields_are_rejected():
    """A typo in a destination must not be silently ignored."""
    with pytest.raises(ValueError, match="unknown destination fields"):
        Destination.from_dict({"kind": "local", "path": "/x", "pathh": "/y"})


def test_describe_is_readable():
    assert Destination(kind="local", path="/backup").describe() == "/backup"
    assert Destination(kind="nfs", server="10.0.0.5", export="/vol",
                       path="site-a").describe() == "nfs://10.0.0.5/vol/site-a"


# -- lifecycle --------------------------------------------------------------

def test_create_then_reopen(local):
    repo = local(create=True)
    digest = repo.store.put(b"payload" * 1000).hash
    repo.close()

    again = local()
    try:
        assert again.store.get(digest) == b"payload" * 1000
    finally:
        again.close()


def test_opening_a_missing_repository_is_an_error(local):
    with pytest.raises(RepositoryError, match="no repository at"):
        local()


def test_creating_over_an_existing_repository_is_refused(local):
    local(create=True).close()
    with pytest.raises(RepositoryError, match="already holds a repository"):
        Repository(
            Destination(kind="local", path=local().destination.path),
        ).open(create=True, config=RepositoryConfig())


def test_settings_travel_with_the_repository(local, tmp_path):
    """Chunk and pack size describe data already written, so a fresh install
    must pick them up rather than write incompatible chunks alongside."""
    repo = local(create=True)
    repo.close()
    raw = json.loads((tmp_path / "repo" / CONFIG_PATH).read_bytes())
    assert raw["chunk_size"] and raw["pack_size"]

    reopened = local()
    try:
        assert reopened.config.pack_size == raw["pack_size"]
    finally:
        reopened.close()


def test_future_repository_version_is_refused():
    blob = json.dumps({"version": 99, "chunk_size": 1, "pack_size": 1}).encode()
    with pytest.raises(RepositoryError, match="this build speaks"):
        RepositoryConfig.from_json(blob)


# -- encryption -------------------------------------------------------------

def test_encrypted_repository_requires_its_key(local):
    key = os.urandom(32)
    repo = local(create=True, key=key)
    digest = repo.store.put(b"secret" * 500).hash
    repo.close()

    with pytest.raises(RepositoryError, match="no key was supplied"):
        local()

    with_key = local(key=key)
    try:
        assert with_key.store.get(digest) == b"secret" * 500
    finally:
        with_key.close()


def test_key_supplied_for_a_plaintext_repository_is_refused(local):
    """Otherwise the key would be silently ignored and the operator would
    believe their backups were encrypted."""
    local(create=True).close()
    with pytest.raises(RepositoryError, match="not encrypted"):
        local(key=os.urandom(32))


# -- failure handling -------------------------------------------------------

def test_a_failed_job_does_not_commit_its_pack(local, tmp_path):
    repo = local(create=True)
    with pytest.raises(RuntimeError):
        with repo:
            repo.store.put(b"partial work")
            raise RuntimeError("job failed")
    packs = list((tmp_path / "repo").rglob("*.pack"))
    assert not packs


def test_a_successful_job_commits_on_exit(local, tmp_path):
    repo = local(create=True)
    with repo:
        repo.store.put(b"good work" * 1000)
    assert list((tmp_path / "repo").rglob("*.pack"))
