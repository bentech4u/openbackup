from __future__ import annotations

import http.server
import os
import shutil
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from openbackup.engine.context import NullContext
from openbackup.engine.filerestore import FileRestoreError, restore_files
from openbackup.vsphere.guestops import GuestError, GuestFiles, GuestInfo, _fingerprint, put_file

STAMP = "20261004-0632"


class FakeSession:
    """Browse paths /v0/... map onto a local 'backup' tree."""

    def __init__(self, root: Path, guest_path: str | None = "/"):
        self.root = root
        self.volumes = [{"id": "v0", "guest_path": guest_path, "mounted": True}]

    def _p(self, path: str) -> Path:
        assert path.startswith("/v0")
        return self.root / path[len("/v0"):].lstrip("/")

    def volume_of(self, path):
        return self.volumes[0]

    def _entry(self, p: Path) -> dict:
        st = p.lstat()
        return {"name": p.name, "type": "dir" if p.is_dir() else "file", "size": st.st_size,
                "mode": st.st_mode & 0o7777, "uid": 0, "gid": 0, "mtime": int(st.st_mtime)}

    def stat(self, path):
        return self._entry(self._p(path))

    def walk(self, path):
        base = self._p(path)
        return [{**self._entry(p), "path": "/" + p.relative_to(base).as_posix()}
                for p in sorted(base.rglob("*"))]

    def download(self, path, dest):
        shutil.copyfile(self._p(path), dest)
        return self.stat(path)


class FakeGuest(GuestFiles):
    """A Linux guest whose filesystem is a local directory."""

    def __init__(self, root: Path, windows: bool = False):
        self.root = root
        self.info = GuestInfo("windowsGuest" if windows else "linuxGuest", "", "")
        self.uploads: list[tuple[str, bool]] = []

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def exists(self, path):
        p = self._p(path)
        return ("directory" if p.is_dir() else "file") if p.exists() else None

    def mkdirs(self, path):
        self._p(path).mkdir(parents=True, exist_ok=True)

    def upload(self, local, guest_path, overwrite, meta):
        p = self._p(guest_path)
        if p.exists() and not overwrite:
            raise GuestError(f"{guest_path} already exists")
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, p)
        self.uploads.append((guest_path, overwrite))


@pytest.fixture
def env(tmp_path):
    backup = tmp_path / "backup"
    (backup / "home/alice/docs").mkdir(parents=True)
    (backup / "home/alice/docs/report.txt").write_text("from backup")
    (backup / "home/alice/docs/sub").mkdir()
    (backup / "home/alice/docs/sub/a.bin").write_bytes(os.urandom(5000))
    guest = tmp_path / "guest"
    (guest / "home/alice/docs").mkdir(parents=True)
    (guest / "home/alice/docs/report.txt").write_text("damaged")
    return FakeSession(backup), FakeGuest(guest), tmp_path


def run(env, items, conflict, target=None):
    s, g, tmp = env
    return restore_files(s, g, items, conflict, target, STAMP, tmp / "staging", NullContext())


def test_overwrite_replaces_the_file(env):
    s, g, _ = env
    res = run(env, ["/v0/home/alice/docs/report.txt"], "overwrite")
    assert (g.root / "home/alice/docs/report.txt").read_text() == "from backup"
    assert res.files == 1 and g.uploads == [("/home/alice/docs/report.txt", True)]


def test_rename_keeps_the_existing_file(env):
    s, g, _ = env
    res = run(env, ["/v0/home/alice/docs/report.txt"], "rename")
    assert (g.root / "home/alice/docs/report.txt").read_text() == "damaged"
    restored = g.root / f"home/alice/docs/report_restored_{STAMP}.txt"
    assert restored.read_text() == "from backup"
    assert res.renamed == [f"/home/alice/docs/report_restored_{STAMP}.txt"]
    # A second rename does not clobber the first.
    run(env, ["/v0/home/alice/docs/report.txt"], "rename")
    assert (g.root / f"home/alice/docs/report_restored_{STAMP}_2.txt").exists()


def test_skip_leaves_existing_alone(env):
    s, g, _ = env
    res = run(env, ["/v0/home/alice/docs/report.txt"], "skip")
    assert (g.root / "home/alice/docs/report.txt").read_text() == "damaged"
    assert res.skipped == 1 and res.files == 0


def test_missing_file_restores_under_its_own_name_in_any_mode(env):
    s, g, _ = env
    (g.root / "home/alice/docs/report.txt").unlink()
    run(env, ["/v0/home/alice/docs/report.txt"], "rename")
    assert (g.root / "home/alice/docs/report.txt").read_text() == "from backup"


def test_folder_restore_with_rename(env):
    s, g, _ = env
    res = run(env, ["/v0/home/alice/docs"], "rename")
    new = g.root / f"home/alice/docs_restored_{STAMP}"
    assert (new / "report.txt").read_text() == "from backup"
    assert (new / "sub/a.bin").read_bytes() == (s.root / "home/alice/docs/sub/a.bin").read_bytes()
    assert (g.root / "home/alice/docs/report.txt").read_text() == "damaged"
    assert res.files == 2 and res.folders == 2


def test_folder_restore_skip_skips_an_existing_folder(env):
    s, g, _ = env
    res = run(env, ["/v0/home/alice/docs"], "skip")
    # The folder exists, so the whole item is skipped.
    assert res.skipped == 1 and not (g.root / "home/alice/docs/sub").exists()


def test_target_folder(env):
    s, g, _ = env
    run(env, ["/v0/home/alice/docs/report.txt", "/v0/home/alice/docs/sub"], "overwrite",
        target="/tmp/recovered")
    assert (g.root / "tmp/recovered/report.txt").read_text() == "from backup"
    assert (g.root / "tmp/recovered/sub/a.bin").exists()


def test_unmapped_volume_needs_a_target(env):
    s, g, tmp = env
    s.volumes[0]["guest_path"] = None
    with pytest.raises(FileRestoreError, match="target folder"):
        run(env, ["/v0/home/alice/docs/report.txt"], "overwrite")


def test_windows_paths():
    g = FakeGuest(Path("/nonexistent"), windows=True)
    assert g.join("C:", "/Users/bob/a.docx") == r"C:\Users\bob\a.docx"
    assert g.join(r"D:\Restore", "/x") == r"D:\Restore\x"
    assert g.renamed(r"C:\Users\bob\a.docx", STAMP) == rf"C:\Users\bob\a_restored_{STAMP}.docx"
    assert g.renamed(r"C:\Users\bob\.profile", STAMP) == rf"C:\Users\bob\.profile_restored_{STAMP}"


# ----------------------------------------------------------- pinned uploads


@pytest.fixture
def https_server(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=esxi"],
                   check=True, capture_output=True)
    received: list[bytes] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    srv.handle_error = lambda *a: None  # the refused-certificate test hangs up on purpose
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    yield f"https://127.0.0.1:{srv.server_port}/guestFile?id=1&token=x", _fingerprint(der), received
    srv.shutdown()


def test_upload_to_pinned_host(https_server, tmp_path):
    url, thumb, received = https_server
    f = tmp_path / "f"
    f.write_bytes(os.urandom(3 << 20))
    put_file(url, f, thumb)
    assert received == [f.read_bytes()]


def test_upload_refuses_wrong_certificate(https_server, tmp_path):
    url, _thumb, received = https_server
    f = tmp_path / "f"
    f.write_bytes(b"secret")
    with pytest.raises(GuestError, match="refusing"):
        put_file(url, f, ":".join(["00"] * 20))
    assert received == []
