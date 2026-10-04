import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, FolderOpen, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import { del, get, post, type Placement, type PointDetail, type RestorePoint, type Task, type VCenter } from "../api";
import type { Cluster } from "./Clusters";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Empty, Field, Loading, Modal, PageHeader, errorText } from "../components/ui";
import { bytes, dateTime, relative } from "../format";

interface ProtectedVm {
  subject_kind: "vm" | "namespace" | "etcd";
  vm_uuid: string;
  vm_name: string;
  points: number;
  latest: string;
  stored_bytes: number;
}

export default function Restore() {
  const [filter, setFilter] = useState("");
  const [vm, setVm] = useState<ProtectedVm | null>(null);
  const vms = useQuery({ queryKey: ["protected-vms"], queryFn: () => get<ProtectedVm[]>("/api/points/vms") });
  const visible = (vms.data ?? []).filter((v) => v.vm_name.toLowerCase().includes(filter.toLowerCase()));

  return (
    <>
      <PageHeader title="Restore" subtitle="Pick a VM, then the point in time to go back to" />
      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <Card title={<input className="search" placeholder="Search VMs…" value={filter} onChange={(e) => setFilter(e.target.value)} />} pad={false}>
          {vms.isLoading ? (
            <Loading />
          ) : !visible.length ? (
            <Empty>No backed-up VMs{filter ? " match" : " yet"}.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>VM</th>
                    <th>Points</th>
                    <th>Latest</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((v) => (
                    <tr key={v.vm_uuid} className={`clickable ${vm?.vm_uuid === v.vm_uuid ? "selected" : ""}`} onClick={() => setVm(v)}>
                      <td>
                        <span className="row gap-s">
                          {v.vm_name}
                          {v.subject_kind === "namespace" && <Badge tone="info">namespace</Badge>}
                          {v.subject_kind === "etcd" && <Badge tone="info">etcd</Badge>}
                        </span>
                      </td>
                      <td>{v.points}</td>
                      <td className="nowrap">{relative(v.latest)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        {vm ? <PointList vm={vm} /> : <Card><Empty>Select a VM to see its restore points.</Empty></Card>}
      </div>
    </>
  );
}

function PointList({ vm }: { vm: ProtectedVm }) {
  const nav = useNavigate();
  const points = useQuery({
    queryKey: ["points", vm.vm_uuid],
    queryFn: () => get<RestorePoint[]>(`/api/points?vm_uuid=${encodeURIComponent(vm.vm_uuid)}`),
  });
  return (
    <Card title={`Restore points of ${vm.vm_name}`} pad={false}>
      {points.isLoading ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Created</th>
                <th>Type</th>
                <th>Read</th>
                <th>New data</th>
              </tr>
            </thead>
            <tbody>
              {points.data?.map((p) => (
                <tr key={p.id} className="clickable" onClick={() => nav(`/points/${p.id}`)}>
                  <td>
                    <div>{dateTime(p.created_at)}</div>
                    <div className="muted small">{relative(p.created_at)}</div>
                  </td>
                  <td>
                    <Badge tone={p.kind === "full" ? "info" : "neutral"}>{p.kind}</Badge>
                  </td>
                  <td className="nowrap">{bytes(p.read_bytes)}</td>
                  <td className="nowrap">{bytes(p.new_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export function PointPage() {
  const id = useParams().id!;
  const nav = useNavigate();
  const { can } = useAuth();
  const [restoring, setRestoring] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const point = useQuery({ queryKey: ["point", id], queryFn: () => get<PointDetail>(`/api/points/${id}`) });

  if (point.isLoading) return <Loading />;
  if (point.error) return <Alert>{errorText(point.error)}</Alert>;
  const p = point.data!;
  if (p.subject_kind === "namespace") return <NamespacePoint point={p as NamespacePointDetail} />;

  async function startTask(fn: () => Promise<Task>) {
    setBusy(true);
    setError("");
    try {
      const t = await fn();
      nav(`/tasks/${t.id}`);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title={`${p.vm_name} — ${dateTime(p.created_at)}`}
        subtitle={
          <span className="row gap-s">
            <Button variant="ghost" onClick={() => nav("/restore")}>
              <ArrowLeft size={14} /> All VMs
            </Button>
            <span className="mono muted">{p.id}</span>
          </span>
        }
        actions={
          <>
            {can("operator") && (
              <Button onClick={() => startTask(() => post<Task>(`/api/points/${id}/verify`))} busy={busy}>
                <ShieldCheck size={15} /> Verify
              </Button>
            )}
            {can("admin") && (
              <Button onClick={() => setDeleting(true)}>
                <Trash2 size={15} /> Delete
              </Button>
            )}
            {can("operator") && (
              <Button onClick={() => nav(`/points/${id}/files`)}>
                <FolderOpen size={15} /> Browse files
              </Button>
            )}
            {can("operator") && (
              <Button variant="primary" onClick={() => setRestoring(true)}>
                <RotateCcw size={15} /> Restore…
              </Button>
            )}
          </>
        }
      />
      <Alert>{error}</Alert>
      {p.warnings?.length > 0 && <Alert tone="warn">{p.warnings.join(" · ")}</Alert>}
      <div className="grid grid-2">
        <Card title="Virtual machine">
          <dl className="kv">
            <dt>Name</dt>
            <dd>{p.config.name}</dd>
            <dt>Guest OS</dt>
            <dd>{p.config.guest_full_name || "—"}</dd>
            <dt>CPU / memory</dt>
            <dd>
              {p.config.cpu} vCPU · {bytes(p.config.memory_mb * 1024 * 1024, 0)}
            </dd>
            <dt>Firmware</dt>
            <dd>{p.config.firmware ?? "bios"}</dd>
            <dt>Networks</dt>
            <dd>{p.config.nics.map((n) => `${n.network} (${n.type})`).join(", ") || "—"}</dd>
            <dt>vCenter</dt>
            <dd>{p.vcenter || "—"}</dd>
          </dl>
        </Card>
        <Card title="Backup">
          <dl className="kv">
            <dt>Type</dt>
            <dd>{p.kind}</dd>
            <dt>Logical size</dt>
            <dd>{bytes(p.logical_bytes)}</dd>
            <dt>Read from vSphere</dt>
            <dd>{bytes(p.read_bytes)}</dd>
            <dt>New data stored</dt>
            <dd>{bytes(p.new_bytes)}</dd>
            <dt>Duration</dt>
            <dd>{p.duration_s != null ? `${Math.round(p.duration_s)} s` : "—"}</dd>
          </dl>
        </Card>
      </div>
      <Card title="Disks" pad={false}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Disk</th>
                <th>Capacity</th>
                <th>Mode</th>
                <th>Read</th>
              </tr>
            </thead>
            <tbody>
              {p.disk_details.map((d) => (
                <tr key={d.key}>
                  <td>{d.label}</td>
                  <td>{bytes(d.capacity)}</td>
                  <td>{d.mode}</td>
                  <td>{bytes(d.read_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {restoring && <RestoreWizard point={p} onClose={() => setRestoring(false)} />}
      {deleting && (
        <Confirm
          title="Delete this restore point?"
          message="The point is removed from the repository and space no other point uses is reclaimed. This cannot be undone."
          confirmLabel="Delete"
          danger
          requireText={p.vm_name}
          busy={busy}
          onConfirm={() => startTask(() => del<Task>(`/api/points/${id}`))}
          onClose={() => setDeleting(false)}
        />
      )}
    </>
  );
}

type Mode = "new_vm" | "in_place" | "export";

function RestoreWizard({ point, onClose }: { point: PointDetail; onClose: () => void }) {
  const nav = useNavigate();
  const { can } = useAuth();
  const [mode, setMode] = useState<Mode>("new_vm");
  const vcs = useQuery({ queryKey: ["vcenters"], queryFn: () => get<VCenter[]>("/api/vcenters") });
  const defaultVc = vcs.data?.find((v) => v.host === point.vcenter)?.id ?? vcs.data?.[0]?.id;
  const [vcId, setVcId] = useState<number | undefined>(undefined);
  const vcenterId = vcId ?? defaultVc;
  const placement = useQuery({
    queryKey: ["placement", vcenterId],
    queryFn: () => get<Placement>(`/api/vcenters/${vcenterId}/placement`),
    enabled: !!vcenterId && mode === "new_vm",
  });
  const [name, setName] = useState(`${point.vm_name}-restored`);
  const [folder, setFolder] = useState("");
  const [pool, setPool] = useState("");
  const [datastore, setDatastore] = useState("");
  const [host, setHost] = useState("");
  const [netMap, setNetMap] = useState<Record<string, string>>({});
  const [powerOn, setPowerOn] = useState(false);
  const [format, setFormat] = useState("vmdk");
  const [confirmText, setConfirmText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const pl = placement.data;
  const sourceNets = useMemo(() => [...new Set(point.config.nics.map((n) => n.network))], [point]);
  const need = point.disk_details.reduce((a, d) => a + d.capacity, 0);
  const ds = pl?.datastores.find((d) => d.name === datastore);

  async function submit() {
    setError("");
    const body: Record<string, unknown> = { mode, vcenter_id: vcenterId };
    if (mode === "new_vm") {
      if (!folder || !pool || !datastore) {
        setError("Choose a folder, resource pool and datastore");
        return;
      }
      body.target = { name, folder, resource_pool: pool, datastore, host: host || null, network_map: netMap, power_on: powerOn };
    } else if (mode === "in_place") {
      if (confirmText !== point.vm_name) {
        setError(`Type ${point.vm_name} to confirm`);
        return;
      }
      body.power_on = powerOn;
    } else {
      body.format = format;
    }
    setBusy(true);
    try {
      const t = await post<Task>(`/api/points/${point.id}/restore`, body);
      nav(`/tasks/${t.id}`);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Restore ${point.vm_name}`}
      onClose={onClose}
      wide
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant={mode === "in_place" ? "danger" : "primary"} onClick={submit} busy={busy}>
            {mode === "in_place" ? "Overwrite VM" : "Start restore"}
          </Button>
        </>
      }
    >
      <div className="stack">
        <div className="segmented">
          <button className={mode === "new_vm" ? "on" : ""} onClick={() => setMode("new_vm")}>
            As a new VM
          </button>
          {can("admin") && (
            <button className={mode === "in_place" ? "on" : ""} onClick={() => setMode("in_place")}>
              Over the original
            </button>
          )}
          <button className={mode === "export" ? "on" : ""} onClick={() => setMode("export")}>
            Export disks
          </button>
        </div>
        <Alert>{error}</Alert>
        {mode !== "export" && (
          <Field label="vCenter">
            <select value={vcenterId ?? ""} onChange={(e) => setVcId(Number(e.target.value))}>
              {vcs.data?.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name} ({v.host})
                </option>
              ))}
            </select>
          </Field>
        )}
        {mode === "new_vm" && (
          <>
            {placement.isLoading && <Loading />}
            {placement.error && <Alert>{errorText(placement.error)}</Alert>}
            {pl && (
              <>
                <div className="form-grid">
                  <Field label="New VM name">
                    <input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} />
                  </Field>
                  <Field label="Folder">
                    <select value={folder} onChange={(e) => setFolder(e.target.value)}>
                      <option value="">Choose…</option>
                      {pl.folders.map((f) => (
                        <option key={f.moref} value={f.moref}>
                          {f.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Resource pool">
                    <select value={pool} onChange={(e) => setPool(e.target.value)}>
                      <option value="">Choose…</option>
                      {pl.resource_pools.map((r) => (
                        <option key={r.moref} value={r.moref}>
                          {r.owner ? `${r.owner} / ` : ""}
                          {r.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Host (optional)">
                    <select value={host} onChange={(e) => setHost(e.target.value)}>
                      <option value="">Let vCenter choose</option>
                      {pl.hosts
                        .filter((h) => h.connected && !h.maintenance)
                        .map((h) => (
                          <option key={h.moref} value={h.moref}>
                            {h.name}
                          </option>
                        ))}
                    </select>
                  </Field>
                  <Field
                    label="Datastore"
                    error={ds && ds.free < need ? `Needs up to ${bytes(need)}; ${bytes(ds.free)} free` : undefined}
                    hint={`Disks need up to ${bytes(need)}`}
                  >
                    <select value={datastore} onChange={(e) => setDatastore(e.target.value)}>
                      <option value="">Choose…</option>
                      {pl.datastores
                        .filter((d) => d.accessible)
                        .map((d) => (
                          <option key={d.moref} value={d.name}>
                            {d.name} ({bytes(d.free)} free)
                          </option>
                        ))}
                    </select>
                  </Field>
                </div>
                {sourceNets.length > 0 && (
                  <Card title="Network mapping">
                    <div className="form-grid">
                      {sourceNets.map((n) => (
                        <Field key={n} label={`Original: ${n}`}>
                          <select value={netMap[n] ?? n} onChange={(e) => setNetMap({ ...netMap, [n]: e.target.value })}>
                            {!pl.networks.some((x) => x.name === n) && <option value={n}>— not found, choose —</option>}
                            {pl.networks.map((x) => (
                              <option key={x.moref} value={x.name}>
                                {x.name}
                              </option>
                            ))}
                          </select>
                        </Field>
                      ))}
                    </div>
                  </Card>
                )}
                <Alert tone="info">
                  The new VM gets new MAC addresses. If the original is still running, consider connecting the restored VM to an isolated
                  network first.
                </Alert>
                <label className="check">
                  <input type="checkbox" checked={powerOn} onChange={(e) => setPowerOn(e.target.checked)} />
                  Power on after restore
                </label>
              </>
            )}
          </>
        )}
        {mode === "in_place" && (
          <>
            <Alert tone="warn">
              <strong>{point.vm_name}</strong> will be powered off and every disk overwritten with the state from {dateTime(point.created_at)}.
              Everything written since then is lost. The VM must have no snapshots and the same disk layout.
            </Alert>
            <Field label={`Type ${point.vm_name} to confirm`}>
              <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off" />
            </Field>
            <label className="check">
              <input type="checkbox" checked={powerOn} onChange={(e) => setPowerOn(e.target.checked)} />
              Power on after restore
            </label>
          </>
        )}
        {mode === "export" && (
          <>
            <Field label="Format">
              <select value={format} onChange={(e) => setFormat(e.target.value)}>
                <option value="vmdk">VMDK (stream-optimized, for vSphere import)</option>
                <option value="qcow2">QCOW2 (KVM, Proxmox)</option>
                <option value="raw">Raw image</option>
              </select>
            </Field>
            <Alert tone="info">Files are written to the repository under exports/{point.id}/.</Alert>
          </>
        )}
      </div>
    </Modal>
  );
}

interface NamespacePointDetail extends PointDetail {
  cluster: { id: number; name: string; api_url: string };
  namespace: string;
  resources: Record<string, number>;
  pvcs: { name: string; storage_class: string; size: string; volume_mode: string; data: boolean }[];
  skipped_types: string[];
}

function NamespacePoint({ point: p }: { point: NamespacePointDetail }) {
  const nav = useNavigate();
  const { can } = useAuth();
  const [restoring, setRestoring] = useState(false);
  const total = Object.values(p.resources).reduce((a, b) => a + b, 0);
  return (
    <>
      <PageHeader
        title={`${p.vm_name} — ${dateTime(p.created_at)}`}
        subtitle={
          <span className="row gap-s">
            <Button variant="ghost" onClick={() => nav("/restore")}>
              <ArrowLeft size={14} /> All backups
            </Button>
            <Badge tone="info">OpenShift namespace</Badge>
            <span className="mono muted">{p.id}</span>
          </span>
        }
        actions={
          can("operator") && (
            <Button variant="primary" onClick={() => setRestoring(true)}>
              <RotateCcw size={15} /> Restore namespace…
            </Button>
          )
        }
      />
      {p.warnings?.length > 0 && <Alert tone="warn">{p.warnings.join(" · ")}</Alert>}
      <div className="grid grid-2">
        <Card title="Namespace">
          <dl className="kv">
            <dt>Cluster</dt>
            <dd>{p.cluster.name}</dd>
            <dt>Namespace</dt>
            <dd>{p.namespace}</dd>
            <dt>Objects</dt>
            <dd>{total}</dd>
            <dt>Secrets</dt>
            <dd className="muted">not backed up</dd>
          </dl>
        </Card>
        <Card title="Objects by kind">
          <dl className="kv">
            {Object.entries(p.resources).map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </Card>
      </div>
      {p.pvcs.length > 0 && (
        <Card title="Persistent volumes" pad={false}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Claim</th>
                  <th>Size</th>
                  <th>Storage class</th>
                  <th>Mode</th>
                  <th>Data</th>
                </tr>
              </thead>
              <tbody>
                {p.pvcs.map((v) => (
                  <tr key={v.name}>
                    <td>{v.name}</td>
                    <td>{v.size}</td>
                    <td>{v.storage_class}</td>
                    <td>{v.volume_mode}</td>
                    <td>{v.data ? <Badge tone="good">backed up</Badge> : <Badge>claim only</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      {p.skipped_types?.length > 0 && (
        <Alert tone="warn">Not readable at backup time, so not included: {p.skipped_types.join("; ")}</Alert>
      )}
      {restoring && <NamespaceRestore point={p} onClose={() => setRestoring(false)} />}
    </>
  );
}

function NamespaceRestore({ point, onClose }: { point: NamespacePointDetail; onClose: () => void }) {
  const nav = useNavigate();
  const { can } = useAuth();
  const clusters = useQuery({ queryKey: ["clusters"], queryFn: () => get<Cluster[]>("/api/clusters") });
  const [clusterId, setClusterId] = useState<number | undefined>(undefined);
  const target = clusterId ?? point.cluster.id;
  const cluster = clusters.data?.find((c) => c.id === target);
  const [nsName, setNsName] = useState(`${point.namespace}-restored`);
  const [merge, setMerge] = useState(false);
  const [keepUid, setKeepUid] = useState(true);
  const [includeData, setIncludeData] = useState(true);
  const classes = [...new Set(point.pvcs.map((v) => v.storage_class).filter(Boolean))];
  const [scMap, setScMap] = useState<Record<string, string>>({});
  const [routeFrom, setRouteFrom] = useState("");
  const [routeTo, setRouteTo] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setError("");
    setBusy(true);
    try {
      const t = await post<Task>(`/api/points/${point.id}/restore`, {
        mode: "namespace",
        namespace: {
          cluster_id: target,
          target_namespace: nsName.trim(),
          storage_class_map: Object.fromEntries(Object.entries(scMap).filter(([a, b]) => b && a !== b)),
          route_host_map: routeFrom.trim() && routeTo.trim() ? { [routeFrom.trim()]: routeTo.trim() } : {},
          keep_uid_range: keepUid,
          merge,
          include_data: includeData,
          restore_token: token.trim() || null,
        },
      });
      nav(`/tasks/${t.id}`);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Restore ${point.vm_name}`}
      onClose={onClose}
      wide
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant={merge ? "danger" : "primary"} onClick={submit} busy={busy}>
            {merge ? "Restore into existing namespace" : "Restore"}
          </Button>
        </>
      }
    >
      <div className="stack">
        <Alert>{error}</Alert>
        <div className="form-grid">
          <Field label="Target cluster" hint={target === point.cluster.id ? "The cluster it was backed up from" : "Another cluster (e.g. DR)"}>
            <select value={target} onChange={(e) => setClusterId(Number(e.target.value))}>
              {clusters.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.id === point.cluster.id ? " (original)" : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Target namespace" hint="Lowercase letters, digits and dashes">
            <input value={nsName} onChange={(e) => setNsName(e.target.value)} />
          </Field>
        </div>
        <label className="check" style={{ opacity: can("admin") ? 1 : 0.5 }}>
          <input type="checkbox" disabled={!can("admin")} checked={merge} onChange={(e) => setMerge(e.target.checked)} />
          <span>
            The namespace already exists: restore into it, replacing objects with the backed-up versions
            {!can("admin") && <span className="muted small"> (admins only)</span>}
          </span>
        </label>
        {cluster && !cluster.has_restore_token && (
          <Field label="Restore token" hint="This cluster has no stored restore token. It is used for this restore only and not kept.">
            <textarea className="mono small" rows={2} value={token} onChange={(e) => setToken(e.target.value)} />
          </Field>
        )}
        {classes.length > 0 && (
          <Card title="Storage classes">
            <div className="form-grid">
              {classes.map((c) => (
                <Field key={c} label={`Backed up as ${c}`} hint="Storage class on the target cluster">
                  <input value={scMap[c] ?? c} onChange={(e) => setScMap({ ...scMap, [c]: e.target.value })} />
                </Field>
              ))}
            </div>
          </Card>
        )}
        <Card title="Routes">
          <div className="form-grid">
            <Field label="Replace host suffix" hint="e.g. .apps.homelab.bentech.work">
              <input value={routeFrom} onChange={(e) => setRouteFrom(e.target.value)} />
            </Field>
            <Field label="With" hint="e.g. .apps.drhomelab.bentech.work">
              <input value={routeTo} onChange={(e) => setRouteTo(e.target.value)} />
            </Field>
          </div>
        </Card>
        <label className="check">
          <input type="checkbox" checked={keepUid} onChange={(e) => setKeepUid(e.target.checked)} />
          <span>
            Keep the original UID range
            <div className="muted small">Restored files and pods then run as the same user IDs as before. Recommended.</div>
          </span>
        </label>
        <label className="check">
          <input type="checkbox" checked={includeData} onChange={(e) => setIncludeData(e.target.checked)} />
          Restore persistent volume data
        </label>
      </div>
    </Modal>
  );
}
