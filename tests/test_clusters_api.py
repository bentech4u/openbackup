from __future__ import annotations

import subprocess

import pytest
from fake_kube import FakeKube

from openbackup.kube.client import KubeClient


@pytest.fixture(scope="module")
def ca_pem(tmp_path_factory):
    d = tmp_path_factory.mktemp("ca")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(d / "k"), "-out", str(d / "c"), "-subj", "/CN=test-ca",
                    "-addext", "basicConstraints=critical,CA:TRUE"],
                   check=True, capture_output=True)
    return (d / "c").read_text()


@pytest.fixture
def cluster(monkeypatch):
    from openbackup.api.routers import clusters

    fk = FakeKube()
    fk.denied.add(("list", "secrets"))  # cluster-reader cannot read secrets
    fk.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "shop"},
            "status": {"phase": "Active"}})
    fk.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "openshift-etcd"}})
    fk.add({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "pv-1"},
            "spec": {"csi": {"driver": "csi.vsphere.vmware.com",
                             "volumeHandle": "1946bed2-53a1-4ef6-b263-776c63f798be"}}})
    fk.add({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "pv-2"},
            "spec": {"csi": {"driver": "nfs.csi.k8s.io", "volumeHandle": "x"}}})
    fk.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": "db", "namespace": "shop"},
            "spec": {"volumeName": "pv-1", "storageClassName": "thin-csi",
                     "resources": {"requests": {"storage": "10Gi"}}},
            "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}}})
    fk.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": "shared", "namespace": "shop"},
            "spec": {"volumeName": "pv-2", "storageClassName": "nfs",
                     "resources": {"requests": {"storage": "1Gi"}}},
            "status": {"phase": "Bound"}})
    fk.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": "vm1", "namespace": "shop"}})
    monkeypatch.setattr(clusters, "client_factory",
                        lambda url, token, ca, **kw: KubeClient(url, token, ca,
                                                                transport=fk.transport()))
    return fk


def _add(api, ca_pem, **kw):
    body = {"name": "homelab", "api_url": "https://api.homelab.example:6443", "ca_pem": ca_pem,
            "backup_token": "backup-token-value", **kw}
    return api.post("/api/clusters", json=body)


def test_add_cluster_reports_identity_and_permissions(admin_api, cluster, ca_pem):
    r = _add(admin_api, ca_pem, restore_token="restore-token-value")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["backup"]["user"] == cluster.username
    assert body["backup"]["permissions"]["read_secrets"] is False
    assert body["backup"]["kubevirt"] and body["backup"]["openshift"]
    assert body["cluster"]["has_restore_token"] is True
    assert "test-ca" in body["cluster"]["ca_subjects"][0]
    listed = admin_api.get("/api/clusters").text
    for secret in ("backup-token-value", "restore-token-value"):
        assert secret not in r.text and secret not in listed


def test_namespaces_with_volumes(admin_api, cluster, ca_pem):
    cid = _add(admin_api, ca_pem).json()["cluster"]["id"]
    ns = admin_api.get(f"/api/clusters/{cid}/namespaces").json()
    assert [n["name"] for n in ns] == ["shop"]  # system namespaces hidden
    shop = ns[0]
    assert shop["vms"] == 1 and shop["pvc_bytes"] == 11 << 30
    pvcs = {p["name"]: p for p in shop["pvcs"]}
    assert pvcs["db"]["data_supported"] and not pvcs["shared"]["data_supported"]
    all_ns = admin_api.get(f"/api/clusters/{cid}/namespaces?include_system=true").json()
    assert {n["name"] for n in all_ns} == {"shop", "openshift-etcd"}


def test_token_that_cannot_read_is_refused(admin_api, cluster, ca_pem):
    cluster.denied.add(("list", "persistentvolumes"))
    r = _add(admin_api, ca_pem)
    assert r.status_code == 400 and "cluster-reader" in r.json()["detail"]


def test_bad_ca_and_roles(admin_api, cluster, ca_pem, make_user, login):
    assert _add(admin_api, "garbage").status_code == 400
    make_user("op", "operator")
    op = login("op")
    assert op.post("/api/clusters", json={}).status_code in (403, 422)
    r = op.post("/api/clusters", json={"name": "x", "api_url": "https://a:6443",
                                       "ca_pem": ca_pem, "backup_token": "t"})
    assert r.status_code == 403
