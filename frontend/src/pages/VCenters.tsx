import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import { del, get, patch, post, type VCenter, type VmSummary } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Empty, Field, Loading, Modal, PageHeader, errorText } from "../components/ui";
import { bytes } from "../format";

export default function VCenters() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const vcs = useQuery({ queryKey: ["vcenters"], queryFn: () => get<VCenter[]>("/api/vcenters") });
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<VCenter | null>(null);
  const [removing, setRemoving] = useState<VCenter | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");
  const current = selected ?? vcs.data?.[0]?.id ?? null;

  return (
    <>
      <PageHeader
        title="vCenters"
        subtitle="The vSphere environments OpenBackup protects"
        actions={
          can("admin") && (
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus size={16} /> Add vCenter
            </Button>
          )
        }
      />
      <Alert>{error}</Alert>
      <Card pad={false}>
        {vcs.isLoading ? (
          <Loading />
        ) : !vcs.data?.length ? (
          <Empty>No vCenters yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Address</th>
                  <th>Account</th>
                  <th>Certificate (SHA-1)</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {vcs.data.map((v) => (
                  <tr key={v.id} className={`clickable ${current === v.id ? "selected" : ""}`} onClick={() => setSelected(v.id)}>
                    <td>
                      <strong>{v.name}</strong>
                    </td>
                    <td>
                      {v.host}:{v.port}
                    </td>
                    <td>{v.username}</td>
                    <td className="mono small">{v.thumbprint}</td>
                    <td className="right">
                      {can("admin") && (
                        <div className="row gap-s" style={{ justifyContent: "flex-end" }} onClick={(e) => e.stopPropagation()}>
                          <Button onClick={() => setEditing(v)}>Edit</Button>
                          <Button variant="ghost" onClick={() => setRemoving(v)}>
                            Remove
                          </Button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {current && <Inventory vcId={current} />}
      {adding && (
        <AddVCenter
          onClose={() => setAdding(false)}
          onDone={() => {
            setAdding(false);
            qc.invalidateQueries({ queryKey: ["vcenters"] });
          }}
        />
      )}
      {editing && (
        <EditVCenter
          vc={editing}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null);
            qc.invalidateQueries({ queryKey: ["vcenters"] });
          }}
        />
      )}
      {removing && (
        <Confirm
          title={`Remove ${removing.name}?`}
          message="OpenBackup forgets this vCenter. Existing restore points are kept. Jobs that use it must be deleted first."
          confirmLabel="Remove"
          danger
          onClose={() => setRemoving(null)}
          onConfirm={async () => {
            try {
              await del(`/api/vcenters/${removing.id}`);
              qc.invalidateQueries({ queryKey: ["vcenters"] });
            } catch (e) {
              setError(errorText(e));
            }
            setRemoving(null);
          }}
        />
      )}
    </>
  );
}

function Inventory({ vcId }: { vcId: number }) {
  const [filter, setFilter] = useState("");
  const [refresh, setRefresh] = useState(false);
  const vms = useQuery({
    queryKey: ["vms", vcId, refresh],
    queryFn: () => get<VmSummary[]>(`/api/vcenters/${vcId}/vms${refresh ? "?refresh=true" : ""}`),
  });
  const visible = (vms.data ?? []).filter((v) => v.name.toLowerCase().includes(filter.toLowerCase()));
  return (
    <Card
      title={
        <div className="row gap-s">
          Virtual machines
          <input className="search" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
      }
      actions={
        <Button onClick={() => (refresh ? vms.refetch() : setRefresh(true))} busy={vms.isFetching}>
          <RefreshCw size={14} /> Refresh
        </Button>
      }
      pad={false}
    >
      {vms.error && <Alert>{errorText(vms.error)}</Alert>}
      {vms.isLoading ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>VM</th>
                <th>Power</th>
                <th>Host</th>
                <th>CPU / RAM</th>
                <th>Disks</th>
                <th>CBT</th>
                <th>Tools</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((v) => (
                <tr key={v.moref}>
                  <td>
                    <div className="row gap-s">
                      {v.name} {v.is_template && <Badge>template</Badge>} {v.has_snapshots && <Badge tone="warn">snapshots</Badge>}
                    </div>
                    <div className="muted small">{v.guest_os}</div>
                  </td>
                  <td className="small">{v.power_state.replace("powered", "")}</td>
                  <td className="small">{v.host}</td>
                  <td className="nowrap small">
                    {v.cpu} · {bytes(v.memory_mb * 1048576, 0)}
                  </td>
                  <td className="nowrap small">
                    {v.disks} · {bytes(v.provisioned_bytes)}
                  </td>
                  <td>{v.cbt_enabled ? <Badge tone="good">on</Badge> : <Badge>off</Badge>}</td>
                  <td className="small muted">{v.tools_status.replace("guestTools", "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function AddVCenter({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [host, setHost] = useState("");
  const [port, setPort] = useState(443);
  const [thumbs, setThumbs] = useState<{ sha1: string; sha256: string } | null>(null);
  const [trusted, setTrusted] = useState(false);
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function fetchThumb() {
    setBusy(true);
    setError("");
    try {
      setThumbs(await post("/api/vcenters/thumbprint", { host: host.trim(), port }));
      if (!name) setName(host.split(".")[0]);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setError("");
    try {
      await post("/api/vcenters", { name, host: host.trim(), port, username, password, thumbprint: thumbs!.sha1 });
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Add vCenter"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          {!thumbs ? (
            <Button variant="primary" onClick={fetchThumb} busy={busy} disabled={!host.trim()}>
              Connect
            </Button>
          ) : (
            <Button variant="primary" onClick={save} busy={busy} disabled={!trusted || !username || !password || !name}>
              Test and save
            </Button>
          )}
        </>
      }
    >
      <div className="stack">
        <Alert>{error}</Alert>
        <div className="form-grid">
          <Field label="vCenter address">
            <input value={host} onChange={(e) => { setHost(e.target.value); setThumbs(null); setTrusted(false); }} placeholder="vcenter.example.com" disabled={!!thumbs} />
          </Field>
          <Field label="Port">
            <input type="number" value={port} onChange={(e) => setPort(Number(e.target.value))} disabled={!!thumbs} style={{ maxWidth: 120 }} />
          </Field>
        </div>
        {thumbs && (
          <>
            <Alert tone="warn">
              Check this fingerprint against the vCenter certificate before trusting it. OpenBackup pins it and refuses to connect if it
              changes.
            </Alert>
            <dl className="kv">
              <dt>SHA-1</dt>
              <dd className="mono">{thumbs.sha1}</dd>
              <dt>SHA-256</dt>
              <dd className="mono small">{thumbs.sha256}</dd>
            </dl>
            <label className="check">
              <input type="checkbox" checked={trusted} onChange={(e) => setTrusted(e.target.checked)} />I have verified this certificate fingerprint
            </label>
            {trusted && (
              <>
                <Field label="Display name">
                  <input value={name} onChange={(e) => setName(e.target.value)} />
                </Field>
                <Field label="Username" hint="A dedicated account with the backup privileges, e.g. backup@vsphere.local">
                  <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
                </Field>
                <Field label="Password">
                  <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
                </Field>
              </>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

function EditVCenter({ vc, onClose, onDone }: { vc: VCenter; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(vc.name);
  const [username, setUsername] = useState(vc.username);
  const [password, setPassword] = useState("");
  const [thumbprint, setThumbprint] = useState(vc.thumbprint);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    setError("");
    try {
      await patch(`/api/vcenters/${vc.id}`, {
        name: name !== vc.name ? name : null,
        username: username !== vc.username ? username : null,
        password: password || null,
        thumbprint: thumbprint !== vc.thumbprint ? thumbprint : null,
      });
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Edit ${vc.name}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} busy={busy}>
            Save
          </Button>
        </>
      }
    >
      <div className="stack">
        <Alert>{error}</Alert>
        <Field label="Display name">
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Username">
          <input value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="New password" hint="Leave empty to keep the current password">
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </Field>
        <Field label="Certificate SHA-1 thumbprint" hint="Change only after the vCenter certificate was replaced and you verified the new one">
          <input className="mono" value={thumbprint} onChange={(e) => setThumbprint(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}
