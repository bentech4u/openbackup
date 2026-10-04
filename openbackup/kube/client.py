"""A small Kubernetes/OpenShift API client.

Only what backup and restore need: discovery, paginated lists, get, create,
server-side apply, delete, and a few subresources. The API server is
trusted through a pinned CA bundle (from the cluster's kubeconfig or
confirmed by an administrator), never the system trust store.
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

FIELD_MANAGER = "openbackup"


class KubeError(Exception):
    def __init__(self, message: str, status: int = 0, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class ApiResource:
    group: str  # "" for core
    version: str
    plural: str
    kind: str
    namespaced: bool
    verbs: frozenset[str]

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    def path(self, namespace: str | None = None, name: str | None = None) -> str:
        base = f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"
        if self.namespaced and namespace:
            base += f"/namespaces/{quote(namespace, safe='')}"
        base += f"/{self.plural}"
        if name:
            base += f"/{quote(name, safe='')}"
        return base


class KubeClient:
    def __init__(self, api_url: str, token: str, ca_pem: str, timeout: float = 60,
                 transport: httpx.BaseTransport | None = None):
        self.api_url = api_url.rstrip("/")
        # No token means an anonymous request (e.g. OAuth discovery).
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if transport is None:
            ctx = ssl.create_default_context(cadata=ca_pem)
            self._http = httpx.Client(base_url=self.api_url, verify=ctx, timeout=timeout,
                                      headers=headers)
        else:
            self._http = httpx.Client(base_url=self.api_url, transport=transport,
                                      timeout=timeout, headers=headers)
        self._resources: list[ApiResource] | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KubeClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- http

    def _request(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.ConnectError as e:
            msg = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify failed" in msg:
                raise KubeError("The API server certificate is not signed by the pinned CA "
                                "(or its name does not match the URL)") from None
            raise KubeError(f"Cannot reach {self.api_url}: {msg}") from None
        except httpx.HTTPError as e:
            raise KubeError(f"Request to {self.api_url} failed: {e}") from None
        if r.status_code >= 400:
            try:
                body = r.json()
                msg, reason = body.get("message", r.text), body.get("reason", "")
            except ValueError:
                msg, reason = r.text[:300], ""
            if r.status_code == 401:
                msg = "The token was rejected (expired or revoked)"
            raise KubeError(msg, r.status_code, reason)
        if not r.content:
            return None
        return r.json()

    def get(self, path: str, **params) -> Any:
        return self._request("GET", path, params=params or None)

    def get_text(self, path: str, **params) -> str:
        """A plain-text endpoint, such as pod logs."""
        try:
            r = self._http.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise KubeError(f"Request to {self.api_url} failed: {e}") from None
        if r.status_code >= 400:
            raise KubeError(r.text[:300], r.status_code)
        return r.text

    def list(self, path: str, label_selector: str | None = None) -> list[dict]:
        """All items of a collection, following pagination."""
        items: list[dict] = []
        params: dict[str, Any] = {"limit": 500}
        if label_selector:
            params["labelSelector"] = label_selector
        while True:
            page = self._request("GET", path, params=params)
            items += page.get("items") or []
            cont = (page.get("metadata") or {}).get("continue")
            if not cont:
                return items
            params["continue"] = cont

    def create(self, path: str, obj: dict) -> dict:
        return self._request("POST", path, json=obj)

    def apply(self, path: str, obj: dict, force: bool = True) -> dict:
        """Server-side apply. ``path`` names the object itself."""
        return self._request(
            "PATCH", path, content=json.dumps(obj),
            headers={"Content-Type": "application/apply-patch+yaml"},
            params={"fieldManager": FIELD_MANAGER, "force": "true" if force else "false"})

    def delete(self, path: str) -> None:
        try:
            self._request("DELETE", path, params={"propagationPolicy": "Background"})
        except KubeError as e:
            if e.status != 404:
                raise

    def put(self, path: str, obj: dict | None = None) -> Any:
        return self._request("PUT", path, json=obj or {})

    # ------------------------------------------------------------ discovery

    def version(self) -> dict:
        return self.get("/version")

    def resources(self, refresh: bool = False) -> list[ApiResource]:
        """Every served resource type, at each group's preferred version."""
        if self._resources is not None and not refresh:
            return self._resources
        out: list[ApiResource] = []
        core = self.get("/api/v1")
        out += self._parse_resources("", "v1", core)
        groups = self.get("/apis")
        for g in groups.get("groups", []):
            pv = (g.get("preferredVersion") or {}).get("groupVersion")
            if not pv:
                continue
            group, _, version = pv.partition("/")
            try:
                out += self._parse_resources(group, version, self.get(f"/apis/{pv}"))
            except KubeError:
                continue  # an aggregated API that is down must not stop discovery
        self._resources = out
        return out

    @staticmethod
    def _parse_resources(group: str, version: str, doc: dict) -> list[ApiResource]:
        out = []
        for r in doc.get("resources", []):
            if "/" in r["name"]:
                continue  # subresource
            out.append(ApiResource(group, version, r["name"], r["kind"], r["namespaced"],
                                   frozenset(r.get("verbs", []))))
        return out

    def resource(self, api_version: str, kind: str) -> ApiResource | None:
        group, _, version = api_version.rpartition("/")
        for r in self.resources():
            if r.group == group and r.kind == kind and r.version == version:
                return r
        for r in self.resources():  # same kind served at another version
            if r.group == group and r.kind == kind:
                return r
        return None

    def has_group(self, group: str) -> bool:
        return any(r.group == group for r in self.resources())

    # --------------------------------------------------------------- kubevirt

    def freeze_vm(self, namespace: str, name: str, timeout: str = "5m") -> None:
        """Freeze a running VM's guest filesystems via the QEMU guest agent. It
        thaws by itself after ``timeout`` even if unfreeze never arrives."""
        self.put(f"/apis/subresources.kubevirt.io/v1/namespaces/{namespace}/"
                 f"virtualmachineinstances/{name}/freeze", {"unfreezeTimeout": timeout})

    def unfreeze_vm(self, namespace: str, name: str) -> None:
        self.put(f"/apis/subresources.kubevirt.io/v1/namespaces/{namespace}/"
                 f"virtualmachineinstances/{name}/unfreeze")

    # -------------------------------------------------------------- identity

    def whoami(self) -> str:
        try:
            r = self.create("/apis/authentication.k8s.io/v1/selfsubjectreviews",
                            {"apiVersion": "authentication.k8s.io/v1",
                             "kind": "SelfSubjectReview"})
            return r["status"]["userInfo"]["username"]
        except KubeError:
            return ""

    def can(self, verb: str, resource: str, group: str = "", namespace: str = "",
            subresource: str = "") -> bool:
        attrs = {"verb": verb, "resource": resource, "group": group}
        if namespace:
            attrs["namespace"] = namespace
        if subresource:
            attrs["subresource"] = subresource
        r = self.create("/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                        {"apiVersion": "authorization.k8s.io/v1",
                         "kind": "SelfSubjectAccessReview",
                         "spec": {"resourceAttributes": attrs}})
        return bool(r.get("status", {}).get("allowed"))


# ------------------------------------------------------------------ CA pinning


@dataclass
class PeerCert:
    pem: str
    subject: str
    issuer: str
    sha256: str
    is_ca: bool


def fetch_chain(api_url: str, timeout: int = 15) -> list[PeerCert]:
    """The certificate chain the API server presents, for an administrator
    to confirm. Python's ssl module cannot return the unverified chain, so
    openssl does the handshake."""
    u = urlsplit(api_url)
    if u.scheme != "https" or not u.hostname:
        raise KubeError("The API URL must be https://host:port")
    host, port = u.hostname, u.port or 443
    try:
        proc = subprocess.run(
            ["openssl", "s_client", "-connect", f"{host}:{port}", "-servername", host,
             "-showcerts"], input="", capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise KubeError(f"Timed out connecting to {host}:{port}") from None
    pems = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                      proc.stdout, re.S)
    if not pems:
        raise KubeError(f"No certificate received from {host}:{port}: "
                        f"{proc.stderr.strip()[:300]}")
    return [describe_cert(p) for p in pems]


def describe_cert(pem: str) -> PeerCert:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    c = x509.load_pem_x509_certificate(pem.encode())
    try:
        is_ca = c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        is_ca = False
    fp = c.fingerprint(hashes.SHA256())
    return PeerCert(pem=pem.strip() + "\n", subject=c.subject.rfc4514_string(),
                    issuer=c.issuer.rfc4514_string(),
                    sha256=":".join(f"{b:02X}" for b in fp), is_ca=is_ca)


def validate_ca_bundle(ca_pem: str) -> list[PeerCert]:
    pems = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", ca_pem, re.S)
    if not pems:
        raise KubeError("The CA bundle contains no certificates")
    try:
        certs = [describe_cert(p) for p in pems]
        ssl.create_default_context(cadata="\n".join(c.pem for c in certs))
    except (ValueError, ssl.SSLError) as e:
        raise KubeError(f"Invalid CA bundle: {e}") from None
    return certs
