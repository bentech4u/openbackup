"""A minimal in-process NBD server used to force protocol edge cases.

Real servers (nbdkit, qemu-nbd) decide for themselves how to packetise
replies, which makes framing races impossible to reproduce on demand. This
server lets a test dictate the packetisation exactly.
"""

from __future__ import annotations

import socket
import threading
import time

from openbackup.nbd import protocol as p


class FakeNbdServer:
    """Serves one export from an in-memory bytearray over a unix socket.

    :param split_read_replies: send the reply header and its payload as two
        separate writes with a pause between them. This reproduces the case
        where a client sees a completed reply header while the payload is
        still in flight.
    :param short_read_by: truncate read payloads by this many bytes and then
        drop the connection, to check the client refuses to return short data.
    """

    def __init__(self, path: str, size: int = 8 * 1024 * 1024,
                 split_read_replies: bool = False, split_delay: float = 0.03,
                 short_read_by: int = 0,
                 transmission_flags: int = p.NBD_FLAG_HAS_FLAGS | p.NBD_FLAG_SEND_FLUSH):
        self.path = path
        self.data = bytearray(size)
        self.split_read_replies = split_read_replies
        self.split_delay = split_delay
        #: Send this many fewer payload bytes than requested, then hang up.
        self.short_read_by = short_read_by
        self.transmission_flags = transmission_flags
        self.error = None

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(4)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- plumbing -----------------------------------------------------------

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    return
                threading.Thread(target=self._session, args=(conn,),
                                 daemon=True).start()
        except Exception as exc:  # pragma: no cover - surfaced via self.error
            self.error = exc

    @staticmethod
    def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _session(self, conn: socket.socket) -> None:
        try:
            self._handshake(conn)
            self._transmission(conn)
        except Exception as exc:  # pragma: no cover
            self.error = exc
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn: socket.socket) -> None:
        conn.sendall(p.S_HANDSHAKE.pack(
            p.NBD_MAGIC, p.NBD_IHAVEOPT, p.NBD_FLAG_FIXED_NEWSTYLE
        ))
        self._recv_exact(conn, p.S_CLIENT_FLAGS.size)

        while True:
            hdr = self._recv_exact(conn, p.S_OPTION.size)
            if hdr is None:
                return
            _magic, option, length = p.S_OPTION.unpack(hdr)
            if length:
                self._recv_exact(conn, length)

            if option in (p.NBD_OPT_GO, p.NBD_OPT_INFO):
                info = (p.S_INFO_TYPE.pack(p.NBD_INFO_EXPORT)
                        + p.S_INFO_EXPORT.pack(len(self.data),
                                               self.transmission_flags))
                conn.sendall(p.S_OPT_REPLY.pack(
                    p.NBD_OPT_REPLY_MAGIC, option, p.NBD_REP_INFO, len(info)
                ) + info)
                conn.sendall(p.S_OPT_REPLY.pack(
                    p.NBD_OPT_REPLY_MAGIC, option, p.NBD_REP_ACK, 0
                ))
                if option == p.NBD_OPT_GO:
                    return
            else:
                conn.sendall(p.S_OPT_REPLY.pack(
                    p.NBD_OPT_REPLY_MAGIC, option, p.NBD_REP_ERR_UNSUP, 0
                ))

    def _transmission(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            hdr = self._recv_exact(conn, p.S_REQUEST.size)
            if hdr is None:
                return
            magic, _flags, cmd, cookie, offset, length = p.S_REQUEST.unpack(hdr)
            if magic != p.NBD_REQUEST_MAGIC:
                raise AssertionError(f"client sent bad request magic {magic:#x}")

            if cmd == p.NBD_CMD_DISC:
                return

            if cmd == p.NBD_CMD_WRITE:
                payload = self._recv_exact(conn, length)
                if payload is None:
                    return
                self.data[offset:offset + length] = payload
                conn.sendall(p.S_SIMPLE_REPLY.pack(
                    p.NBD_SIMPLE_REPLY_MAGIC, 0, cookie))

            elif cmd == p.NBD_CMD_READ:
                reply = p.S_SIMPLE_REPLY.pack(
                    p.NBD_SIMPLE_REPLY_MAGIC, 0, cookie)
                payload = bytes(self.data[offset:offset + length])
                if self.short_read_by:
                    conn.sendall(reply + payload[:-self.short_read_by])
                    return
                if self.split_read_replies:
                    # Header first, then a pause, then the data. A client that
                    # treats "header parsed" as "command complete" truncates here.
                    conn.sendall(reply)
                    time.sleep(self.split_delay)
                    conn.sendall(payload)
                else:
                    conn.sendall(reply + payload)

            elif cmd == p.NBD_CMD_FLUSH:
                conn.sendall(p.S_SIMPLE_REPLY.pack(
                    p.NBD_SIMPLE_REPLY_MAGIC, 0, cookie))

            else:
                # ENOTSUP
                conn.sendall(p.S_SIMPLE_REPLY.pack(
                    p.NBD_SIMPLE_REPLY_MAGIC, 95, cookie))

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)
