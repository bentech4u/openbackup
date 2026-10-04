import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Plus, XCircle } from "lucide-react";
import { del, get, post, type VCenter } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Empty, Field, Loading, Modal, PageHeader, errorText } from "../components/ui";
import { bytes } from "../format";

export interface Cluster {
  id: number;
  name: string;
  api_url: string;
  vcenter_id: number | null;
  has_restore_token: boolean;
  ca_subjects: string[];
  created_at: string;
}

export interface NamespaceInfo {
  name: string;
  system: boolean;
  phase: string;
  pvc_bytes: number;
  vms: number;
  pvcs: { name: string; phase: string; size: number; storage_class: string; volume_mode: string; driver: string; data_supported: boolean }[];
}

interface Probe {
  version: string;
  user: string;
  kubevirt: boolean;
  openshift: boolean;
  permissions: Record<string, boolean>;
}

const PERM_LABELS: Record<string, string> = {
  list_namespaces: "List namespaces",
  list_pvcs: "List volume claims",
  list_pvs: "List persistent volumes",
  list_deployments: "List workloads",
  read_secrets: "Read Secrets",
  create_namespaces: "Create namespaces",
  create_pods: "Create pods",
  freeze_vms: "Freeze VMs (guest agent)",
};

function Perms({ p, title }: { p: Probe; title: string }) {
  return (
    <div className="stack" style={{ gap: 6 }}>
      <strong>{title}</strong>
      <div className="muted small">
        {p.user} · Kubernetes {p.version}
        {p.kubevirt ? " · OpenShift Virtualization" : ""}
      </div>
      <div className="form-grid" style={{ gap: 4 }}>
        {Object.entries(p.permissions).map(([k, v]) => (
          <span key={k} className="row gap-s small">
            {v ? <CheckCircle2 size={14} color="var(--good)" /> : <XCircle size={14} color="var(--muted)" />}
            {PERM_LABELS[k] ?? k}
          </span>
        ))}
      </div>
    </div>
  );
}

export default function Clusters() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const clusters = useQuery({ queryKey: ["clusters"], queryFn: () => get<Cluster[]>("/api/clusters") });
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Cluster | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");
  const current = selected ?? clusters.data?.[0]?.id ?? null;

  return (
    <>
      <PageHeader
        title="OpenShift clusters"
        subtitle="Clusters whose namespaces and persistent volumes OpenBackup protects"
        actions={
          can("admin") && (
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus size={16} /> Add cluster
            </Button>
          )
        }
      />
      <Alert>{error}</Alert>
      <Card pad={false}>
        {clusters.isLoading ? (
          <Loading />
        ) : !clusters.data?.length ? (
          <Empty>
            No clusters yet. Apply <code>deploy/openshift/backup-serviceaccount.yaml</code> on a cluster, then add it here.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>API</th>
                  <th>Trusted CA</th>
                  <th>Restore credential</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {clusters.data.map((c) => (
                  <tr key={c.id} className={`clickable ${current === c.id ? "selected" : ""}`} onClick={() => setSelected(c.id)}>
                    <td>
                      <strong>{c.name}</strong>
                    </td>
                    <td className="mono small">{c.api_url}</td>
                    <td className="small">{c.ca_subjects.join(", ")}</td>
                    <td>{c.has_restore_token ? <Badge tone="info">stored</Badge> : <Badge>asked per restore</Badge>}</td>
                    <td className="right">
                      {can("admin") && (
                        <Button variant="ghost" onClick={(e) => { e.stopPropagation(); setRemoving(c); }}>
                          Remove
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {current && <Namespaces clusterId={current} />}
      {adding && (
        <AddCluster
          onClose={() => setAdding(false)}
          onDone={() => {
            qc.invalidateQueries({ queryKey: ["clusters"] });
          }}
        />
      )}
      {removing && (
        <Confirm
          title={`Remove ${removing.name}?`}
          message="OpenBackup forgets this cluster and its tokens. Restore points are kept. Jobs that use it must be deleted first."
          confirmLabel="Remove"
          danger
          onClose={() => setRemoving(null)}
          onConfirm={async () => {
            try {
              await del(`/api/clusters/${removing.id}`);
              qc.invalidateQueries({ queryKey: ["clusters"] });
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

function Namespaces({ clusterId }: { clusterId: number }) {
  const [system, setSystem] = useState(false);
  const ns = useQuery({
    queryKey: ["namespaces", clusterId, system],
    queryFn: () => get<NamespaceInfo[]>(`/api/clusters/${clusterId}/namespaces?include_system=${system}`),
  });
  return (
    <Card
      title="Namespaces"
      actions={
        <label className="check small">
          <input type="checkbox" checked={system} onChange={(e) => setSystem(e.target.checked)} />
          Show system namespaces
        </label>
      }
      pad={false}
    >
      {ns.error && <Alert>{errorText(ns.error)}</Alert>}
      {ns.isLoading ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Namespace</th>
                <th>Volumes</th>
                <th>Volume data</th>
                <th>VMs</th>
              </tr>
            </thead>
            <tbody>
              {ns.data?.map((n) => {
                const unsupported = n.pvcs.filter((p) => !p.data_supported);
                return (
                  <tr key={n.name}>
                    <td>
                      {n.name} {n.system && <Badge>system</Badge>}
                    </td>
                    <td className="small">
                      {n.pvcs.length ? `${n.pvcs.length} · ${bytes(n.pvc_bytes)}` : <span className="muted">none</span>}
                    </td>
                    <td className="small">
                      {!n.pvcs.length ? (
                        ""
                      ) : unsupported.length ? (
                        <span title={unsupported.map((p) => `${p.name}: ${p.driver || "no CSI driver"}`).join("\n")}>
                          <Badge tone="warn">
                            {unsupported.length} not vSphere CSI
                          </Badge>
                        </span>
                      ) : (
                        <Badge tone="good">all backed up</Badge>
                      )}
                    </td>
                    <td>{n.vms || ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

interface ChainCert {
  pem: string;
  subject: string;
  issuer: string;
  sha256: string;
  is_ca: boolean;
}

function AddCluster({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const vcs = useQuery({ queryKey: ["vcenters"], queryFn: () => get<VCenter[]>("/api/vcenters") });
  const [name, setName] = useState("");
  const [url, setUrl] = useState("https://api.");
  const [chain, setChain] = useState<ChainCert[] | null>(null);
  const [caPem, setCaPem] = useState("");
  const [pasteCa, setPasteCa] = useState(false);
  const [backupToken, setBackupToken] = useState("");
  const [restoreToken, setRestoreToken] = useState("");
  const [vcenterId, setVcenterId] = useState<number | "">("");
  const [result, setResult] = useState<{ backup: Probe; restore: Probe | null } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function fetchCa() {
    setBusy(true);
    setError("");
    try {
      const c = await post<ChainCert[]>("/api/clusters/fetch-ca", { api_url: url.trim() });
      setChain(c);
      const ca = [...c].reverse().find((x) => x.is_ca) ?? c[c.length - 1];
      setCaPem(ca.pem);
      if (!name) setName(url.replace(/^https:\/\/api\./, "").split(/[.:]/)[0]);
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
      const r = await post<{ backup: Probe; restore: Probe | null }>("/api/clusters", {
        name: name.trim(),
        api_url: url.trim(),
        ca_pem: caPem,
        backup_token: backupToken.trim(),
        restore_token: restoreToken.trim() || null,
        vcenter_id: vcenterId === "" ? null : vcenterId,
      });
      setResult(r);
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (result) {
    return (
      <Modal title="Cluster added" onClose={onClose} footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
        <div className="stack">
          <Perms p={result.backup} title="Backup token" />
          {result.restore && <Perms p={result.restore} title="Restore token" />}
          {result.backup.permissions.read_secrets && (
            <Alert tone="warn">The backup token can read Secrets. OpenBackup never backs them up, but a cluster-reader token is enough.</Alert>
          )}
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      title="Add OpenShift cluster"
      onClose={onClose}
      wide
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} busy={busy} disabled={!caPem || !backupToken || !name}>
            Test and save
          </Button>
        </>
      }
    >
      <div className="stack">
        <Alert>{error}</Alert>
        <div className="form-grid">
          <Field label="API URL" hint="From `oc whoami --show-server`">
            <input className="mono" value={url} onChange={(e) => { setUrl(e.target.value); setChain(null); setCaPem(""); }} />
          </Field>
          <Field label="Name">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="homelab" />
          </Field>
        </div>
        <Card title="Trusted certificate authority">
          <div className="stack">
            <div className="muted small">
              OpenBackup only trusts this CA for the cluster's API, never the system trust store. Fetch the chain and confirm the
              fingerprint, or paste the CA from your kubeconfig (<code>certificate-authority-data</code>, base64-decoded).
            </div>
            <div className="row gap-s">
              <Button onClick={fetchCa} busy={busy && !chain} disabled={!/^https:\/\/.+/.test(url)}>
                Fetch certificate chain
              </Button>
              <Button variant="ghost" onClick={() => setPasteCa(!pasteCa)}>
                {pasteCa ? "Use fetched chain" : "Paste CA instead"}
              </Button>
            </div>
            {!pasteCa && chain && (
              <div className="stack" style={{ gap: 8 }}>
                {chain.map((c) => (
                  <label key={c.sha256} className="check">
                    <input type="radio" checked={caPem === c.pem} onChange={() => setCaPem(c.pem)} />
                    <span>
                      <div>
                        {c.subject} {c.is_ca && <Badge tone="info">CA</Badge>}
                      </div>
                      <div className="muted small">issued by {c.issuer}</div>
                      <div className="mono small">SHA-256 {c.sha256}</div>
                    </span>
                  </label>
                ))}
              </div>
            )}
            {pasteCa && (
              <textarea className="mono small" rows={6} value={caPem} onChange={(e) => setCaPem(e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" />
            )}
          </div>
        </Card>
        <Field label="Backup token" hint="From deploy/openshift/backup-serviceaccount.yaml (read-only, cluster-reader)">
          <textarea className="mono small" rows={3} value={backupToken} onChange={(e) => setBackupToken(e.target.value)} />
        </Field>
        <Field label="Restore token (optional)" hint="From restore-serviceaccount.yaml. Leave empty to paste it for each restore instead of storing it.">
          <textarea className="mono small" rows={2} value={restoreToken} onChange={(e) => setRestoreToken(e.target.value)} />
        </Field>
        <Field label="vCenter for persistent volume data" hint="The vCenter whose vSphere CSI volumes this cluster uses">
          <select value={vcenterId} onChange={(e) => setVcenterId(e.target.value ? Number(e.target.value) : "")}>
            <option value="">None (resources only)</option>
            {vcs.data?.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
              </option>
            ))}
          </select>
        </Field>
      </div>
    </Modal>
  );
}

