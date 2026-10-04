from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from conftest import run_worker_until_idle

from openbackup.engine.context import NullContext
from openbackup.kube import etcd
from openbackup.repo.repository import Repository


def make_set(root, name, size=300_000):
    d = root / name
    d.mkdir(parents=True)
    stamp = name.replace("-", "_")
    (d / f"snapshot_{stamp}.db").write_bytes(os.urandom(size))
    (d / f"static_kuberesources_{stamp}.tar.gz").write_bytes(os.urandom(5000))
    return d


def test_find_sets_skips_incomplete(tmp_path):
    make_set(tmp_path, "20261001-0300")
    make_set(tmp_path, "20261001-0900")
    partial = tmp_path / "20261001-1500"
    partial.mkdir()
    (partial / "snapshot_x.db").write_bytes(b"x")  # still being written
    sets = etcd.find_sets(tmp_path)
    assert [s.name for s in sets] == ["20261001-0300", "20261001-0900"]
    assert sets[0].taken_at < sets[1].taken_at
    with pytest.raises(etcd.EtcdError):
        etcd.find_sets(tmp_path / "missing")


def test_ingest_round_trip_and_dedupe_by_set(tmp_path):
    d = make_set(tmp_path / "share", "20261001-0300", size=3_000_000)
    repo = Repository.create(tmp_path / "repo", tmp_path / "idx", passphrase="etcd pass phrase")
    (s,) = etcd.find_sets(tmp_path / "share")
    m = etcd.ingest(repo, s, "homelab", 7, "etcd", NullContext())
    assert m["subject_kind"] == "etcd" and m["vm"]["uuid"] == "etcd:homelab"
    assert etcd.collected_sets(repo, "homelab") == {"20261001-0300"}
    for f in m["files"]:
        bm = repo.load_map(m["id"], f["key"])
        data = b"".join(repo.read_chunk(c, bm.block_length(i)) for i, c in enumerate(bm.ids))
        assert data == (d / f["name"]).read_bytes()
    repo.close()


def test_collection_job_end_to_end(admin_api, setup, monkeypatch, tmp_path):
    from openbackup.repo import nfs

    share = tmp_path / "share"
    old = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y%m%d-%H%M")
    make_set(share / "etcd-backup", old)
    mounted = []

    def fake_mount(server, export, mp, options="", timeout=60, read_only=False):
        assert read_only
        mounted.append(mp)
        if not mp.exists():
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.symlink_to(share)
        return False

    monkeypatch.setattr(nfs, "ensure_mounted", fake_mount)
    body = {"name": "etcd", "kind": "etcd", "repository_id": setup["repo_id"],
            "retention_points": 30,
            "selection": {"source": {"server": "192.168.68.126", "export": "/volume1/homelab",
                                     "path": "etcd-backup", "label": "homelab"}}}
    bad = {**body, "selection": {"source": {**body["selection"]["source"], "path": "../x"}}}
    assert admin_api.post("/api/jobs", json=bad).status_code == 422
    job = admin_api.post("/api/jobs", json=body).json()

    tid = admin_api.post(f"/api/jobs/{job['id']}/run").json()["id"]
    run_worker_until_idle()
    t = admin_api.get(f"/api/tasks/{tid}").json()
    logs = [x["message"] for x in admin_api.get(f"/api/tasks/{tid}/logs").json()]
    assert t["state"] == "warning" and "1 new" in t["summary"], (t, logs)  # 3 days old
    assert mounted

    new = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    make_set(share / "etcd-backup", new)
    tid = admin_api.post(f"/api/jobs/{job['id']}/run").json()["id"]
    run_worker_until_idle()
    t = admin_api.get(f"/api/tasks/{tid}").json()
    assert t["state"] == "success" and "1 new" in t["summary"]  # the old one is not re-read

    pts = [p for p in admin_api.get("/api/points").json() if p["subject_kind"] == "etcd"]
    assert len(pts) == 2
    detail = admin_api.get(f"/api/points/{pts[0]['id']}").json()
    f = next(x for x in detail["files"] if x["name"].startswith("snapshot_"))
    r = admin_api.get(f"/api/points/{pts[0]['id']}/files/{f['key']}")
    assert r.status_code == 200
    assert r.content == (share / "etcd-backup" / detail["source_set"] / f["name"]).read_bytes()


def test_control_plane_vms_are_refused(admin_api, setup):
    job = admin_api.get(f"/api/jobs/{setup['job_id']}").json()
    body = {k: job[k] for k in ("name", "vcenter_id", "repository_id")}
    body["vms"] = [{"moref": "vm-9", "name": "homelab-np7jp-master-0"}]
    r = admin_api.put(f"/api/jobs/{setup['job_id']}", json=body)
    assert r.status_code == 422 and "control-plane" in r.text
