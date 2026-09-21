"""Tests for repository storage and NFS mount handling."""

import subprocess

import pytest

from openbackup.repo.backend import (
    BackendError, DEFAULT_NFS_OPTIONS, FilesystemBackend, NfsMount,
)


@pytest.fixture
def backend(tmp_path):
    be = FilesystemBackend(tmp_path / "repo")
    be.init()
    return be


# -- filesystem -------------------------------------------------------------

def test_write_then_read(backend):
    backend.write("packs/ab/x.pack", b"hello world")
    assert backend.read("packs/ab/x.pack") == b"hello world"


def test_range_and_tail_reads(backend):
    backend.write("a.pack", bytes(range(256)))
    assert backend.read_range("a.pack", 10, 4) == bytes(range(10, 14))
    assert backend.read_tail("a.pack", 4) == bytes(range(252, 256))


def test_tail_longer_than_the_file_returns_the_file(backend):
    backend.write("small", b"abc")
    assert backend.read_tail("small", 100) == b"abc"


def test_short_range_read_is_an_error(backend):
    """A truncated read must not pass for a chunk."""
    backend.write("a.pack", b"12345")
    with pytest.raises(BackendError, match="expected"):
        backend.read_range("a.pack", 3, 10)


def test_missing_file_reports_clearly(backend):
    with pytest.raises(BackendError, match="not found"):
        backend.read("nope.pack")


def test_path_traversal_is_blocked(backend):
    """Repository paths come from indexes and metadata; escaping the root
    would write backup data somewhere nobody expects."""
    for bad in ("../../etc/passwd", "packs/../../outside"):
        with pytest.raises(BackendError, match="escapes"):
            backend.path(bad)


def test_write_is_atomic_on_failure(backend, monkeypatch):
    """A crash mid-write must not leave a file the index would trust."""
    import openbackup.repo.backend as mod

    monkeypatch.setattr(mod.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        backend.write("packs/x.pack", b"data")
    assert not backend.exists("packs/x.pack")


def test_interrupted_writes_leave_only_temp_files(backend, monkeypatch):
    import openbackup.repo.backend as mod

    monkeypatch.setattr(mod.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    for _ in range(3):
        with pytest.raises(OSError):
            backend.write("x.pack", b"data")
    monkeypatch.undo()
    assert backend.cleanup_temp() == 0   # failures unlink their own temp file


def test_delete_and_exists(backend):
    backend.write("a", b"1")
    assert backend.exists("a")
    assert backend.delete("a") is True
    assert backend.delete("a") is False


def test_list_returns_relative_paths(backend):
    backend.write("packs/ab/one.pack", b"1")
    backend.write("packs/cd/two.pack", b"2")
    found = {f for f in backend.list("packs")}
    assert found == {"packs/ab/one.pack", "packs/cd/two.pack"}


def test_free_space_is_reported(backend):
    assert backend.free_space() > 0


# -- NFS validation ---------------------------------------------------------

@pytest.mark.parametrize("server", [
    "10.0.0.5; rm -rf /", "host && reboot", "$(id)", "a b", "",
])
def test_malicious_server_names_are_rejected(server):
    """Configuration must not be able to become a command."""
    with pytest.raises(ValueError, match="invalid NFS server"):
        NfsMount(server, "/export", "/mnt/x")


@pytest.mark.parametrize("export", ["/v$(id)", "relative/path", "/v;reboot", ""])
def test_malicious_export_paths_are_rejected(export):
    with pytest.raises(ValueError, match="invalid NFS export"):
        NfsMount("nfs.lab", export, "/mnt/x")


def test_malicious_options_are_rejected():
    with pytest.raises(ValueError, match="invalid NFS mount options"):
        NfsMount("nfs.lab", "/export", "/mnt/x", options="hard,$(reboot)")


def test_valid_specs_are_accepted():
    m = NfsMount("10.0.0.5", "/vol/backup", "/mnt/x")
    assert m.spec == "10.0.0.5:/vol/backup"
    NfsMount("nfs-01.lab.local", "/exports/backup-01", "/mnt/x")
    NfsMount("[fd00::1]", "/vol", "/mnt/x")


def test_default_options_use_a_hard_mount():
    """A soft mount returns EIO on timeout, which during a restore means
    silently incomplete data. Backups must block and wait instead."""
    assert "hard" in DEFAULT_NFS_OPTIONS
    assert "soft" not in DEFAULT_NFS_OPTIONS


# -- NFS lifecycle ----------------------------------------------------------

def _fake_mounts(entries):
    return staticmethod(lambda: entries)


def test_detects_an_existing_mount(monkeypatch, tmp_path):
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path)
    monkeypatch.setattr(NfsMount, "_mount_table",
                        _fake_mounts([("10.0.0.5:/vol/backup", str(tmp_path), "nfs4")]))
    assert m.is_mounted()
    assert m.mounted_source() == "10.0.0.5:/vol/backup"


def test_refuses_to_stack_over_a_different_export(monkeypatch, tmp_path):
    """Mounting over someone else's export would hide their data and silently
    write ours somewhere unintended."""
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path)
    monkeypatch.setattr(NfsMount, "_mount_table",
                        _fake_mounts([("10.0.0.9:/other", str(tmp_path), "nfs4")]))
    with pytest.raises(BackendError, match="refusing to stack"):
        m.mount()


def test_existing_matching_mount_is_adopted_not_remounted(monkeypatch, tmp_path):
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path)
    monkeypatch.setattr(NfsMount, "_mount_table",
                        _fake_mounts([("10.0.0.5:/vol/backup", str(tmp_path), "nfs4")]))
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    m.mount()
    assert not called, "should not re-mount an export already in place"


def test_we_never_unmount_what_we_did_not_mount(monkeypatch, tmp_path):
    """Tearing down an operator's mount mid-job is worse than leaving ours up."""
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path)
    monkeypatch.setattr(NfsMount, "_mount_table",
                        _fake_mounts([("10.0.0.5:/vol/backup", str(tmp_path), "nfs4")]))
    m.mount()
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    m.unmount()
    assert not called


def test_refuses_to_mount_over_a_non_empty_directory(monkeypatch, tmp_path):
    (tmp_path / "existing-file").write_text("important")
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path)
    monkeypatch.setattr(NfsMount, "_mount_table", _fake_mounts([]))
    with pytest.raises(BackendError, match="not empty"):
        m.mount()


def test_mount_command_is_built_as_argument_list(monkeypatch, tmp_path):
    """Never a shell string: that is what keeps the validation meaningful."""
    target = tmp_path / "mnt"
    m = NfsMount("10.0.0.5", "/vol/backup", target, version="4.1")
    monkeypatch.setattr(NfsMount, "_mount_table", _fake_mounts([]))
    captured = {}

    class Result:
        returncode = 0
        stderr = stdout = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        monkeypatch.setattr(
            NfsMount, "_mount_table",
            _fake_mounts([("10.0.0.5:/vol/backup", str(target), "nfs4")]))
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    m.mount()
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "mount" and "-t" in cmd and "nfs" in cmd
    assert "10.0.0.5:/vol/backup" in cmd
    assert any(part.startswith("vers=4.1") for part in cmd)


def test_mount_failure_surfaces_the_reason(monkeypatch, tmp_path):
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path / "mnt")
    monkeypatch.setattr(NfsMount, "_mount_table", _fake_mounts([]))

    class Result:
        returncode = 32
        stderr = "mount.nfs: access denied by server"
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(BackendError, match="access denied"):
        m.mount()


def test_missing_mount_binary_is_explained(monkeypatch, tmp_path):
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path / "mnt")
    monkeypatch.setattr(NfsMount, "_mount_table", _fake_mounts([]))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(BackendError, match="nfs-utils"):
        m.mount()


def test_unreachable_server_times_out_with_a_clear_error(monkeypatch, tmp_path):
    m = NfsMount("10.0.0.5", "/vol/backup", tmp_path / "mnt")
    monkeypatch.setattr(NfsMount, "_mount_table", _fake_mounts([]))

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="mount", timeout=120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(BackendError, match="unreachable"):
        m.mount()
