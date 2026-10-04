"""What a cluster offers for backup: namespaces, their persistent volumes and
KubeVirt VMs, and what the configured token is allowed to do."""

from __future__ import annotations

from collections import defaultdict

from .client import KubeClient, KubeError

VSPHERE_CSI = "csi.vsphere.vmware.com"
SYSTEM_PREFIXES = ("openshift", "kube-")
# "openbackup" holds only our own ServiceAccounts.
SYSTEM_NAMES = {"default", "openshift", "kube-system", "kube-public", "kube-node-lease",
                "openbackup"}


def is_system_namespace(name: str) -> bool:
    return name in SYSTEM_NAMES or name.startswith(SYSTEM_PREFIXES)


def parse_quantity(q: str | None) -> int:
    """Kubernetes storage quantity to bytes ('10Gi' -> 10737418240)."""
    if not q:
        return 0
    units = {"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "Pi": 1 << 50,
             "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15}
    for suffix in sorted(units, key=len, reverse=True):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * units[suffix])
    return int(float(q))


def namespaces(c: KubeClient, include_system: bool = False) -> list[dict]:
    pvs = {pv["metadata"]["name"]: pv for pv in c.list("/api/v1/persistentvolumes")}
    by_ns: dict[str, list[dict]] = defaultdict(list)
    for pvc in c.list("/api/v1/persistentvolumeclaims"):
        ns = pvc["metadata"]["namespace"]
        pv = pvs.get((pvc.get("spec") or {}).get("volumeName") or "")
        driver = ((pv or {}).get("spec") or {}).get("csi", {}).get("driver", "")
        by_ns[ns].append({
            "name": pvc["metadata"]["name"],
            "phase": (pvc.get("status") or {}).get("phase", ""),
            "size": parse_quantity(((pvc.get("status") or {}).get("capacity") or {}).get(
                "storage") or pvc["spec"].get("resources", {}).get("requests", {}).get("storage")),
            "storage_class": pvc["spec"].get("storageClassName") or "",
            "volume_mode": pvc["spec"].get("volumeMode") or "Filesystem",
            "driver": driver,
            "data_supported": driver == VSPHERE_CSI,
        })
    vms: dict[str, int] = defaultdict(int)
    if c.has_group("kubevirt.io"):
        try:
            for vm in c.list("/apis/kubevirt.io/v1/virtualmachines"):
                vms[vm["metadata"]["namespace"]] += 1
        except KubeError:
            pass
    out = []
    for ns in c.list("/api/v1/namespaces"):
        name = ns["metadata"]["name"]
        system = is_system_namespace(name)
        if system and not include_system:
            continue
        pvcs = by_ns.get(name, [])
        out.append({
            "name": name,
            "system": system,
            "phase": (ns.get("status") or {}).get("phase", ""),
            "pvcs": pvcs,
            "pvc_bytes": sum(p["size"] for p in pvcs),
            "vms": vms.get(name, 0),
        })
    return sorted(out, key=lambda n: n["name"])


def permissions(c: KubeClient) -> dict:
    """What the token may do, as the UI shows it when a cluster is added."""
    checks = {
        "list_namespaces": ("list", "namespaces", ""),
        "list_pvcs": ("list", "persistentvolumeclaims", ""),
        "list_pvs": ("list", "persistentvolumes", ""),
        "list_deployments": ("list", "deployments", "apps"),
        "read_secrets": ("list", "secrets", ""),
        "create_namespaces": ("create", "namespaces", ""),
        "create_pods": ("create", "pods", ""),
    }
    out = {k: c.can(verb, res, group) for k, (verb, res, group) in checks.items()}
    if c.has_group("kubevirt.io"):
        out["freeze_vms"] = c.can("update", "virtualmachineinstances", "subresources.kubevirt.io",
                                  subresource="freeze")
    return out
