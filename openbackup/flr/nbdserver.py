"""A read-only NBD server that serves the disks of a restore point straight
from the repository, so a backup can be inspected without restoring it.

Each disk is an export named by its disk key. Blocks are fetched and
verified from the repository on demand, with a small cache in front: a
filesystem walk touches the same metadata blocks over and over.

Clients are qemu (inside libguestfs) and our own NbdClient. Only what they
need is implemented: fixed newstyle negotiation, GO/INFO/EXPORT_NAME/LIST,
READ, FLUSH and DISC. Everything that would modify data is refused.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from collections import OrderedDict
from pathlib import Path

from ..repo.blockmap import BlockMap
from ..repo.repository import Repository

log = logging.getLogger("openbackup.flr")

NBDMAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
OPT_REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698

FLAG_FIXED_NEWSTYLE = 1
FLAG_NO_ZEROES = 2

OPT_EXPORT_NAME = 1
OPT_ABORT = 2
OPT_LIST = 3
OPT_INFO = 6
OPT_GO = 7

REP_ACK = 1
REP_SERVER = 2
REP_INFO = 3
REP_ERR_UNSUP = (1 << 31) | 1
REP_ERR_UNKNOWN = (1 << 31) | 6

INFO_EXPORT = 0
INFO_BLOCK_SIZE = 3

TFLAG_HAS_FLAGS = 1
TFLAG_READ_ONLY = 1 << 1
TFLAG_SEND_FLUSH = 1 << 2
TFLAG_CAN_MULTI_CONN = 1 << 8

CMD_READ = 0
CMD_DISC = 2
CMD_FLUSH = 3

EPERM = 1
EIO = 5
EINVAL = 22
MAX_READ = 32 << 20

EXPORT_FLAGS = TFLAG_HAS_FLAGS | TFLAG_READ_ONLY | TFLAG_SEND_FLUSH | TFLAG_CAN_MULTI_CONN


class _ChunkCache:
    def __init__(self, repo: Repository, entries: int = 64):
        self.repo = repo
        self.entries = entries
        self._lock = threading.Lock()
        self._data: OrderedDict[bytes, bytes] = OrderedDict()

    def get(self, chunk_id: bytes, length: int) -> bytes:
        with self._lock:
            hit = self._data.get(chunk_id)
            if hit is not None:
                self._data.move_to_end(chunk_id)
                return hit
        data = self.repo.read_chunk(chunk_id, length)
        with self._lock:
            self._data[chunk_id] = data
            if len(self._data) > self.entries:
                self._data.popitem(last=False)
        return data


class PointDisk:
    def __init__(self, cache: _ChunkCache, m: BlockMap):
        self.cache, self.map = cache, m
        self.size = m.capacity

    def read(self, offset: int, length: int) -> bytes:
        bs = self.map.block_size
        out = bytearray()
        end = offset + length
        while offset < end:
            i = offset // bs
            within = offset - i * bs
            n = min(end - offset, self.map.block_length(i) - within)
            block = self.cache.get(self.map.ids[i], self.map.block_length(i))
            out += block[within:within + n]
            offset += n
        return bytes(out)


class PointNbdServer:
    """Serve one restore point on a private unix socket until stopped."""

    def __init__(self, repo: Repository, point_id: str, socket_path: Path):
        self.repo = repo
        self.point_id = point_id
        self.socket_path = socket_path
        manifest = repo.load_manifest(point_id)
        cache = _ChunkCache(repo)
        self.disks = {key: PointDisk(cache, repo.load_map(point_id, key))
                      for key in manifest["disk_keys"]}
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> PointNbdServer:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name=f"flr-nbd-{self.point_id}")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    # ------------------------------------------------------------ protocol

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client went away")
            buf += chunk
        return bytes(buf)

    def _serve(self, conn: socket.socket) -> None:
        try:
            with conn:
                disk = self._negotiate(conn)
                if disk is not None:
                    self._transmission(conn, disk)
        except (ConnectionError, OSError):
            pass
        except Exception:
            log.exception("NBD connection for point %s failed", self.point_id)

    def _reply(self, conn, opt: int, rtype: int, data: bytes = b"") -> None:
        conn.sendall(struct.pack(">QIII", OPT_REPLY_MAGIC, opt, rtype, len(data)) + data)

    def _negotiate(self, conn: socket.socket) -> PointDisk | None:
        conn.sendall(NBDMAGIC + struct.pack(">QH", IHAVEOPT,
                                            FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES))
        (cflags,) = struct.unpack(">I", self._recv(conn, 4))
        no_zeroes = bool(cflags & FLAG_NO_ZEROES)
        while True:
            magic, opt, length = struct.unpack(">QII", self._recv(conn, 16))
            if magic != IHAVEOPT or length > 65536:
                return None
            data = self._recv(conn, length) if length else b""
            if opt == OPT_ABORT:
                self._reply(conn, opt, REP_ACK)
                return None
            if opt == OPT_LIST:
                for name in self.disks:
                    n = name.encode()
                    self._reply(conn, opt, REP_SERVER, struct.pack(">I", len(n)) + n)
                self._reply(conn, opt, REP_ACK)
                continue
            if opt == OPT_EXPORT_NAME:
                disk = self._lookup(data.decode(errors="replace"))
                if disk is None:
                    return None  # the protocol only allows hanging up here
                conn.sendall(struct.pack(">QH", disk.size, EXPORT_FLAGS) +
                             (b"" if no_zeroes else bytes(124)))
                return disk
            if opt in (OPT_INFO, OPT_GO):
                (nlen,) = struct.unpack_from(">I", data)
                name = data[4:4 + nlen].decode(errors="replace")
                disk = self._lookup(name)
                if disk is None:
                    self._reply(conn, opt, REP_ERR_UNKNOWN, f"no export {name!r}".encode())
                    continue
                self._reply(conn, opt, REP_INFO,
                            struct.pack(">HQH", INFO_EXPORT, disk.size, EXPORT_FLAGS))
                self._reply(conn, opt, REP_INFO,
                            struct.pack(">HIII", INFO_BLOCK_SIZE, 1, 4096, MAX_READ))
                self._reply(conn, opt, REP_ACK)
                if opt == OPT_GO:
                    return disk
                continue
            self._reply(conn, opt, REP_ERR_UNSUP)

    def _lookup(self, name: str) -> PointDisk | None:
        if name == "" and len(self.disks) == 1:
            return next(iter(self.disks.values()))
        return self.disks.get(name)

    def _transmission(self, conn: socket.socket, disk: PointDisk) -> None:
        while True:
            magic, _flags, cmd, handle, offset, length = struct.unpack(
                ">IHHQQI", self._recv(conn, 28))
            if magic != REQUEST_MAGIC:
                return
            if cmd == CMD_DISC:
                return
            if cmd == CMD_READ:
                if length > MAX_READ or offset + length > disk.size:
                    conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, EINVAL, handle))
                    continue
                try:
                    data = disk.read(offset, length)
                except Exception:
                    log.exception("read %s+%s of point %s failed", offset, length,
                                  self.point_id)
                    conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, EIO, handle))
                    continue
                conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 0, handle) + data)
            elif cmd == CMD_FLUSH:
                conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 0, handle))
            else:
                # WRITE, TRIM, WRITE_ZEROES...: a backup is never modified. A
                # WRITE carries a payload that must be drained first.
                if cmd == 1:
                    self._recv(conn, length)
                conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, EPERM, handle))
