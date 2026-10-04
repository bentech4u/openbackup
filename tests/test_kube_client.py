from __future__ import annotations

import subprocess

import pytest
from fake_kube import FakeKube

from openbackup.kube.client import KubeClient, KubeError, describe_cert, validate_ca_bundle


@pytest.fixture
def kube():
    fk = FakeKube()
    fk.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "shop"}})
    for i in range(5):
        fk.add({"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"cm{i}", "namespace": "shop", "labels": {"i": str(i)}}})
    return fk, KubeClient("https://api.test:6443", "tok", "", transport=fk.transport())


def test_discovery(kube):
    _fk, c = kube
    kinds = {(r.group, r.kind) for r in c.resources()}
    assert ("apps", "Deployment") in kinds and ("route.openshift.io", "Route") in kinds
    dep = c.resource("apps/v1", "Deployment")
    assert dep.path("shop", "web") == "/apis/apps/v1/namespaces/shop/deployments/web"
    assert c.resource("v1", "Namespace").path(name="shop") == "/api/v1/namespaces/shop"
    assert c.has_group("kubevirt.io")


def test_list_follows_pagination(kube):
    _fk, c = kube
    items = c.list("/api/v1/namespaces/shop/configmaps")
    assert [i["metadata"]["name"] for i in items] == [f"cm{i}" for i in range(5)]
    assert len(c.list("/api/v1/namespaces/shop/configmaps", label_selector="i=3")) == 1


def test_apply_create_delete(kube):
    fk, c = kube
    cm = c.resource("v1", "ConfigMap")
    c.apply(cm.path("shop", "new"), {"apiVersion": "v1", "kind": "ConfigMap",
                                     "metadata": {"name": "new"}, "data": {"a": "1"}})
    assert fk.get_obj("", "configmaps", "shop", "new")["data"] == {"a": "1"}
    with pytest.raises(KubeError) as e:
        c.create(cm.path("shop"), {"apiVersion": "v1", "kind": "ConfigMap",
                                   "metadata": {"name": "new"}})
    assert e.value.status == 409
    c.delete(cm.path("shop", "new"))
    c.delete(cm.path("shop", "new"))  # already gone is fine
    assert fk.get_obj("", "configmaps", "shop", "new") is None


def test_identity_and_access(kube):
    fk, c = kube
    fk.denied.add(("list", "secrets"))
    assert c.whoami() == fk.username
    assert c.can("list", "configmaps") and not c.can("list", "secrets")


def test_errors_are_readable(kube):
    fk, c = kube
    with pytest.raises(KubeError) as e:
        c.get("/api/v1/namespaces/shop/configmaps/missing")
    assert e.value.status == 404


@pytest.fixture
def ca_pair(tmp_path):
    ca_key, ca_crt = tmp_path / "ca.key", tmp_path / "ca.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(ca_key), "-out", str(ca_crt),
                    "-subj", "/CN=kube-apiserver-lb-signer",
                    "-addext", "basicConstraints=critical,CA:TRUE"],
                   check=True, capture_output=True)
    return ca_crt.read_text()


def test_ca_bundle_validation(ca_pair):
    certs = validate_ca_bundle(ca_pair)
    assert certs[0].is_ca and "kube-apiserver-lb-signer" in certs[0].subject
    assert len(describe_cert(ca_pair).sha256.split(":")) == 32
    with pytest.raises(KubeError):
        validate_ca_bundle("not a certificate")
