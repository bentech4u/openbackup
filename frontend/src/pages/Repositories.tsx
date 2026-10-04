import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Lock, Plus } from "lucide-react";
import { del, get, post, type Repository, type Task } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Empty, Field, Loading, Modal, PageHeader, errorText } from "../components/ui";
import { bytes } from "../format";
import { capacityMeter } from "./Dashboard";

interface Stats {
  packs: number;
  chunks: number;
  stored_bytes: number;
  points: number;
}

export default function Repositories() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const nav = useNavigate();
  const repos = useQuery({ queryKey: ["repositories"], queryFn: () => get<Repository[]>("/api/repositories") });
  const [adding, setAdding] = useState(false);
  const [detaching, setDetaching] = useState<Repository | null>(null);
  const [error, setError] = useState("");

  async function task(url: string) {
    setError("");
    try {
      const t = await post<Task>(url);
      nav(`/tasks/${t.id}`);
    } catch (e) {
      setError(errorText(e));
    }
  }

  return (
    <>
      <PageHeader
        title="Repositories"
        subtitle="Where backups are stored. NFS shares are mounted with hard mounts and kept mounted."
        actions={
          can("admin") && (
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus size={16} /> Add repository
            </Button>
          )
        }
      />
      <Alert>{error}</Alert>
      {repos.isLoading ? (
        <Loading />
      ) : !repos.data?.length ? (
        <Card>
          <Empty>No repositories yet. Add an NFS share to store backups.</Empty>
        </Card>
      ) : (
        <div className="grid grid-2">
          {repos.data.map((r) => (
            <Card
              key={r.id}
              title={
                <span className="row gap-s">
                  {r.name}
                  <Badge tone="info">{r.kind.toUpperCase()}</Badge>
                  {r.encrypted && (
                    <Badge tone="good">
                      <Lock size={11} /> encrypted
                    </Badge>
                  )}
                </span>
              }
              actions={
                <>
                  {can("operator") && <Button onClick={() => task(`/api/repositories/${r.id}/verify`)}>Verify all</Button>}
                  {can("operator") && <Button onClick={() => task(`/api/repositories/${r.id}/maintenance`)}>Clean up</Button>}
                  {can("admin") && (
                    <Button variant="ghost" onClick={() => setDetaching(r)}>
                      Detach
                    </Button>
                  )}
                </>
              }
            >
              <RepoBody repo={r} />
            </Card>
          ))}
        </div>
      )}
      {adding && (
        <AddRepository
          onClose={() => setAdding(false)}
          onDone={() => {
            setAdding(false);
            qc.invalidateQueries({ queryKey: ["repositories"] });
          }}
        />
      )}
      {detaching && (
        <Confirm
          title={`Detach ${detaching.name}?`}
          message="OpenBackup stops using this repository and forgets its restore points. The backup data on the share is NOT deleted and can be imported again later (with its passphrase, if encrypted)."
          confirmLabel="Detach"
          danger
          onClose={() => setDetaching(null)}
          onConfirm={async () => {
            try {
              await del(`/api/repositories/${detaching.id}`);
              qc.invalidateQueries({ queryKey: ["repositories"] });
            } catch (e) {
              setError(errorText(e));
            }
            setDetaching(null);
          }}
        />
      )}
    </>
  );
}

function RepoBody({ repo }: { repo: Repository }) {
  const stats = useQuery({
    queryKey: ["repo-stats", repo.id],
    queryFn: () => get<Stats & { capacity_bytes: number; free_bytes: number }>(`/api/repositories/${repo.id}/stats`),
    staleTime: 30_000,
  });
  const merged = stats.data ? { ...repo, capacity_bytes: stats.data.capacity_bytes, free_bytes: stats.data.free_bytes } : repo;
  return (
    <div className="stack">
      {repo.shares_datastore && (
        <Alert tone="warn">
          This repository is inside the same NFS export as datastore <strong>{repo.shares_datastore}</strong>. If that volume or NAS
          fails, the VMs and their backups are lost together. Use a separate share, ideally on another NAS, for real protection.
        </Alert>
      )}
      <dl className="kv">
        <dt>Location</dt>
        <dd className="mono small">
          {repo.kind === "nfs" ? `${repo.nfs_server}:${repo.nfs_export}${repo.path ? ` / ${repo.path}` : ""}` : repo.path}
        </dd>
        {repo.kind === "nfs" && (
          <>
            <dt>Options</dt>
            <dd className="mono small">{repo.nfs_options}</dd>
          </>
        )}
        <dt>Restore points</dt>
        <dd>{stats.data?.points ?? "…"}</dd>
        <dt>Stored data</dt>
        <dd>
          {stats.data ? `${bytes(stats.data.stored_bytes)} in ${stats.data.packs} pack files` : "…"}
        </dd>
      </dl>
      {stats.error && <Alert>{errorText(stats.error)}</Alert>}
      {capacityMeter(merged)}
    </div>
  );
}

interface TestResult {
  ok: boolean;
  message: string;
  capacity_bytes?: number;
  free_bytes?: number;
  write_mib_s?: number;
  existing_repository?: boolean;
}

function AddRepository({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [kind, setKind] = useState<"nfs" | "local">("nfs");
  const [name, setName] = useState("");
  const [server, setServer] = useState("");
  const [exportPath, setExportPath] = useState("");
  const [sub, setSub] = useState("openbackup");
  const [options, setOptions] = useState("nfsvers=4.2,hard");
  const [path, setPath] = useState("/backup/openbackup");
  const [encrypt, setEncrypt] = useState(true);
  const [pass, setPass] = useState("");
  const [pass2, setPass2] = useState("");
  const [test, setTest] = useState<TestResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const location = kind === "nfs"
    ? { kind, nfs_server: server.trim(), nfs_export: exportPath.trim(), nfs_options: options.trim(), path: sub.trim() }
    : { kind, path: path.trim() };
  const importing = !!test?.existing_repository;

  async function runTest() {
    setBusy(true);
    setError("");
    setTest(null);
    try {
      setTest(await post<TestResult>("/api/repositories/test", location));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setError("");
    if (!importing && encrypt && pass.length < 12) {
      setError("Enter a passphrase of at least 12 characters, or untick Encrypt backups");
      return;
    }
    if (!importing && encrypt && pass !== pass2) {
      setError("The passphrases do not match");
      return;
    }
    setBusy(true);
    try {
      await post("/api/repositories", {
        ...location,
        name: name.trim(),
        mode: importing ? "import" : "create",
        passphrase: importing ? pass || null : encrypt ? pass : null,
      });
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  const reset = () => setTest(null);

  return (
    <Modal
      title="Add repository"
      onClose={onClose}
      wide
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button onClick={runTest} busy={busy && !test}>
            Test connection
          </Button>
          <Button variant="primary" onClick={save} busy={busy && !!test} disabled={!test?.ok || !name.trim()}>
            {importing ? "Import repository" : "Create repository"}
          </Button>
        </>
      }
    >
      <div className="stack">
        <div className="segmented">
          <button className={kind === "nfs" ? "on" : ""} onClick={() => { setKind("nfs"); reset(); }}>
            NFS share
          </button>
          <button className={kind === "local" ? "on" : ""} onClick={() => { setKind("local"); reset(); }}>
            Local directory
          </button>
        </div>
        <Alert>{error}</Alert>
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. NAS-01" />
        </Field>
        {kind === "nfs" ? (
          <div className="form-grid">
            <Field label="NFS server">
              <input value={server} onChange={(e) => { setServer(e.target.value); reset(); }} placeholder="10.0.0.5" />
            </Field>
            <Field label="Export path">
              <input value={exportPath} onChange={(e) => { setExportPath(e.target.value); reset(); }} placeholder="/volume1/backup" />
            </Field>
            <Field label="Subdirectory" hint="Inside the export; created if missing">
              <input value={sub} onChange={(e) => { setSub(e.target.value); reset(); }} />
            </Field>
            <Field label="Mount options" hint="Soft mounts are refused: they can silently lose data">
              <input className="mono" value={options} onChange={(e) => { setOptions(e.target.value); reset(); }} />
            </Field>
          </div>
        ) : (
          <Field label="Directory" hint="A dedicated directory on this server, ideally its own filesystem">
            <input value={path} onChange={(e) => { setPath(e.target.value); reset(); }} />
          </Field>
        )}
        {test && (
          <Alert tone={test.ok ? "good" : "bad"}>
            {test.ok
              ? `Writable. ${bytes(test.free_bytes)} free of ${bytes(test.capacity_bytes)}, write test ${test.write_mib_s} MiB/s.` +
                (test.existing_repository ? " An existing OpenBackup repository was found here and will be imported." : "")
              : test.message}
          </Alert>
        )}
        {test?.ok &&
          (importing ? (
            <Field label="Repository passphrase" hint="Only needed if the repository is encrypted">
              <input type="password" value={pass} onChange={(e) => setPass(e.target.value)} autoComplete="off" />
            </Field>
          ) : (
            <>
              <label className="check">
                <input type="checkbox" checked={encrypt} onChange={(e) => setEncrypt(e.target.checked)} />
                <span>
                  Encrypt backups (AES-256-GCM)
                  <div className="muted small">Protects the data on the share if the NAS is compromised.</div>
                </span>
              </label>
              {encrypt && (
                <>
                  <Alert tone="warn">
                    Store this passphrase somewhere safe, away from this server. Without it, nobody — including you — can restore from
                    this repository after a server rebuild.
                  </Alert>
                  <div className="form-grid">
                    <Field label="Passphrase" hint="At least 12 characters">
                      <input type="password" value={pass} onChange={(e) => setPass(e.target.value)} autoComplete="new-password" />
                    </Field>
                    <Field label="Repeat passphrase">
                      <input type="password" value={pass2} onChange={(e) => setPass2(e.target.value)} autoComplete="new-password" />
                    </Field>
                  </div>
                </>
              )}
            </>
          ))}
      </div>
    </Modal>
  );
}
