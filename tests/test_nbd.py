from __future__ import annotations

import os
import shutil

import pytest

from openbackup.nbd.client import NbdError
from openbackup.nbd.nbdkit import Nbdkit, VddkTarget, vddk_args

pytestmark = pytest.mark.skipif(shutil.which("nbdkit") is None, reason="nbdkit not installed")

MiB = 1 << 20


def test_read_write_round_trip():
    with Nbdkit(["memory", "size=16M"], readonly=False) as srv, srv.connect() as c:
        assert c.size == 16 * MiB
        assert not c.read_only
        data = os.urandom(3 * MiB)
        c.pwrite(MiB, data)
        c.flush()
        assert c.pread(MiB, 3 * MiB) == data
        assert c.pread(0, MiB) == bytes(MiB)


def test_pipelined_reads_come_back_in_order(tmp_path):
    img = tmp_path / "disk.img"
    data = os.urandom(20 * MiB)
    img.write_bytes(data)
    with Nbdkit(["file", f"file={img}"]) as srv, srv.connect() as c:
        assert c.read_only
        reqs = [(i * MiB, MiB) for i in reversed(range(20))]
        got = list(c.pread_many(reqs, depth=6))
        assert got == [data[o:o + n] for o, n in reqs]


def test_pipelined_writes(tmp_path):
    with Nbdkit(["memory", "size=8M"], readonly=False) as srv, srv.connect() as c:
        blocks = [(i * MiB, os.urandom(MiB)) for i in range(8)]
        assert c.pwrite_many(blocks, depth=4) == 8 * MiB
        c.write_zeroes(2 * MiB, MiB)
        assert c.pread(0, MiB) == blocks[0][1]
        assert c.pread(2 * MiB, MiB) == bytes(MiB)


def test_readonly_export_rejects_writes():
    with Nbdkit(["memory", "size=1M"]) as srv, srv.connect() as c:
        with pytest.raises(NbdError):
            c.pwrite(0, b"x" * 512)


def test_read_past_end_is_refused():
    with Nbdkit(["memory", "size=1M"]) as srv, srv.connect() as c:
        with pytest.raises(NbdError):
            c.pread(MiB - 10, 20)


def test_startup_failure_is_reported():
    with pytest.raises(NbdError, match="nbdkit"):
        Nbdkit(["file", "file=/nonexistent/disk.img"]).start()


def test_secret_is_passed_by_fd_not_argv(tmp_path):
    # The sh plugin lets us observe what nbdkit was given; like the vddk
    # plugin, it resolves password=-FD by reading the inherited descriptor.
    script = tmp_path / "plugin.sh"
    out = tmp_path / "seen"
    script.write_text(f"""#!/bin/sh
case "$1" in
  config) v="$3"; case "$v" in -[0-9]*) v=$(eval "cat <&${{v#-}}") ;; esac
          echo "$2=$v" >> {out} ;;
  get_size) echo 1048576 ;;
  pread) dd if=/dev/zero count=$3 iflag=count_bytes status=none ;;
  *) exit 2 ;;
esac
""")
    script.chmod(0o755)
    with Nbdkit(["sh", str(script)], secret="s3cret-pw") as srv, srv.connect() as c:
        c.pread(0, 512)
        cmdline = open(f"/proc/{srv._proc.pid}/cmdline", "rb").read()
    assert b"s3cret-pw" not in cmdline
    assert "password=s3cret-pw" in out.read_text()


def test_vddk_args_shape():
    t = VddkTarget(libdir="/opt/openvddk", server="vc", user="u", password="p",
                   thumbprint="AA:BB", vm_moref="vm-1", file="[ds] a/a.vmdk",
                   snapshot_moref="snapshot-9")
    args = vddk_args(t)
    assert args[0] == "vddk"
    assert "vm=moref=vm-1" in args and "snapshot=snapshot-9" in args
    assert not any(a.startswith("password") for a in args)


def test_crash_is_reported_as_a_crash(tmp_path):
    # A plugin that dies with SIGSEGV on the first read, like a faulty VDDK.
    script = tmp_path / "crash.sh"
    script.write_text("""#!/bin/sh
case "$1" in
  get_size) echo 1048576 ;;
  pread) kill -SEGV $PPID; sleep 5 ;;
  *) exit 2 ;;
esac
""")
    script.chmod(0o755)
    with Nbdkit(["sh", str(script)]) as srv, srv.connect() as c:
        with pytest.raises((NbdError, OSError)):
            c.pread(0, 512)
        reason = srv.failure_reason()
    assert "crashed (SIGSEGV)" in reason
