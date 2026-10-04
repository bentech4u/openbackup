"""An in-memory Kubernetes API behind httpx.MockTransport: discovery, lists
with pagination, get/create/apply/delete, access reviews and subresource
calls. Enough for OpenBackup's cluster code; not a conformant API server."""

from __future__ import annotations

import copy
import itertools
import json
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

# (group, version, plural, kind, namespaced)
RESOURCES = [
    ("", "v1", "namespaces", "Namespace", False),
    ("", "v1", "configmaps", "ConfigMap", True),
    ("", "v1", "secrets", "Secret", True),
    ("", "v1", "services", "Service", True),
    ("", "v1", "serviceaccounts", "ServiceAccount", True),
    ("", "v1", "persistentvolumeclaims", "PersistentVolumeClaim", True),
    ("", "v1", "persistentvolumes", "PersistentVolume", False),
    ("", "v1", "pods", "Pod", True),
    ("", "v1", "events", "Event", True),
    ("", "v1", "endpoints", "Endpoints", True),
    ("apps", "v1", "deployments", "Deployment", True),
    ("apps", "v1", "statefulsets", "StatefulSet", True),
    ("apps", "v1", "replicasets", "ReplicaSet", True),
    ("apps", "v1", "controllerrevisions", "ControllerRevision", True),
    ("batch", "v1", "jobs", "Job", True),
    ("batch", "v1", "cronjobs", "CronJob", True),
    ("discovery.k8s.io", "v1", "endpointslices", "EndpointSlice", True),
    ("rbac.authorization.k8s.io", "v1", "roles", "Role", True),
    ("rbac.authorization.k8s.io", "v1", "rolebindings", "RoleBinding", True),
    ("route.openshift.io", "v1", "routes", "Route", True),
    ("storage.k8s.io", "v1", "storageclasses", "StorageClass", False),
    ("image.openshift.io", "v1", "imagestreams", "ImageStream", True),
    ("rbac.authorization.k8s.io", "v1", "clusterroles", "ClusterRole", False),
    ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "ClusterRoleBinding", False),
    ("oauth.openshift.io", "v1", "oauthaccesstokens", "OAuthAccessToken", False),
    ("config.openshift.io", "v1", "infrastructures", "Infrastructure", False),
    ("kubevirt.io", "v1", "virtualmachines", "VirtualMachine", True),
    ("kubevirt.io", "v1", "virtualmachineinstances", "VirtualMachineInstance", True),
]


class FakeKube:
    def __init__(self, resources=RESOURCES, page_size: int = 2):
        self.types = {(g, p): (g, v, p, k, ns) for g, v, p, k, ns in resources}
        self.by_kind = {(g, k): (g, v, p, k, ns) for g, v, p, k, ns in resources}
        self.objects: dict[tuple[str, str, str, str], dict] = {}
        self.page_size = page_size
        self.denied: set[tuple[str, str]] = set()  # (verb, resource)
        self.calls: list[tuple[str, str]] = []
        self.username = "system:serviceaccount:openbackup:backup"
        self.on_create = []  # callbacks(obj) after a create/apply
        self._rv = itertools.count(1)
        # When set, only these bearer tokens are accepted; each maps to a user
        # and that user's denied (verb, resource) pairs.
        self.tokens: dict[str, tuple[str, set]] | None = None
        self.oauth_url = "https://oauth-openshift.apps.test/oauth/authorize"
        self.on_create.append(self._fill_sa_token)

    def _fill_sa_token(self, obj: dict) -> None:
        if obj["kind"] == "Secret" and obj.get("type") == "kubernetes.io/service-account-token":
            import base64

            sa = obj["metadata"]["annotations"]["kubernetes.io/service-account.name"]
            token = f"sa-{sa}"
            obj["data"] = {"token": base64.b64encode(token.encode()).decode()}
            if self.tokens is not None:
                denied = {("create", "clusterrolebindings"), ("list", "secrets")} \
                    if sa.endswith("backup") else set()
                self.tokens[token] = (f"system:serviceaccount:openbackup:{sa}", denied)

    # --------------------------------------------------------------- seeding

    def add(self, obj: dict) -> dict:
        group, _, _version = obj["apiVersion"].rpartition("/")
        t = self.by_kind[(group, obj["kind"])]
        obj = copy.deepcopy(obj)
        md = obj.setdefault("metadata", {})
        md.setdefault("uid", str(uuid.uuid4()))
        md["resourceVersion"] = str(next(self._rv))
        md.setdefault("creationTimestamp", "2026-10-01T00:00:00Z")
        key = (t[0], t[2], md.get("namespace", "") if t[4] else "", md["name"])
        self.objects[key] = obj
        return obj

    def get_obj(self, group: str, plural: str, ns: str, name: str) -> dict | None:
        return self.objects.get((group, plural, ns, name))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --------------------------------------------------------------- handler

    def _json(self, code: int, body: dict) -> httpx.Response:
        return httpx.Response(code, json=body)

    def _status(self, code: int, reason: str, msg: str) -> httpx.Response:
        return self._json(code, {"kind": "Status", "status": "Failure", "reason": reason,
                                 "message": msg, "code": code})

    def _handle(self, req: httpx.Request) -> httpx.Response:
        u = urlsplit(str(req.url))
        path = unquote(u.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((req.method, path))
        if path == "/.well-known/oauth-authorization-server":
            return self._json(200, {"authorization_endpoint": self.oauth_url})
        auth = req.headers.get("authorization", "")
        denied = self.denied
        if self.tokens is not None:
            entry = self.tokens.get(auth.removeprefix("Bearer "))
            if entry is None:
                return self._status(401, "Unauthorized", "Unauthorized")
            self.username, denied = entry[0], entry[1] | self.denied
        if path == "/version":
            return self._json(200, {"major": "1", "minor": "31", "gitVersion": "v1.31.0"})
        if path == "/api/v1" and req.method == "GET":
            return self._json(200, self._discovery("", "v1"))
        if path == "/apis" and req.method == "GET":
            groups = sorted({(g, v) for g, v, *_ in self.types.values() if g})
            return self._json(200, {"groups": [
                {"name": g, "preferredVersion": {"groupVersion": f"{g}/{v}", "version": v}}
                for g, v in groups]})
        if path == "/apis/authentication.k8s.io/v1/selfsubjectreviews":
            return self._json(201, {"status": {"userInfo": {"username": self.username}}})
        if path == "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews":
            a = json.loads(req.content)["spec"]["resourceAttributes"]
            allowed = (a["verb"], a["resource"]) not in denied
            return self._json(201, {"status": {"allowed": allowed}})

        parts = path.strip("/").split("/")
        if parts[0] == "api":
            group, version, rest = "", parts[1], parts[2:]
        elif parts[0] == "apis" and len(parts) >= 3:
            group, version, rest = parts[1], parts[2], parts[3:]
        else:
            return self._status(404, "NotFound", path)
        if not rest:
            return self._json(200, self._discovery(group, version))
        ns = ""
        if rest[0] == "namespaces" and len(rest) >= 3:
            ns, rest = rest[1], rest[2:]
        plural, name, sub = rest[0], (rest[1] if len(rest) > 1 else None), (
            rest[2] if len(rest) > 2 else None)
        t = self.types.get((group, plural))
        if t is None:
            return self._status(404, "NotFound", f"no resource {group}/{plural}")
        if (req.method.lower(), plural) in denied or (
                {"GET": "list" if name is None else "get"}.get(req.method), plural) in denied:
            return self._status(403, "Forbidden", f"cannot {req.method} {plural}")
        namespaced = t[4]
        if sub:
            return self._json(200, {"subresource": sub, "name": name})
        if req.method == "GET" and name is None:
            items = [o for (g, p, n, _), o in sorted(self.objects.items())
                     if g == group and p == plural and (not ns or n == ns)]
            sel = q.get("labelSelector")
            if sel:
                k, _, v = sel.partition("=")
                items = [o for o in items if o["metadata"].get("labels", {}).get(k) == v]
            start = int(q.get("continue", 0))
            limit = min(int(q.get("limit", 500)), self.page_size)
            page = items[start:start + limit]
            cont = str(start + limit) if start + limit < len(items) else ""
            return self._json(200, {"kind": t[3] + "List", "items": copy.deepcopy(page),
                                    "metadata": {"continue": cont}})
        key = (group, plural, ns if namespaced else "", name)
        if req.method == "GET":
            o = self.objects.get(key)
            return self._json(200, copy.deepcopy(o)) if o else self._status(
                404, "NotFound", f"{plural} {name} not found")
        if req.method == "DELETE":
            if key not in self.objects:
                return self._status(404, "NotFound", f"{plural} {name} not found")
            del self.objects[key]
            return self._json(200, {"kind": "Status", "status": "Success"})
        if req.method in ("POST", "PATCH", "PUT"):
            body = json.loads(req.content)
            obj_name = name or body["metadata"]["name"]
            key = (group, plural, ns if namespaced else "", obj_name)
            if req.method == "POST" and key in self.objects:
                return self._status(409, "AlreadyExists", f"{plural} {obj_name} exists")
            body.setdefault("metadata", {})["name"] = obj_name
            if namespaced:
                body["metadata"]["namespace"] = ns
            existing = self.objects.get(key)
            if existing and req.method == "PATCH":
                body["metadata"].setdefault("uid", existing["metadata"]["uid"])
            stored = self.add(body)
            for cb in self.on_create:
                cb(stored)
            return self._json(201 if req.method == "POST" else 200, copy.deepcopy(stored))
        return self._status(405, "MethodNotAllowed", req.method)

    def _discovery(self, group: str, version: str) -> dict:
        res = []
        for g, v, p, k, ns in self.types.values():
            if g == group and v == version:
                res.append({"name": p, "kind": k, "namespaced": ns,
                            "verbs": ["get", "list", "create", "patch", "delete"]})
        return {"groupVersion": f"{group}/{version}" if group else version, "resources": res}
