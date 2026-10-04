"""Minimal NBD client (fixed newstyle handshake, simple replies).

Enough to read and write a VM disk exported by nbdkit's vddk plugin: NBD_OPT_GO,
READ, WRITE, FLUSH, WRITE_ZEROES and DISC. Requests are pipelined, and replies
are matched by handle because nbdkit may answer out of order.
"""

from __future__ import annotations

import socket
import struct
from collections import deque
from collections.abc import Iterable, Iterator

NBDMAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
OPT_REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698

FLAG_FIXED_NEWSTYLE = 1
FLAG_NO_ZEROES = 2

OPT_ABORT = 2
OPT_GO = 7
REP_ACK = 1
REP_INFO = 3
REP_ERR_BIT = 1 << 31
INFO_EXPORT = 0

CMD_READ = 0
CMD_WRITE = 1
CMD_DISC = 2
CMD_FLUSH = 3
CMD_WRITE_ZEROES = 6
CMD_FLAG_FUA = 1

TFLAG_READ_ONLY = 1 << 1
TFLAG_SEND_FLUSH = 1 << 2
TFLAG_SEND_WRITE_ZEROES = 1 << 6

MAX_REQUEST = 32 << 20


class NbdError(Exception):
    pass


class NbdClient:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._handle = 0
        self.size = 0
        self.flags = 0
        self.description = ""

    # --------------------------------------------------------------- connect

    @classmethod
    def connect_unix(cls, path: str, export: str = "", timeout: float = 300) -> NbdClient:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        c = cls(s)
        try:
            c._handshake(export)
        except BaseException:
            s.close()
            raise
        return c

    def _recv(self, n: int) -> bytes:
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = self._sock.recv_into(view[got:], n - got)
            if k == 0:
                raise NbdError("server closed the connection")
            got += k
        return bytes(buf)

    def _handshake(self, export: str) -> None:
        magic, opt_magic, hflags = struct.unpack(">8sQH", self._recv(18))
        if magic != NBDMAGIC or opt_magic != IHAVEOPT:
            raise NbdError("not an NBD newstyle server")
        if not hflags & FLAG_FIXED_NEWSTYLE:
            raise NbdError("server does not support fixed newstyle negotiation")
        cflags = FLAG_FIXED_NEWSTYLE | (hflags & FLAG_NO_ZEROES)
        self._sock.sendall(struct.pack(">I", cflags))

        name = export.encode()
        data = struct.pack(">I", len(name)) + name + struct.pack(">HH", 1, INFO_EXPORT)
        self._sock.sendall(struct.pack(">QII", IHAVEOPT, OPT_GO, len(data)) + data)
        while True:
            magic, opt, rtype, length = struct.unpack(">QIII", self._recv(20))
            if magic != OPT_REPLY_MAGIC or opt != OPT_GO:
                raise NbdError("bad option reply")
            payload = self._recv(length) if length else b""
            if rtype & REP_ERR_BIT:
                msg = payload.decode(errors="replace") or f"error {rtype & ~REP_ERR_BIT}"
                raise NbdError(f"export {export!r} refused: {msg}")
            if rtype == REP_INFO and len(payload) >= 12:
                (info,) = struct.unpack_from(">H", payload)
                if info == INFO_EXPORT:
                    self.size, self.flags = struct.unpack_from(">QH", payload, 2)
            elif rtype == REP_ACK:
                return

    @property
    def read_only(self) -> bool:
        return bool(self.flags & TFLAG_READ_ONLY)

    # --------------------------------------------------------------- commands

    def _send(self, cmd: int, offset: int, length: int, data: bytes = b"",
              flags: int = 0) -> int:
        self._handle += 1
        hdr = struct.pack(">IHHQQI", REQUEST_MAGIC, flags, cmd, self._handle, offset, length)
        self._sock.sendall(hdr + data if len(data) < 65536 else hdr)
        if len(data) >= 65536:
            self._sock.sendall(data)
        return self._handle

    def _reply(self) -> tuple[int, int]:
        magic, error, handle = struct.unpack(">IIQ", self._recv(16))
        if magic != SIMPLE_REPLY_MAGIC:
            raise NbdError(f"unexpected reply magic {magic:#x}")
        return handle, error

    def _check(self, error: int, what: str) -> None:
        if error:
            raise NbdError(f"{what} failed with errno {error}")

    def pread(self, offset: int, length: int) -> bytes:
        return next(self.pread_many([(offset, length)]))

    def pread_many(self, requests: Iterable[tuple[int, int]], depth: int = 8) -> Iterator[bytes]:
        """Read many ranges with up to ``depth`` requests in flight, yielding
        the data in request order."""
        it = iter(requests)
        inflight: deque[int] = deque()
        lengths: dict[int, tuple[int, int]] = {}
        done: dict[int, bytes] = {}
        exhausted = False
        while True:
            while not exhausted and len(inflight) < depth:
                try:
                    off, ln = next(it)
                except StopIteration:
                    exhausted = True
                    break
                if ln > MAX_REQUEST or off + ln > self.size:
                    raise NbdError(f"read {off}+{ln} is outside the export or too large")
                h = self._send(CMD_READ, off, ln)
                inflight.append(h)
                lengths[h] = (off, ln)
            if not inflight:
                return
            while inflight[0] not in done:
                h, err = self._reply()
                if h not in lengths:
                    raise NbdError(f"reply for unknown handle {h}")
                off, ln = lengths.pop(h)
                if err:
                    raise NbdError(f"read at {off} failed with errno {err}")
                done[h] = self._recv(ln)
            yield done.pop(inflight.popleft())

    def pwrite(self, offset: int, data: bytes, fua: bool = False) -> None:
        h = self._send(CMD_WRITE, offset, len(data), data, CMD_FLAG_FUA if fua else 0)
        rh, err = self._reply()
        if rh != h:
            raise NbdError("reply out of sequence")
        self._check(err, f"write at {offset}")

    def pwrite_many(self, writes: Iterable[tuple[int, bytes]], depth: int = 8) -> int:
        """Write many ranges with up to ``depth`` requests in flight. Returns
        bytes written."""
        inflight: dict[int, int] = {}
        total = 0

        def reap() -> None:
            h, err = self._reply()
            off = inflight.pop(h, None)
            if off is None:
                raise NbdError(f"reply for unknown handle {h}")
            self._check(err, f"write at {off}")

        for off, data in writes:
            if off + len(data) > self.size:
                raise NbdError(f"write {off}+{len(data)} is outside the export")
            inflight[self._send(CMD_WRITE, off, len(data), data)] = off
            total += len(data)
            if len(inflight) >= depth:
                reap()
        while inflight:
            reap()
        return total

    def write_zeroes(self, offset: int, length: int) -> None:
        if not self.flags & TFLAG_SEND_WRITE_ZEROES:
            step = 1 << 20
            for o in range(offset, offset + length, step):
                self.pwrite(o, bytes(min(step, offset + length - o)))
            return
        h = self._send(CMD_WRITE_ZEROES, offset, length)
        rh, err = self._reply()
        if rh != h:
            raise NbdError("reply out of sequence")
        self._check(err, f"write zeroes at {offset}")

    def flush(self) -> None:
        if not self.flags & TFLAG_SEND_FLUSH:
            return
        h = self._send(CMD_FLUSH, 0, 0)
        rh, err = self._reply()
        if rh != h:
            raise NbdError("reply out of sequence")
        self._check(err, "flush")

    def close(self) -> None:
        try:
            self._send(CMD_DISC, 0, 0)
        except OSError:
            pass
        self._sock.close()

    def __enter__(self) -> NbdClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
