import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, FolderOpen, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import { del, get, post, type Placement, type PointDetail, type RestorePoint, type Task, type VCenter } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Empty, Field, Loading, Modal, PageHeader, errorText } from "../components/ui";
import { bytes, dateTime, relative } from "../format";

interface ProtectedVm {
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
                      <td>{v.vm_name}</td>
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
