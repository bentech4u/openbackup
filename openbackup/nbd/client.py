"""A pure-Python NBD client, used to move VM disk blocks to and from nbdkit.

Why hand-rolled rather than libnbd's bindings: RHEL 9 ships python3-libnbd built
against the system Python 3.9 interpreter, so it cannot be imported from our
3.12 virtualenv. Binding libnbd through ctypes would work but drags in ABI
fragility for the async API, which is exactly the part we lean on. The NBD
fixed-newstyle protocol is small and stable, so we speak it directly.

The engine below is non-blocking and pipelined. That is not premature
optimisation: a backup reading over NBDSSL from an ESXi host has a long enough
round-trip that a strictly serial request/response loop leaves most of the link
idle. Keeping several commands in flight is the difference between ~80 MB/s and
link speed.
"""

from __future__ import annotations

import errno
import os
import selectors
import socket
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import protocol as p


class NbdError(Exception):
    """Any protocol-level or server-reported NBD failure."""


class NbdServerError(NbdError):
    """The server completed a command with a non-zero error code."""

    def __init__(self, cmd_type: int, offset: int, length: int, code: int):
        self.code = code
        name = p.CMD_NAMES.get(cmd_type, f"cmd {cmd_type}")
        super().__init__(
            f"{name} at offset {offset} length {length} failed: "
            f"{os.strerror(code) if code else 'unknown'} (errno {code})"
        )


@dataclass
class _Cmd:
    """One in-flight NBD command."""

    cookie: int
    cmd_type: int
    offset: int
    length: int
    flags: int = 0
    payload: bytes | None = None          # outbound data (WRITE)
    result: bytearray | None = None       # inbound data (READ)
    error: int = 0
    index: int = 0                        # position in the caller's batch

    def header(self) -> bytes:
        return p.S_REQUEST.pack(
            p.NBD_REQUEST_MAGIC, self.flags, self.cmd_type,
            self.cookie, self.offset, self.length,
        )


@dataclass
class ExportInfo:
    """What the server told us about the export during NBD_OPT_GO."""

    size: int
    transmission_flags: int
    min_block: int = 1
    preferred_block: int = 4096
    max_block: int = p.MAX_REQUEST_SIZE

    @property
    def read_only(self) -> bool:
        return bool(self.transmission_flags & p.NBD_FLAG_READ_ONLY)

    @property
    def can_multi_conn(self) -> bool:
        return bool(self.transmission_flags & p.NBD_FLAG_CAN_MULTI_CONN)

    @property
    def can_flush(self) -> bool:
        return bool(self.transmission_flags & p.NBD_FLAG_SEND_FLUSH)

    @property
    def can_trim(self) -> bool:
        return bool(self.transmission_flags & p.NBD_FLAG_SEND_TRIM)

    @property
    def can_zero(self) -> bool:
        return bool(self.transmission_flags & p.NBD_FLAG_SEND_WRITE_ZEROES)


class NbdClient:
    """A single NBD connection.

    One connection is one command stream. For parallelism open several clients;
    check :attr:`ExportInfo.can_multi_conn` before doing so against a writable
    export, as without that flag the server makes no cross-connection cache
    coherency or flush-ordering guarantees.
    """

    #: Outstanding commands allowed on the wire at once.
    DEFAULT_DEPTH = 8
    #: Cap on buffered outbound bytes, to bound memory during writes.
    _OUT_HIGH_WATER = 8 * 1024 * 1024

    def __init__(self, sock: socket.socket, export: str = "",
                 depth: int = DEFAULT_DEPTH):
        self._sock = sock
        self._export = export
        self._depth = max(1, depth)
        self._cookie = 0
        self._closed = False
        self._rbuf = bytearray()
        self.info: ExportInfo = self._handshake(export)

    # -- construction -------------------------------------------------------

    @classmethod
    def connect_unix(cls, path: str, export: str = "",
                     depth: int = DEFAULT_DEPTH,
                     timeout: float | None = 60.0) -> "NbdClient":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        return cls(sock, export, depth)

    @classmethod
    def connect_tcp(cls, host: str, port: int = 10809, export: str = "",
                    depth: int = DEFAULT_DEPTH,
                    timeout: float | None = 60.0) -> "NbdClient":
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock, export, depth)

    def __enter__(self) -> "NbdClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- handshake ----------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        """Blocking read of exactly n bytes; only used during handshake."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise NbdError(
                    f"server closed the connection after {len(buf)} of {n} bytes"
                )
            buf += chunk
        return bytes(buf)

    def _handshake(self, export: str) -> ExportInfo:
        magic, ihaveopt, hs_flags = p.S_HANDSHAKE.unpack(
            self._recv_exact(p.S_HANDSHAKE.size)
        )
        if magic != p.NBD_MAGIC:
            raise NbdError(f"not an NBD server (magic {magic:#x})")
        if ihaveopt != p.NBD_IHAVEOPT:
            raise NbdError("server does not support newstyle negotiation")
        if not hs_flags & p.NBD_FLAG_FIXED_NEWSTYLE:
            raise NbdError("server does not support fixed-newstyle negotiation")

        client_flags = p.NBD_FLAG_C_FIXED_NEWSTYLE
        if hs_flags & p.NBD_FLAG_NO_ZEROES:
            client_flags |= p.NBD_FLAG_C_NO_ZEROES
        self._sock.sendall(p.S_CLIENT_FLAGS.pack(client_flags))

        return self._opt_go(export)

    def _opt_go(self, export: str) -> ExportInfo:
        """NBD_OPT_GO: ask for the export and move to the transmission phase."""
        name = export.encode()
        # name length, name, number of info requests, the requests themselves
        payload = (
            len(name).to_bytes(4, "big") + name
            + (2).to_bytes(2, "big")
            + p.S_INFO_TYPE.pack(p.NBD_INFO_EXPORT)
            + p.S_INFO_TYPE.pack(p.NBD_INFO_BLOCK_SIZE)
        )
        self._sock.sendall(
            p.S_OPTION.pack(p.NBD_IHAVEOPT, p.NBD_OPT_GO, len(payload)) + payload
        )

        size: int | None = None
        tflags = 0
        min_block, pref_block, max_block = 1, 4096, p.MAX_REQUEST_SIZE

        while True:
            magic, option, rep_type, length = p.S_OPT_REPLY.unpack(
                self._recv_exact(p.S_OPT_REPLY.size)
            )
            if magic != p.NBD_OPT_REPLY_MAGIC:
                raise NbdError(f"bad option reply magic {magic:#x}")
            data = self._recv_exact(length) if length else b""

            if rep_type & p.NBD_REP_FLAG_ERROR:
                why = p.REP_ERR_NAMES.get(rep_type, f"error {rep_type:#x}")
                detail = data.decode("utf-8", "replace").strip()
                raise NbdError(
                    f"NBD_OPT_GO for export {export!r} rejected: {why}"
                    + (f" ({detail})" if detail else "")
                )

            if rep_type == p.NBD_REP_INFO:
                if len(data) < p.S_INFO_TYPE.size:
                    continue
                (info_type,) = p.S_INFO_TYPE.unpack(data[: p.S_INFO_TYPE.size])
                body = data[p.S_INFO_TYPE.size:]
                if info_type == p.NBD_INFO_EXPORT:
                    size, tflags = p.S_INFO_EXPORT.unpack(body)
                elif info_type == p.NBD_INFO_BLOCK_SIZE:
                    min_block, pref_block, max_block = p.S_INFO_BLOCK_SIZE.unpack(body)
                # Other info types (name, description) are informational.
            elif rep_type == p.NBD_REP_ACK:
                break
            # NBD_REP_SERVER only arrives for NBD_OPT_LIST; ignore it here.

        if size is None:
            raise NbdError("server acknowledged NBD_OPT_GO without an export size")

        return ExportInfo(
            size=size,
            transmission_flags=tflags,
            min_block=min_block or 1,
            preferred_block=pref_block or 4096,
            max_block=min(max_block or p.MAX_REQUEST_SIZE, p.MAX_REQUEST_SIZE),
        )

    # -- transmission engine ------------------------------------------------

    def _next_cookie(self) -> int:
        self._cookie += 1
        return self._cookie

    def _run(self, cmds: Sequence[_Cmd]) -> None:
        """Drive a batch of commands to completion, keeping several in flight.

        Both directions are polled. Sending only headers (as READ does) could
        never block, but WRITE pushes large payloads while the server is pushing
        replies back; if we blocked on sendall() while the server blocked
        writing replies we would deadlock. Reacting to whichever direction is
        ready avoids that without bounding throughput.
        """
        if not cmds:
            return

        sock = self._sock
        prev_timeout = sock.gettimeout()
        sock.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(sock, selectors.EVENT_READ | selectors.EVENT_WRITE)

        pending = deque(cmds)
        inflight: dict[int, _Cmd] = {}
        out = bytearray()
        out_pos = 0
        # Reply parser state: either awaiting a header, or draining a payload.
        awaiting: _Cmd | None = None
        awaiting_need = 0

        try:
            # `awaiting` must be part of this condition: a command leaves
            # `inflight` when its reply *header* is parsed, but its payload is
            # still arriving. Without it, the last read of a batch can return
            # mid-payload with a truncated buffer, and the leftover bytes are
            # then misparsed as the next reply's header.
            while (pending or inflight or awaiting is not None
                   or out_pos < len(out)):
                # Top up the outbound buffer within both the depth and the
                # memory ceiling.
                while (pending and len(inflight) < self._depth
                       and len(out) - out_pos < self._OUT_HIGH_WATER):
                    cmd = pending.popleft()
                    out += cmd.header()
                    if cmd.payload is not None:
                        out += cmd.payload
                    inflight[cmd.cookie] = cmd

                want = (selectors.EVENT_READ
                        if (inflight or awaiting is not None) else 0)
                if out_pos < len(out):
                    want |= selectors.EVENT_WRITE
                if not want:
                    break
                sel.modify(sock, want)

                for _key, events in sel.select(timeout=prev_timeout):
                    if events & selectors.EVENT_WRITE and out_pos < len(out):
                        try:
                            sent = sock.send(memoryview(out)[out_pos:])
                        except (BlockingIOError, InterruptedError):
                            sent = 0
                        out_pos += sent
                        if out_pos == len(out):
                            out.clear()
                            out_pos = 0
                        elif out_pos > self._OUT_HIGH_WATER:
                            del out[:out_pos]
                            out_pos = 0

                    if events & selectors.EVENT_READ:
                        try:
                            chunk = sock.recv(1 << 20)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError as exc:
                            if exc.errno == errno.EAGAIN:
                                continue
                            raise
                        if not chunk:
                            raise NbdError(
                                "server closed the connection with "
                                f"{len(inflight)} command(s) outstanding"
                            )
                        self._rbuf += chunk

                        # Parse as many complete replies as the buffer holds.
                        while True:
                            if awaiting is not None:
                                take = min(awaiting_need, len(self._rbuf))
                                if take:
                                    assert awaiting.result is not None
                                    awaiting.result += self._rbuf[:take]
                                    del self._rbuf[:take]
                                    awaiting_need -= take
                                if awaiting_need:
                                    break
                                awaiting = None
                                continue

                            if len(self._rbuf) < p.S_SIMPLE_REPLY.size:
                                break
                            magic, err, cookie = p.S_SIMPLE_REPLY.unpack(
                                self._rbuf[: p.S_SIMPLE_REPLY.size]
                            )
                            if magic != p.NBD_SIMPLE_REPLY_MAGIC:
                                raise NbdError(f"bad reply magic {magic:#x}")
                            del self._rbuf[: p.S_SIMPLE_REPLY.size]

                            cmd = inflight.pop(cookie, None)
                            if cmd is None:
                                raise NbdError(
                                    f"reply for unknown cookie {cookie}"
                                )
                            cmd.error = err
                            # On error the server sends no read payload.
                            if cmd.cmd_type == p.NBD_CMD_READ and not err:
                                cmd.result = bytearray()
                                awaiting = cmd
                                awaiting_need = cmd.length
        finally:
            sel.close()
            sock.setblocking(True)
            sock.settimeout(prev_timeout)

        for cmd in cmds:
            if cmd.error:
                raise NbdServerError(
                    cmd.cmd_type, cmd.offset, cmd.length, cmd.error
                )
            # A short payload means we lost frame sync with the server. Fail
            # loudly: silently returning truncated data would write a corrupt
            # block into the repository and we would not find out until restore.
            if cmd.cmd_type == p.NBD_CMD_READ:
                got = len(cmd.result) if cmd.result is not None else 0
                if got != cmd.length:
                    raise NbdError(
                        f"short read at offset {cmd.offset}: expected "
                        f"{cmd.length} bytes, assembled {got}"
                    )

    def _check_range(self, offset: int, length: int, writing: bool) -> None:
        if self._closed:
            raise NbdError("operation on a closed NBD connection")
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        if offset + length > self.info.size:
            raise ValueError(
                f"range {offset}+{length} extends past the export size "
                f"{self.info.size}"
            )
        if writing and self.info.read_only:
            raise NbdError("export is read-only")

    # -- public operations --------------------------------------------------

    @property
    def size(self) -> int:
        return self.info.size

    def pread(self, offset: int, length: int) -> bytes:
        """Read `length` bytes at `offset`, splitting to honour the server cap."""
        self._check_range(offset, length, writing=False)
        if length == 0:
            return b""
        cmds = [
            _Cmd(self._next_cookie(), p.NBD_CMD_READ, off, size, index=i)
            for i, (off, size) in enumerate(_split(offset, length, self.info.max_block))
        ]
        self._run(cmds)
        return b"".join(bytes(c.result or b"") for c in cmds)

    def pread_into(self, offset: int, buf: bytearray) -> None:
        """Read len(buf) bytes at `offset` into an existing buffer."""
        data = self.pread(offset, len(buf))
        buf[: len(data)] = data

    def pread_batch(self, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
        """Read several disjoint ranges in one pipelined round.

        This is the shape CBT hands us: a list of changed extents. Issuing them
        as one batch keeps the link busy instead of paying a round-trip per
        extent.
        """
        ranges = list(ranges)
        cmds: list[_Cmd] = []
        owners: list[list[_Cmd]] = []
        for i, (off, size) in enumerate(ranges):
            self._check_range(off, size, writing=False)
            mine = [
                _Cmd(self._next_cookie(), p.NBD_CMD_READ, o, s, index=i)
                for o, s in _split(off, size, self.info.max_block)
            ]
            owners.append(mine)
            cmds.extend(mine)
        self._run(cmds)
        return [b"".join(bytes(c.result or b"") for c in mine) for mine in owners]

    def pwrite(self, offset: int, data: bytes, fua: bool = False) -> None:
        """Write `data` at `offset`."""
        self._check_range(offset, len(data), writing=True)
        if not data:
            return
        flags = p.NBD_CMD_FLAG_FUA if (fua and self.info.transmission_flags
                                       & p.NBD_FLAG_SEND_FUA) else 0
        cmds = []
        pos = 0
        for off, size in _split(offset, len(data), self.info.max_block):
            cmds.append(_Cmd(
                self._next_cookie(), p.NBD_CMD_WRITE, off, size,
                flags=flags, payload=data[pos:pos + size],
            ))
            pos += size
        self._run(cmds)

    def zero(self, offset: int, length: int, fua: bool = False) -> None:
        """Punch zeroes without transferring them, falling back to a write."""
        self._check_range(offset, length, writing=True)
        if length == 0:
            return
        if not self.info.can_zero:
            self.pwrite(offset, b"\0" * length, fua=fua)
            return
        flags = p.NBD_CMD_FLAG_FUA if (fua and self.info.transmission_flags
                                       & p.NBD_FLAG_SEND_FUA) else 0
        # WRITE_ZEROES carries no payload, so the server's max block size does
        # not apply to it; a 32-bit length is the only limit.
        cmds = [
            _Cmd(self._next_cookie(), p.NBD_CMD_WRITE_ZEROES, off, size, flags=flags)
            for off, size in _split(offset, length, 0xFFFFFFFF)
        ]
        self._run(cmds)

    def trim(self, offset: int, length: int) -> None:
        self._check_range(offset, length, writing=True)
        if length == 0 or not self.info.can_trim:
            return
        cmds = [
            _Cmd(self._next_cookie(), p.NBD_CMD_TRIM, off, size)
            for off, size in _split(offset, length, 0xFFFFFFFF)
        ]
        self._run(cmds)

    def flush(self) -> None:
        """Force written data to stable storage before we call a restore done."""
        if self._closed or not self.info.can_flush:
            return
        self._run([_Cmd(self._next_cookie(), p.NBD_CMD_FLUSH, 0, 0)])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # A clean NBD_CMD_DISC lets nbdkit tear the VDDK handle down
            # properly; the server sends no reply to it.
            self._sock.sendall(
                p.S_REQUEST.pack(p.NBD_REQUEST_MAGIC, 0, p.NBD_CMD_DISC,
                                 self._next_cookie(), 0, 0)
            )
            self._sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            try:
                self._sock.close()
            except OSError:
                pass


def _split(offset: int, length: int, max_size: int) -> list[tuple[int, int]]:
    """Break a range into server-acceptable pieces."""
    max_size = max(1, min(max_size or p.MAX_REQUEST_SIZE, p.MAX_REQUEST_SIZE))
    out = []
    pos, end = offset, offset + length
    while pos < end:
        size = min(max_size, end - pos)
        out.append((pos, size))
        pos += size
    return out
