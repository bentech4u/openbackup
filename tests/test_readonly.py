"""The read-only guard must refuse every operation that changes vSphere.

This exists because "do not touch vCenter" needs to be enforced by the code
rather than by remembering. An operator who sets it should be able to run any
command without auditing what it does first.
"""

from types import SimpleNamespace

import pytest

from openbackup.vsphere import cbt
from openbackup.vsphere.connection import (
    ReadOnlyError, VSphereConfig, VSphereConnection,
)
from openbackup.vsphere.snapshot import SnapshotSession


def conn_with(read_only: bool) -> VSphereConnection:
    return VSphereConnection(VSphereConfig(
        host="vc.invalid", user="u", password="p", read_only=read_only))


def fake_vm(name="vm1", cbt_on=False):
    return SimpleNamespace(
        name=name,
        config=SimpleNamespace(changeTrackingEnabled=cbt_on),
        snapshot=None,
        guest=SimpleNamespace(toolsStatus="toolsOk"),
        runtime=SimpleNamespace(powerState="poweredOn"),
    )


def test_ensure_writable_refuses_when_read_only():
    with pytest.raises(ReadOnlyError, match="refusing to do a thing"):
        conn_with(True).ensure_writable("do a thing")


def test_ensure_writable_allows_when_not_read_only():
    conn_with(False).ensure_writable("do a thing")   # must not raise


def test_snapshot_creation_is_refused():
    """A backup of a running VM needs a snapshot, so this is what stops one."""
    session = SnapshotSession(conn_with(True), fake_vm())
    with pytest.raises(ReadOnlyError, match="snapshot vm1"):
        session.create()


def test_snapshot_refusal_happens_before_any_api_call():
    """The guard has to come first: checking preconditions would already query
    vCenter, which is exactly what read-only mode is meant to avoid."""
    vm = fake_vm()

    def explode(*_a, **_k):
        raise AssertionError("touched vSphere despite read-only mode")

    vm.CreateSnapshot_Task = explode
    session = SnapshotSession(conn_with(True), vm)
    with pytest.raises(ReadOnlyError):
        session.create()


def test_enabling_cbt_is_refused():
    with pytest.raises(ReadOnlyError, match="enable CBT on vm1"):
        cbt.enable(fake_vm(cbt_on=False), conn=conn_with(True))


def test_cbt_already_enabled_is_not_a_change():
    """No write is needed, so read-only mode must not get in the way."""
    assert cbt.enable(fake_vm(cbt_on=True), conn=conn_with(True)) is False


def test_cbt_activation_is_refused():
    with pytest.raises(ReadOnlyError, match="stun vm1"):
        cbt.activate(fake_vm(), conn=conn_with(True))


def test_read_only_is_off_by_default():
    assert VSphereConfig(host="h", user="u", password="p").read_only is False


def test_env_file_can_turn_it_on(tmp_path):
    path = tmp_path / "lab.env"
    path.write_text(
        "VCENTER_HOST=vc\nVCENTER_USER=u\nVCENTER_PASS=p\n"
        "VCENTER_READ_ONLY=1\n")
    assert VSphereConfig.from_env_file(path).read_only is True
