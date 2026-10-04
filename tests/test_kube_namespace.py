from __future__ import annotations

import json

import pytest
from fake_kube import FakeKube

from openbackup.engine.context import NullContext
from openbackup.kube.client import KubeClient, KubeError
from openbackup.kube.engine import (
    RESOURCES_KEY,
    NamespaceBackupOptions,
    NamespaceRestoreOptions,
    backup_namespace,
    read_blob,
    restore_namespace,
)
from openbackup.kube.resources import capture_namespace, plan_restore
from openbackup.repo.repository import Repository

OWNER = [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "web-1", "uid": "x",
          "controller": True}]


def seed(fk: FakeKube, ns: str = "shop") -> None:
    fk.add({"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": ns, "labels": {"kubernetes.io/metadata.name": ns, "team": "a"},
                         "annotations": {"openshift.io/sa.scc.uid-range": "1000680000/10000",
                                         "openshift.io/requester": "alice"}}})
    fk.add({"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web", "namespace": ns,
                         "annotations": {"deployment.kubernetes.io/revision": "3"}},
            "spec": {"replicas": 2, "template": {"spec": {"containers": [{"name": "c"}]}}},
            "status": {"readyReplicas": 2}})
    fk.add({"apiVersion": "apps/v1", "kind": "ReplicaSet",
            "metadata": {"name": "web-1", "namespace": ns,
                         "ownerReferences": [{**OWNER[0], "kind": "Deployment"}]}})
    fk.add({"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": "web-1-abc", "namespace": ns, "ownerReferences": OWNER},
            "spec": {"nodeName": "worker-0"}})
    fk.add({"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "web", "namespace": ns},
            "spec": {"clusterIP": "172.30.1.5", "clusterIPs": ["172.30.1.5"], "type": "NodePort",
                     "ports": [{"port": 80, "nodePort": 31000}]}})
    fk.add({"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "settings", "namespace": ns}, "data": {"x": "1"}})
    fk.add({"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "kube-root-ca.crt", "namespace": ns}})
    fk.add({"apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "db-password", "namespace": ns}, "data": {"p": "c2VjcmV0"}})
    fk.add({"apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {"name": "app", "namespace": ns},
            "secrets": [{"name": "app-dockercfg-x1"}, {"name": "custom"}],
            "imagePullSecrets": [{"name": "app-dockercfg-x1"}]})
    fk.add({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "pvc-1"},
            "spec": {"csi": {"driver": "csi.vsphere.vmware.com", "volumeHandle": "fcd-1"}}})
    fk.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": "data", "namespace": ns,
                         "annotations": {"pv.kubernetes.io/bind-completed": "yes"}},
            "spec": {"volumeName": "pvc-1", "storageClassName": "thin-csi",
                     "accessModes": ["ReadWriteOnce"],
                     "resources": {"requests": {"storage": "5Gi"}}},
            "status": {"phase": "Bound"}})
    fk.add({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": "thin-csi"}, "provisioner": "csi.vsphere.vmware.com"})
    fk.add({"apiVersion": "route.openshift.io/v1", "kind": "Route",
            "metadata": {"name": "web", "namespace": ns},
            "spec": {"host": "web-shop.apps.homelab.example", "to": {"name": "web"}}})
    fk.add({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": "app-edit", "namespace": ns},
            "subjects": [{"kind": "ServiceAccount", "name": "app", "namespace": ns}],
            "roleRef": {"kind": "ClusterRole", "name": "edit"}})
    fk.add({"apiVersion": "v1", "kind": "Event", "metadata": {"name": "e1", "namespace": ns}})
    fk.add({"apiVersion": "v1", "kind": "Endpoints", "metadata": {"name": "web",
                                                                 "namespace": ns}})
    fk.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": "vm1", "namespace": ns}, "spec": {"running": True}})
    fk.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
            "metadata": {"name": "vm1", "namespace": ns,
                         "ownerReferences": [{"kind": "VirtualMachine", "name": "vm1",
                                              "controller": True}]}})


@pytest.fixture
def src():
    fk = FakeKube()
    seed(fk)
    return fk, KubeClient("https://src:6443", "t", "", transport=fk.transport())


def _by_kind(objs):
    return {(o["kind"], o["metadata"]["name"]): o for o in objs}


def test_capture_keeps_what_matters_and_drops_the_rest(src):
    _fk, c = src
    cap = capture_namespace(c, "shop")
    objs = _by_kind(cap["objects"])
    assert set(objs) == {("Deployment", "web"), ("Service", "web"), ("ConfigMap", "settings"),
                         ("ServiceAccount", "app"), ("PersistentVolumeClaim", "data"),
                         ("Route", "web"), ("RoleBinding", "app-edit"),
                         ("VirtualMachine", "vm1")}
    dep = objs[("Deployment", "web")]
    assert "status" not in dep and "uid" not in dep["metadata"]
    assert "annotations" not in dep["metadata"]
    svc = objs[("Service", "web")]["spec"]
    assert "clusterIP" not in svc and "nodePort" not in svc["ports"][0]
    assert "volumeName" not in objs[("PersistentVolumeClaim", "data")]["spec"]
    sa = objs[("ServiceAccount", "app")]
    assert sa["secrets"] == [{"name": "custom"}] and "imagePullSecrets" not in sa
    assert cap["pvs"]["data"]["spec"]["csi"]["volumeHandle"] == "fcd-1"
    assert "thin-csi" in cap["storage_classes"]
    assert "kubernetes.io/metadata.name" not in cap["namespace"]["metadata"]["labels"]
    assert "c2VjcmV0" not in json.dumps(cap)  # no secret material anywhere


def test_plan_maps_and_orders(src):
    _fk, c = src
    cap = capture_namespace(c, "shop")
    ns, objs = plan_restore(cap, "shop-dr", storage_class_map={"thin-csi": "ocs-rbd"},
                            route_host_map={".apps.homelab.example": ".apps.dr.example"})
    assert ns["metadata"]["name"] == "shop-dr"
    assert ns["metadata"]["annotations"]["openshift.io/sa.scc.uid-range"] == "1000680000/10000"
    kinds = [o["kind"] for o in objs]
    assert kinds.index("ServiceAccount") < kinds.index("PersistentVolumeClaim") \
        < kinds.index("Deployment") < kinds.index("Route") < kinds.index("VirtualMachine")
    m = _by_kind(objs)
    assert all(o["metadata"]["namespace"] == "shop-dr" for o in objs)
    assert m[("PersistentVolumeClaim", "data")]["spec"]["storageClassName"] == "ocs-rbd"
    assert m[("Route", "web")]["spec"]["host"] == "web-shop.apps.dr.example"
    assert m[("RoleBinding", "app-edit")]["subjects"][0]["namespace"] == "shop-dr"
    ns2, _ = plan_restore(cap, "x", keep_uid_range=False)
    assert "openshift.io/sa.scc.uid-range" not in ns2["metadata"]["annotations"]


def test_backup_and_restore_to_another_cluster(src, tmp_path):
    _fk, c = src
    repo = Repository.create(tmp_path / "repo", tmp_path / "idx", passphrase="namespace pass!")
    m = backup_namespace(c, repo, "shop", NamespaceBackupOptions(1, "homelab", "https://src"),
                         NullContext())
    assert m["subject_kind"] == "namespace" and m["vm"]["uuid"] == "k8s:homelab/shop"
    assert m["resources"]["Deployment"] == 1
    assert json.loads(read_blob(repo, m["id"], RESOURCES_KEY))["namespace"]

    dr = FakeKube()
    dc = KubeClient("https://dr:6443", "t", "", transport=dr.transport())
    ctx = NullContext()
    res = restore_namespace(dc, repo, m["id"], NamespaceRestoreOptions(
        "shop", storage_class_map={"thin-csi": "thin-dr"}), ctx)
    assert not res.errors and res.applied == 8
    assert dr.get_obj("", "namespaces", "", "shop")
    assert dr.get_obj("apps", "deployments", "shop", "web")["spec"]["replicas"] == 2
    assert dr.get_obj("", "persistentvolumeclaims", "shop", "data")["spec"][
        "storageClassName"] == "thin-dr"
    assert dr.get_obj("", "secrets", "shop", "db-password") is None
    assert any("not restored by this version" in m for _, m in ctx.messages)

    # Restoring again into the now existing namespace is refused unless merging.
    with pytest.raises(KubeError, match="already exists"):
        restore_namespace(dc, repo, m["id"], NamespaceRestoreOptions("shop"), NullContext())
    res = restore_namespace(dc, repo, m["id"], NamespaceRestoreOptions("shop", merge=True),
                            NullContext())
    assert not res.errors
    repo.close()


def test_kinds_the_target_lacks_are_skipped(src, tmp_path):
    from fake_kube import RESOURCES

    _fk, c = src
    repo = Repository.create(tmp_path / "repo", tmp_path / "idx")
    m = backup_namespace(c, repo, "shop", NamespaceBackupOptions(1, "homelab", "https://src"),
                         NullContext())
    plain = FakeKube([r for r in RESOURCES if r[0] not in ("kubevirt.io", "route.openshift.io")])
    pc = KubeClient("https://k8s:6443", "t", "", transport=plain.transport())
    res = restore_namespace(pc, repo, m["id"], NamespaceRestoreOptions("shop"), NullContext())
    assert sorted(s.split(":")[0] for s in res.skipped) == ["Route/web", "VirtualMachine/vm1"]
    assert not res.errors
    repo.close()
