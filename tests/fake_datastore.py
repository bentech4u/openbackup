"""A stand-in for the vSphere /folder datastore endpoint.

Lets tests drive the Range-handling paths, including servers that misbehave,
without a vCenter.
"""

from __future__ import annotations

import http.server
import re
import threading


class FakeDatastore:
    """Serves one file, with switches for non-conforming behaviour.

    :param ignore_range: answer 200 with the whole file instead of 206.
    :param wrong_range: serve a range shifted from the one requested.
    :param short_body: return fewer bytes than the range covers.
    """

    def __init__(self, data: bytes, *, ignore_range: bool = False,
                 wrong_range: bool = False, short_body: bool = False):
        self.data = bytearray(data)
        self.ignore_range = ignore_range
        self.wrong_range = wrong_range
        self.short_body = short_body
        self.uploads: list[bytes] = []
        self.requests: list[str] = []

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence the default stderr spew
                pass

            def do_HEAD(self):
                outer.requests.append(f"HEAD {self.path}")
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                rng = self.headers.get("Range")
                outer.requests.append(f"GET {self.path} {rng}")
                if not rng or outer.ignore_range:
                    body = bytes(outer.data)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                m = re.match(r"bytes=(\d+)-(\d+)", rng)
                start, end = int(m.group(1)), int(m.group(2))
                served_start, served_end = start, end
                if outer.wrong_range:
                    served_start, served_end = start + 512, end + 512
                body = bytes(outer.data[served_start:served_end + 1])
                if outer.short_body:
                    body = body[:-16]

                self.send_response(206)
                self.send_header(
                    "Content-Range",
                    f"bytes {served_start}-{served_end}/{len(outer.data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.uploads.append(body)
                outer.data = bytearray(body)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/folder/vm/vm-flat.vmdk"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
