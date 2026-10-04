import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Download, File, Folder, HardDrive, Link2, Loader2, RotateCcw } from "lucide-react";
import { api, get, post, type PointDetail, type Task, type VCenter, type VmSummary } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Empty, Field, Modal, PageHeader, errorText } from "../components/ui";
import { bytes, dateTime } from "../format";

interface Volume {
  id: string;
  device: string;
  fstype: string;
  guest_path: string | null;
  label: string;
  size: number;
  mounted: boolean;
  error: string;
}

interface Entry {
  name: string;
  type: "file" | "dir" | "link" | "other";
  size: number;
  mode: number;
  mtime: number;
}

interface Browse {
  session_id: string;
  volumes: Volume[];
  os: { type: string; distro: string; name: string; hostname: string } | null;
}

function volumeTitle(v: Volume) {
  if (v.guest_path) return v.guest_path;
  return v.label || v.device.replace("/dev/", "");
}

// The path as the guest sees it, for display.
function guestPath(v: Volume | undefined, path: string): string {
  if (!v) return path;
  const rel = path.slice(v.id.length + 1);
  if (!v.guest_path) return `${volumeTitle(v)}:${rel || "/"}`;
  if (/^[A-Z]:$/.test(v.guest_path)) return v.guest_path + (rel || "/").replaceAll("/", "\\");
  return (v.guest_path === "/" ? "" : v.guest_path) + (rel || "/");
}

export default function FileBrowser() {
  const pointId = useParams().id!;
  const nav = useNavigate();
  const { can } = useAuth();
  const point = useQuery({ queryKey: ["point", pointId], queryFn: () => get<PointDetail>(`/api/points/${pointId}`) });
  const [browse, setBrowse] = useState<Browse | null>(null);
  const [opening, setOpening] = useState(true);
  const [error, setError] = useState("");
  const [cwd, setCwd] = useState("");
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [listing, setListing] = useState(false);
  const [selected, setSelected] = useState<Map<string, Entry>>(new Map());
  const [restoring, setRestoring] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const sid = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const t0 = Date.now();
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000);
    post<Browse>(`/api/points/${pointId}/browse`)
      .then((b) => {
        if (cancelled) {
          api("DELETE", `/api/flr/${b.session_id}`).catch(() => {});
          return;
        }
        sid.current = b.session_id;
        setBrowse(b);
        const first = b.volumes.find((v) => v.mounted);
        if (first) setCwd(`/${first.id}`);
      })
      .catch((e) => setError(errorText(e)))
      .finally(() => {
        clearInterval(timer);
        setOpening(false);
      });
    return () => {
      cancelled = true;
      clearInterval(timer);
      if (sid.current) api("DELETE", `/api/flr/${sid.current}`).catch(() => {});
    };
  }, [pointId]);

  useEffect(() => {
    if (!browse || !cwd) return;
    setListing(true);
    setError("");
    get<Entry[]>(`/api/flr/${browse.session_id}/ls?path=${encodeURIComponent(cwd)}`)
      .then(setEntries)
      .catch((e) => setError(errorText(e)))
      .finally(() => setListing(false));
  }, [browse, cwd]);

  const vol = browse?.volumes.find((v) => cwd === `/${v.id}` || cwd.startsWith(`/${v.id}/`));
  const crumbs = useMemo(() => {
    if (!vol) return [];
    const rel = cwd.slice(vol.id.length + 1).split("/").filter(Boolean);
    return [{ name: volumeTitle(vol), path: `/${vol.id}` }, ...rel.map((p, i) => ({ name: p, path: `/${vol.id}/${rel.slice(0, i + 1).join("/")}` }))];
  }, [cwd, vol]);

  const toggle = (path: string, e: Entry) =>
    setSelected((prev) => {
      const n = new Map(prev);
      if (n.has(path)) n.delete(path);
      else n.set(path, e);
      return n;
    });

  const sorted = (entries ?? []).slice().sort((a, b) => (a.type === "dir") === (b.type === "dir") ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1);
  const p = point.data;

  return (
    <>
      <PageHeader
        title={p ? `Files in ${p.vm_name}` : "Files"}
        subtitle={
          <span className="row gap-s">
            <Button variant="ghost" onClick={() => nav(`/points/${pointId}`)}>
              <ArrowLeft size={14} /> Restore point
            </Button>
            {p && <span>Backup of {dateTime(p.created_at)}</span>}
            {browse?.os && <Badge tone="info">{browse.os.name || browse.os.distro}</Badge>}
            <Badge>read-only</Badge>
          </span>
        }
        actions={
          <>
            <span className="muted">{selected.size} selected</span>
            <Button
              disabled={selected.size !== 1}
              onClick={() => {
                const path = [...selected.keys()][0];
                window.location.href = `/api/flr/${browse!.session_id}/download?path=${encodeURIComponent(path)}`;
              }}
            >
              <Download size={15} /> Download
            </Button>
            <Button variant="primary" disabled={selected.size === 0 || !can("operator")} onClick={() => setRestoring(true)}>
              <RotateCcw size={15} /> Restore to VM…
            </Button>
          </>
        }
      />
      <Alert>{error}</Alert>
      {opening ? (
        <Card>
          <div className="loading">
            <Loader2 className="spin" size={18} /> Opening the backup ({elapsed}s). This starts a small helper VM that reads the guest
            filesystems and can take up to a minute.
          </div>
        </Card>
      ) : browse ? (
        <div className="grid" style={{ gridTemplateColumns: "240px 1fr", alignItems: "start" }}>
          <Card title="Volumes" pad={false}>
            <div className="stack" style={{ gap: 0 }}>
              {browse.volumes.map((v) => (
                <button
                  key={v.id}
                  className={`vol ${vol?.id === v.id ? "on" : ""}`}
                  disabled={!v.mounted}
                  title={v.error || v.device}
                  onClick={() => setCwd(`/${v.id}`)}
                >
                  <HardDrive size={15} />
                  <span className="grow">
                    <div>{volumeTitle(v)}</div>
                    <div className="muted small">
                      {v.fstype} · {bytes(v.size)}
                      {v.label && v.guest_path ? ` · ${v.label}` : ""}
                    </div>
                    {!v.mounted && <div className="small" style={{ color: "var(--bad)" }}>cannot be read</div>}
                  </span>
                </button>
              ))}
            </div>
          </Card>
          <Card
            pad={false}
            title={
              <span className="crumbs">
                {crumbs.map((c, i) => (
                  <span key={c.path}>
                    {i > 0 && <span className="muted"> / </span>}
                    <a onClick={() => setCwd(c.path)}>{c.name}</a>
                  </span>
                ))}
              </span>
            }
            actions={listing && <Loader2 size={15} className="spin" />}
          >
            {!sorted.length && !listing ? (
              <Empty>This folder is empty.</Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th style={{ width: 32 }} />
                      <th>Name</th>
                      <th className="right">Size</th>
                      <th>Modified</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cwd !== `/${vol?.id}` && (
                      <tr className="clickable" onClick={() => setCwd(cwd.slice(0, cwd.lastIndexOf("/")))}>
                        <td />
                        <td colSpan={3}>
                          <span className="row gap-s">
                            <Folder size={15} /> ..
                          </span>
                        </td>
                      </tr>
                    )}
                    {sorted.map((e) => {
                      const path = `${cwd}/${e.name}`;
                      const selectable = e.type === "file" || e.type === "dir";
                      return (
                        <tr key={e.name} className={selected.has(path) ? "selected" : ""}>
                          <td>{selectable && <input type="checkbox" checked={selected.has(path)} onChange={() => toggle(path, e)} />}</td>
                          <td>
                            {e.type === "dir" ? (
                              <a className="row gap-s" onClick={() => setCwd(path)}>
                                <Folder size={15} /> {e.name}
                              </a>
                            ) : (
                              <span className="row gap-s" onClick={() => selectable && toggle(path, e)} style={{ cursor: "pointer" }}>
                                {e.type === "link" ? <Link2 size={15} /> : <File size={15} />} {e.name}
                              </span>
                            )}
                          </td>
                          <td className="right nowrap">{e.type === "file" ? bytes(e.size) : ""}</td>
                          <td className="nowrap muted">{dateTime(new Date(e.mtime * 1000).toISOString())}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      ) : null}
      {restoring && browse && p && (
        <RestoreFiles
          point={p}
          volumes={browse.volumes}
          items={[...selected.keys()]}
          onClose={() => setRestoring(false)}
          onStarted={(t) => nav(`/tasks/${t.id}`)}
        />
      )}
      <p className="muted small">
        <Link to={`/points/${pointId}`}>Back to the restore point</Link>
      </p>
    </>
  );
}

function RestoreFiles({ point, volumes, items, onClose, onStarted }: {
  point: PointDetail;
  volumes: Volume[];
  items: string[];
  onClose: () => void;
  onStarted: (t: Task) => void;
}) {
  const { can } = useAuth();
  const vcs = useQuery({ queryKey: ["vcenters"], queryFn: () => get<VCenter[]>("/api/vcenters") });
  const [vcId, setVcId] = useState<number | undefined>(undefined);
  const vcenterId = vcId ?? vcs.data?.find((v) => v.host === point.vcenter)?.id ?? vcs.data?.[0]?.id;
  const vms = useQuery({
    queryKey: ["vms", vcenterId],
    queryFn: () => get<VmSummary[]>(`/api/vcenters/${vcenterId}/vms`),
    enabled: !!vcenterId,
  });
  const original = vms.data?.find((v) => v.instance_uuid === point.vm_uuid);
  const [vmMoref, setVmMoref] = useState("");
  const target = vmMoref || original?.moref || "";
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [check, setCheck] = useState<{ ok: boolean; message?: string; os_name?: string; hostname?: string } | null>(null);
  const [conflict, setConflict] = useState<"rename" | "overwrite" | "skip">("rename");
  const [where, setWhere] = useState<"original" | "folder">("original");
  const [folder, setFolder] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const volOf = (path: string) => volumes.find((v) => path === `/${v.id}` || path.startsWith(`/${v.id}/`));
  const unmapped = items.some((i) => !volOf(i)?.guest_path);
  const otherVm = !!original && target !== original.moref;
  const effectiveWhere = unmapped ? "folder" : where;

  async function runCheck() {
    setBusy(true);
    setCheck(null);
    try {
      setCheck(await post(`/api/vcenters/${vcenterId}/vms/${target}/guest-check`, { username: user, password }));
    } catch (e) {
      setCheck({ ok: false, message: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    setError("");
    if (!target) return setError("Choose the VM to restore into");
    if (!user || !password) return setError("Enter the guest OS credentials");
    if (effectiveWhere === "folder" && !folder.trim()) return setError("Enter the target folder in the guest");
    setBusy(true);
    try {
      const t = await post<Task>(`/api/points/${point.id}/restore`, {
        mode: "files",
        vcenter_id: vcenterId,
        files: {
          items,
          vm_moref: target,
          guest_user: user,
          guest_password: password,
          conflict,
          target_dir: effectiveWhere === "folder" ? folder.trim() : null,
        },
      });
      onStarted(t);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Restore ${items.length} item${items.length > 1 ? "s" : ""} into a VM`}
      onClose={onClose}
      wide
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant={conflict === "overwrite" ? "danger" : "primary"} onClick={submit} busy={busy}>
            {conflict === "overwrite" ? "Restore and overwrite" : "Restore"}
          </Button>
        </>
      }
    >
      <div className="stack">
        <div className="muted small">
          {items.slice(0, 6).map((i) => (
            <div key={i} className="mono">
              {guestPath(volOf(i), i)}
            </div>
          ))}
          {items.length > 6 && <div>…and {items.length - 6} more</div>}
        </div>
        <Alert>{error}</Alert>
        <div className="form-grid">
          <Field label="vCenter">
            <select value={vcenterId ?? ""} onChange={(e) => { setVcId(Number(e.target.value)); setVmMoref(""); setCheck(null); }}>
              {vcs.data?.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Target VM" hint={original ? (otherVm ? "A different VM than the one backed up" : "The original VM") : "The original VM was not found"}>
            <select value={target} onChange={(e) => { setVmMoref(e.target.value); setCheck(null); }}>
              <option value="">Choose…</option>
              {vms.data
                ?.filter((v) => !v.is_template)
                .map((v) => (
                  <option key={v.moref} value={v.moref}>
                    {v.name}
                    {v.instance_uuid === point.vm_uuid ? " (original)" : ""}
                    {v.power_state !== "poweredOn" ? " — powered off" : ""}
                  </option>
                ))}
            </select>
          </Field>
        </div>
        <Card title="Guest OS credentials">
          <div className="stack">
            <div className="muted small">
              Files are written by VMware Tools as this guest user (e.g. <code>Administrator</code> or <code>root</code>). The password is
              used for this restore only and is not stored.
            </div>
            <div className="form-grid">
              <Field label="Username">
                <input value={user} onChange={(e) => { setUser(e.target.value); setCheck(null); }} autoComplete="off" />
              </Field>
              <Field label="Password">
                <input type="password" value={password} onChange={(e) => { setPassword(e.target.value); setCheck(null); }} autoComplete="new-password" />
              </Field>
            </div>
            <div className="row gap-s">
              <Button onClick={runCheck} busy={busy && !check} disabled={!target || !user || !password}>
                Check credentials
              </Button>
              {check && (
                <span style={{ color: check.ok ? "var(--good)" : "var(--bad)" }}>
                  {check.ok ? `OK: ${check.hostname || ""} ${check.os_name ? `(${check.os_name})` : ""}` : check.message}
                </span>
              )}
            </div>
          </div>
        </Card>
        <Field label="If a file or folder already exists">
          <div className="stack" style={{ gap: 8 }}>
            <label className="check">
              <input type="radio" checked={conflict === "rename"} onChange={() => setConflict("rename")} />
              <span>
                Keep it, restore beside it as <code>name_restored_&lt;date&gt;</code>
              </span>
            </label>
            <label className="check" style={{ opacity: can("admin") ? 1 : 0.5 }}>
              <input type="radio" disabled={!can("admin")} checked={conflict === "overwrite"} onChange={() => setConflict("overwrite")} />
              <span>
                Overwrite it with the backup copy {!can("admin") && <span className="muted small">(admins only)</span>}
              </span>
            </label>
            <label className="check">
              <input type="radio" checked={conflict === "skip"} onChange={() => setConflict("skip")} />
              <span>Skip it</span>
            </label>
          </div>
        </Field>
        <Field label="Restore to">
          <div className="stack" style={{ gap: 8 }}>
            <label className="check" style={{ opacity: unmapped ? 0.5 : 1 }}>
              <input type="radio" disabled={unmapped} checked={effectiveWhere === "original"} onChange={() => setWhere("original")} />
              <span>
                The original location {unmapped && <span className="muted small">(unknown for some items: their volume is not mapped to a drive or mount point)</span>}
              </span>
            </label>
            <label className="check">
              <input type="radio" checked={effectiveWhere === "folder"} onChange={() => setWhere("folder")} />
              <span>Another folder in the guest</span>
            </label>
            {effectiveWhere === "folder" && (
              <input className="mono" placeholder={check?.os_name?.toLowerCase().includes("windows") ? "C:\\Restore" : "/tmp/restore"} value={folder} onChange={(e) => setFolder(e.target.value)} />
            )}
          </div>
        </Field>
        {conflict === "overwrite" && (
          <Alert tone="warn">Existing files are replaced with the versions from {dateTime(point.created_at)}. This cannot be undone.</Alert>
        )}
      </div>
    </Modal>
  );
}
