"""vCenter expires idle sessions (~30 min); a long disk read must not leave
the backup snapshot stranded because its removal ran on a dead session."""

from __future__ import annotations

from unittest import mock

from openbackup.vsphere import client
from openbackup.vsphere.client import VSphere


def _si(alive: bool):
    si = mock.Mock()
    si.content.sessionManager.currentSession = object() if alive else None
    si._stub = mock.Mock(name=f"stub-{'live' if alive else 'dead'}")
    return si


def test_expired_session_is_re_established(monkeypatch):
    logins = []

    def fake_connect(**kw):
        si = _si(True)
        logins.append(si)
        return si

    monkeypatch.setattr(client, "SmartConnect", fake_connect)
    monkeypatch.setattr(client, "Disconnect", lambda si: None)
    vs = VSphere("vc", "u", "p", "AB:CD")
    vs.KEEPALIVE_SECONDS = 3600
    vs.connect()
    first = vs.si
    vs.ensure_session()
    assert vs.si is first  # alive: no new login

    first.content.sessionManager.currentSession = None  # vCenter expired it
    vs.ensure_session()
    assert vs.si is not first and len(logins) == 2
    vs.close()


def test_snapshot_removal_uses_the_current_session(monkeypatch):
    monkeypatch.setattr(client, "SmartConnect", lambda **kw: _si(True))
    monkeypatch.setattr(client, "Disconnect", lambda si: None)
    vs = VSphere("vc", "u", "p", "AB:CD")
    vs.KEEPALIVE_SECONDS = 3600
    vs.connect()
    old_snap = client.vim.vm.Snapshot("snapshot-66", _si(False)._stub)
    vs.si.content.sessionManager.currentSession = None  # expired during the read

    real_rebind = vs.rebind
    bound = {}

    def spy_rebind(obj):
        bound["snap"] = real_rebind(obj)
        m = mock.Mock()
        m._moId, m._stub = bound["snap"]._moId, bound["snap"]._stub
        return m

    monkeypatch.setattr(vs, "rebind", spy_rebind)
    monkeypatch.setattr(vs, "wait", lambda task, **kw: None)
    vs.remove_snapshot(old_snap)
    assert bound["snap"]._moId == "snapshot-66"
    assert bound["snap"]._stub is vs.si._stub  # the fresh session, not the dead one
    vs.close()
