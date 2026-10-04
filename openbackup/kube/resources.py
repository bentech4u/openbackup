"""Capture a namespace's API objects for backup, and turn them back into an
ordered, cleaned set of objects for restore.

Captured: everything namespaced and listable, except what controllers or
OpenShift recreate (owned objects, Events, Endpoints, ReplicaSets...), and
Secrets, which are never backed up. Volume data is handled separately.
"""

from __future__ import annotations

import copy
from typing import Any

from .client import KubeClient, KubeError

# (group, plural) never captured.
EXCLUDED_TYPES = {
    ("", "events"), ("events.k8s.io", "events"),
    ("", "endpoints"), ("discovery.k8s.io", "endpointslices"),
    ("apps", "controllerrevisions"),
    ("coordination.k8s.io", "leases"),
    ("metrics.k8s.io", "pods"),
    ("packages.operators.coreos.com", "packagemanifests"),
    ("operators.coreos.com", "clusterserviceversions"),
    ("operators.coreos.com", "installplans"),
    ("image.openshift.io", "imagestreamtags"), ("image.openshift.io", "imagetags"),
    ("image.openshift.io", "imagestreamimages"),
    ("snapshot.storage.k8s.io", "volumesnapshots"),
    ("kubevirt.io", "virtualmachineinstancemigrations"),
    ("cdi.kubevirt.io", "dataimportcrons"),
}
# Legacy OpenShift views of RBAC duplicate rbac.authorization.k8s.io.
EXCLUDED_GROUPS = {"authorization.openshift.io", "metrics.k8s.io"}

# Kinds an application can genuinely own that cluster-reader cannot list;
# the backup account gets read access to these (kube/manifests). If they are
# still unreadable, that is worth a warning. Anything else cluster-reader
# cannot read is platform machinery (bare-metal hosts, IPAM, tuned...).
APP_LEVEL_RESTRICTED = {
    ("monitoring.coreos.com", "servicemonitors"), ("monitoring.coreos.com", "podmonitors"),
    ("monitoring.coreos.com", "prometheusrules"), ("monitoring.coreos.com", "probes"),
    ("monitoring.coreos.com", "alertmanagerconfigs"),
    ("monitoring.openshift.io", "alertingrules"),
    ("monitoring.openshift.io", "alertrelabelconfigs"),
    ("config.openshift.io", "imagepolicies"),
}

# Secrets the platform creates and recreates by itself (service account
# tokens, internal registry pull secrets); never part of a backup.
GENERATED_SECRET_TYPES = {"kubernetes.io/service-account-token"}
GENERATED_SECRET_ANNOTATIONS = ("kubernetes.io/service-account.name",
                                "openshift.io/internal-registry-auth-token.service-account")


def generated_secret(obj: dict) -> bool:
    if obj.get("type") in GENERATED_SECRET_TYPES:
        return True
    ann = obj["metadata"].get("annotations") or {}
    return any(a in ann for a in GENERATED_SECRET_ANNOTATIONS)


class SecretsNotReadable(KubeError):
    pass


# Injected into every namespace by the platform.
GENERATED_CONFIGMAPS = {"kube-root-ca.crt", "openshift-service-ca.crt"}

STRIP_METADATA = ("uid", "resourceVersion", "creationTimestamp", "managedFields",
                  "generation", "selfLink", "deletionTimestamp", "deletionGracePeriodSeconds",
                  "ownerReferences")
STRIP_ANNOTATIONS = {
    "deployment.kubernetes.io/revision",
    "pv.kubernetes.io/bind-completed", "pv.kubernetes.io/bound-by-controller",
    "volume.kubernetes.io/storage-provisioner", "volume.beta.kubernetes.io/storage-provisioner",
    "volume.kubernetes.io/selected-node",
    "kubectl.kubernetes.io/last-applied-configuration",
}
# Namespace annotations OpenShift assigns; kept only to preserve the UID range.
SCC_ANNOTATIONS = ("openshift.io/sa.scc.uid-range", "openshift.io/sa.scc.supplemental-groups",
                   "openshift.io/sa.scc.mcs")


def _group(api_version: str) -> str:
    return api_version.rpartition("/")[0]


def owned(obj: dict) -> bool:
    return any(r.get("controller") for r in obj["metadata"].get("ownerReferences") or [])


def clean(obj: dict) -> dict:
    """Strip server-assigned state so the object can be applied elsewhere."""
    o = copy.deepcopy(obj)
    o.pop("status", None)
    md = o.setdefault("metadata", {})
    for k in STRIP_METADATA:
        md.pop(k, None)
    ann = {k: v for k, v in (md.get("annotations") or {}).items() if k not in STRIP_ANNOTATIONS}
    if ann:
        md["annotations"] = ann
    else:
        md.pop("annotations", None)
    kind, spec = o.get("kind"), o.get("spec") or {}
    if kind == "Service":
        for k in ("clusterIP", "clusterIPs", "healthCheckNodePort"):
            spec.pop(k, None)
        for p in spec.get("ports") or []:
            p.pop("nodePort", None)  # would clash on another cluster
    elif kind == "PersistentVolumeClaim":
        spec.pop("volumeName", None)
        spec.pop("dataSource", None)
        spec.pop("dataSourceRef", None)
    elif kind == "Pod":
        spec.pop("nodeName", None)
    elif kind == "ServiceAccount":
        name = md.get("name", "")

        def generated(ref: dict) -> bool:
            n = ref.get("name", "")
            return n.startswith((f"{name}-dockercfg-", f"{name}-token-"))

        for field in ("secrets", "imagePullSecrets"):
            refs = [r for r in o.get(field) or [] if not generated(r)]
            if refs:
                o[field] = refs
            else:
                o.pop(field, None)
    return o


def capture_namespace(c: KubeClient, namespace: str,
                      include_secrets: bool = False) -> dict[str, Any]:
    """Everything needed to recreate ``namespace``, as plain JSON. Secrets
    only when asked for (the repository must then be encrypted)."""
    ns_obj = c.get(f"/api/v1/namespaces/{namespace}")
    objects: list[dict] = []
    skipped: list[str] = []  # app-level kinds that could not be read
    platform: list[str] = []  # platform kinds the backup account cannot read
    for r in c.resources():
        if not r.namespaced or "list" not in r.verbs or r.group in EXCLUDED_GROUPS:
            continue
        if (r.group, r.plural) in EXCLUDED_TYPES:
            continue
        is_secrets = (r.group, r.plural) == ("", "secrets")
        if is_secrets and not include_secrets:
            continue
        try:
            items = c.list(r.path(namespace))
        except KubeError as e:
            if is_secrets and e.status == 403:
                raise SecretsNotReadable(
                    "The backup account cannot read Secrets. On the OpenShift page, select the "
                    "cluster, choose Update permissions and allow reading Secrets.", 403) \
                    from None
            if e.status == 403 and (r.group, r.plural) not in APP_LEVEL_RESTRICTED:
                platform.append(f"{r.plural}.{r.group or 'core'}")
                continue
            if e.status in (403, 404, 405):
                skipped.append(f"{r.plural}.{r.group or 'core'}: {e}")
                continue
            raise
        for item in items:
            item.setdefault("apiVersion", r.api_version)
            item.setdefault("kind", r.kind)
            if owned(item):
                continue
            if r.kind == "ConfigMap" and item["metadata"]["name"] in GENERATED_CONFIGMAPS:
                continue
            if is_secrets and generated_secret(item):
                continue
            objects.append(clean(item))

    pvcs = [o for o in objects if o["kind"] == "PersistentVolumeClaim"]
    pvs, classes = {}, {}
    raw_pvcs = {p["metadata"]["name"]: p for p in
                c.list(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims")}
    for p in pvcs:
        raw = raw_pvcs.get(p["metadata"]["name"], {})
        vol = (raw.get("spec") or {}).get("volumeName")
        if vol:
            try:
                pvs[p["metadata"]["name"]] = c.get(f"/api/v1/persistentvolumes/{vol}")
            except KubeError:
                pass
        sc = (p.get("spec") or {}).get("storageClassName")
        if sc and sc not in classes:
            try:
                classes[sc] = clean(c.get(f"/apis/storage.k8s.io/v1/storageclasses/{sc}"))
            except KubeError:
                pass
    ns_clean = clean(ns_obj)
    labels = ns_clean["metadata"].get("labels") or {}
    labels.pop("kubernetes.io/metadata.name", None)
    return {
        "namespace": ns_clean,
        "objects": sorted(objects, key=lambda o: (o["kind"], o["metadata"]["name"])),
        "pvs": pvs,  # PVC name -> bound PV (as found; used for volume data)
        "storage_classes": classes,
        "skipped_types": skipped,
        "platform_types": platform,
    }


# ---------------------------------------------------------------- restore

ORDER = [
    {"ServiceAccount", "Role", "RoleBinding", "LimitRange", "ResourceQuota", "NetworkPolicy"},
    {"ConfigMap", "Secret", "ImageStream"},
    {"PersistentVolumeClaim"},
    {"Service"},
    {"Deployment", "StatefulSet", "DaemonSet", "DeploymentConfig", "ReplicaSet",
     "ReplicationController", "Job", "CronJob", "BuildConfig", "Pod",
     "HorizontalPodAutoscaler", "PodDisruptionBudget"},
    {"Route", "Ingress"},
]
LAST = {"VirtualMachine"}


def rank(obj: dict) -> int:
    kind = obj["kind"]
    if kind in LAST:
        return len(ORDER) + 1
    for i, kinds in enumerate(ORDER):
        if kind in kinds:
            return i
    return len(ORDER)  # custom resources, after the workloads they may refer to


def plan_restore(captured: dict, target_namespace: str, *,
                 storage_class_map: dict[str, str] | None = None,
                 route_host_map: dict[str, str] | None = None,
                 keep_uid_range: bool = True,
                 restore_secrets: bool = True) -> tuple[dict, list[dict]]:
    """The Namespace object and the ordered objects to apply into it."""
    source = captured["namespace"]["metadata"]["name"]
    scm = storage_class_map or {}
    rhm = route_host_map or {}

    ns = copy.deepcopy(captured["namespace"])
    ns["metadata"]["name"] = target_namespace
    ann = ns["metadata"].get("annotations") or {}
    if not keep_uid_range:
        for k in SCC_ANNOTATIONS:
            ann.pop(k, None)
    ns["metadata"]["annotations"] = ann
    ns.pop("spec", None)

    out = []
    for o in captured["objects"]:
        if o["kind"] == "Secret" and not restore_secrets:
            continue
        o = copy.deepcopy(o)
        o["metadata"]["namespace"] = target_namespace
        kind = o["kind"]
        if kind in ("RoleBinding",):
            for s in o.get("subjects") or []:
                if s.get("namespace") == source:
                    s["namespace"] = target_namespace
        elif kind == "PersistentVolumeClaim":
            sc = o["spec"].get("storageClassName")
            if sc in scm:
                o["spec"]["storageClassName"] = scm[sc]
        elif kind == "Route":
            host = o.get("spec", {}).get("host")
            if host:
                for old, new in rhm.items():
                    if host.endswith(old):
                        o["spec"]["host"] = host[: len(host) - len(old)] + new
                        break
        out.append(o)
    out.sort(key=lambda o: (rank(o), o["kind"], o["metadata"]["name"]))
    return ns, out


def summarize(captured: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in captured["objects"]:
        counts[o["kind"]] = counts.get(o["kind"], 0) + 1
    return dict(sorted(counts.items()))
