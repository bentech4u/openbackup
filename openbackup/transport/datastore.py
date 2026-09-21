"""BlockDevice over the vSphere datastore HTTPS endpoint.

Every ESXi host and vCenter serves datastore files at::

    https://<host>/folder/<path>?dcPath=<datacenter>&dsName=<datastore>

It honours HTTP Range requests, which is the property that matters: it gives
us offset-addressed reads, so CBT incrementals work over it exactly as they do
over VDDK. That makes it a real transport rather than a fallback, and it needs
no entitlement-gated library.

Two limitations are inherent, not bugs:

1. It serves raw files, so it can only read a disk whose current contents live
   in a single flat file. Once we take our own snapshot the base ``-flat.vmdk``
   is frozen and complete -- but only if the VM had no snapshots of its own.
   With a pre-existing snapshot the live data is in a delta in VMware's sparse
   format, which this transport does not parse. The vSphere layer checks for
   that and refuses rather than backing up stale data.
2. Writes are whole-file. See :attr:`supports_random_write`.

Throughput goes through the host's management agents rather than the data
path, so expect well below what HotAdd or SAN transport would give.
"""

from __future__ import annotations

import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import BlockDevice, TransportError, UnsupportedOperation


class DatastoreTransport:
    """Opens disks on one vCenter or ESXi host.

    :param cookie: the ``vmware_soap_session`` cookie from an authenticated
        pyVmomi connection. Preferred over `auth`: it reuses the session we
        already hold instead of putting credentials on every request.
    :param auth: ``(user, password)`` for HTTP basic auth, which ESXi accepts
        directly.
    :param verify: TLS verification. Disabling it is normal against a lab host
        with a self-signed certificate, but it makes the connection
        interceptable, so it must be chosen explicitly.
    """

    def __init__(self, host: str, *, cookie: str | None = None,
                 auth: tuple[str, str] | None = None, verify: bool | str = True,
                 timeout: float = 120.0, max_workers: int = 4):
        if not cookie and not auth:
            raise ValueError("a session cookie or basic-auth credentials are required")
        self.host = host
        self.cookie = cookie
        self.auth = auth
        self.verify = verify
        self.timeout = timeout
        self.max_workers = max(1, max_workers)
        self._local = threading.local()

        if verify is False:
            # Without this urllib3 prints a warning per request, which buries
            # real output during a backup of a few hundred thousand blocks.
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def session(self) -> requests.Session:
        """One session per thread: connection reuse without sharing state."""
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.verify = self.verify
            if self.auth:
                sess.auth = self.auth
            if self.cookie:
                sess.headers["Cookie"] = self.cookie
            retry = Retry(
                total=4, backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD", "PUT"]),
            )
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=self.max_workers)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            self._local.session = sess
        return sess

    def url_for(self, datacenter: str, datastore: str, path: str) -> str:
        quoted = urllib.parse.quote(path.lstrip("/"))
        query = urllib.parse.urlencode({"dcPath": datacenter, "dsName": datastore})
        return f"https://{self.host}/folder/{quoted}?{query}"

    def open(self, *, datacenter: str, datastore: str, path: str,
             read_only: bool = True) -> "DatastoreBlockDevice":
        return DatastoreBlockDevice(
            self, self.url_for(datacenter, datastore, path), read_only=read_only
        )

    def close(self) -> None:
        sess = getattr(self._local, "session", None)
        if sess is not None:
            sess.close()
            self._local.session = None


class DatastoreBlockDevice(BlockDevice):
    """One flat VMDK, read over HTTP Range requests."""

    #: The endpoint replaces whole files; it does not apply a Content-Range on
    #: PUT. Restores therefore rewrite the disk from offset zero via
    #: :meth:`write_stream` rather than writing only the blocks that differ.
    supports_random_write = False

    def __init__(self, transport: DatastoreTransport, url: str,
                 read_only: bool = True):
        self._t = transport
        self.url = url
        self.read_only = read_only
        self._closed = False
        self.size = self._fetch_size()

    # -- metadata -----------------------------------------------------------

    def _fetch_size(self) -> int:
        resp = self._t.session().head(
            self.url, timeout=self._t.timeout, allow_redirects=True)
        if resp.status_code == 404:
            raise TransportError(f"datastore file not found: {self.url}")
        if resp.status_code in (401, 403):
            raise TransportError(
                f"not authorised to read {self.url} (status {resp.status_code}); "
                "the account needs Datastore > Browse datastore and "
                "Low level file operations"
            )
        if resp.status_code >= 400:
            raise TransportError(
                f"HEAD {self.url} failed with status {resp.status_code}")
        length = resp.headers.get("Content-Length")
        if length is None:
            raise TransportError(f"datastore did not report a size for {self.url}")
        return int(length)

    # -- reading ------------------------------------------------------------

    def pread(self, offset: int, length: int) -> bytes:
        self._check_open()
        self._check_range(offset, length)
        if length == 0:
            return b""
        return self._range_get(self._t.session(), offset, length)

    def _range_get(self, sess: requests.Session, offset: int, length: int) -> bytes:
        last = offset + length - 1
        resp = sess.get(
            self.url,
            headers={"Range": f"bytes={offset}-{last}"},
            timeout=self._t.timeout,
            stream=True,
        )
        try:
            # A server that does not honour Range answers 200 with the entire
            # file. Silently accepting that would both blow up memory and put
            # the wrong bytes at this offset, so treat it as fatal.
            if resp.status_code == 200:
                raise TransportError(
                    f"datastore ignored the Range header for {self.url} "
                    f"(returned the whole file); refusing to guess at offsets"
                )
            if resp.status_code != 206:
                raise TransportError(
                    f"range read {offset}+{length} failed with status "
                    f"{resp.status_code}"
                )
            content_range = resp.headers.get("Content-Range", "")
            if content_range:
                self._verify_content_range(content_range, offset, last)
            data = resp.content
        finally:
            resp.close()

        if len(data) != length:
            raise TransportError(
                f"range read {offset}+{length} returned {len(data)} bytes"
            )
        return data

    @staticmethod
    def _verify_content_range(header: str, offset: int, last: int) -> None:
        """Confirm the server sent the range we asked for, not a different one."""
        try:
            spec = header.split()[1].split("/")[0]
            start_s, end_s = spec.split("-")
            start, end = int(start_s), int(end_s)
        except (IndexError, ValueError) as exc:
            raise TransportError(f"unparsable Content-Range {header!r}") from exc
        if start != offset or end != last:
            raise TransportError(
                f"datastore returned bytes {start}-{end} but we asked for "
                f"{offset}-{last}"
            )

    def pread_batch(self, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
        """Fetch several extents at once.

        Each range is its own request here, since the endpoint does not do
        multipart byte ranges. Overlapping them across a few connections still
        beats a serial loop by roughly the concurrency factor, because this
        path is latency-bound rather than bandwidth-bound.
        """
        self._check_open()
        ranges = list(ranges)
        for off, length in ranges:
            self._check_range(off, length)
        if not ranges:
            return []
        if len(ranges) == 1 or self._t.max_workers == 1:
            return [self.pread(off, length) for off, length in ranges]

        def fetch(item):
            off, length = item
            if length == 0:
                return b""
            return self._range_get(self._t.session(), off, length)

        with ThreadPoolExecutor(max_workers=self._t.max_workers) as pool:
            # executor.map preserves input order, which the caller relies on to
            # line results up with the extents it asked for.
            return list(pool.map(fetch, ranges))

    # -- writing ------------------------------------------------------------

    def pwrite(self, offset: int, data: bytes) -> None:
        raise UnsupportedOperation(
            "the datastore endpoint replaces whole files; use write_stream() "
            "to restore this disk, or the VDDK transport for offset writes"
        )

    def write_stream(self, chunks: Iterator[bytes], total: int) -> None:
        """Replace the whole file, streaming the body as it is generated.

        Content-Length is set explicitly so the body streams from the iterator
        instead of being buffered or sent chunked: a disk image will not fit in
        memory, and the endpoint rejects chunked transfer encoding.
        """
        self._check_open()
        if self.read_only:
            raise UnsupportedOperation("disk is open read-only")

        sent = 0

        def body():
            nonlocal sent
            for chunk in chunks:
                sent += len(chunk)
                if sent > total:
                    raise TransportError(
                        f"stream produced more than the declared {total} bytes"
                    )
                yield chunk
            # This has to happen here rather than after the PUT returns. We
            # promised `total` bytes in Content-Length, so a server that has
            # not received them all is still blocked reading the body: the
            # request would never complete and a restore would hang instead of
            # failing. Raising inside the generator aborts the connection.
            if sent != total:
                raise TransportError(
                    f"stream supplied {sent} bytes, declared {total}"
                )

        resp = self._t.session().put(
            self.url,
            data=body(),
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(total)},
            timeout=self._t.timeout,
        )
        if resp.status_code >= 400:
            raise TransportError(
                f"PUT {self.url} failed with status {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        if sent != total:  # pragma: no cover - the generator raises first
            raise TransportError(
                f"stream supplied {sent} bytes, declared {total}"
            )

    # -- lifecycle ----------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise TransportError("operation on a closed disk")

    def close(self) -> None:
        self._closed = True
