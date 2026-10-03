import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Play, Plus } from "lucide-react";
import { del, get, post, put, type Job, type Repository, type Task, type VCenter, type VmSummary } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Empty, Field, Loading, Modal, PageHeader, StateBadge, errorText } from "../components/ui";
import { bytes, dateTime, describeCron, relative } from "../format";

export default function Jobs() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const nav = useNavigate();
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => get<Job[]>("/api/jobs"), refetchInterval: 5000 });
  const repos = useQuery({ queryKey: ["repositories"], queryFn: () => get<Repository[]>("/api/repositories") });
  const vcs = useQuery({ queryKey: ["vcenters"], queryFn: () => get<VCenter[]>("/api/vcenters") });
  const [editing, setEditing] = useState<Job | "new" | null>(null);
  const [deleting, setDeleting] = useState<Job | null>(null);
  const [error, setError] = useState("");

  const refresh = () => qc.invalidateQueries({ queryKey: ["jobs"] });
  const run = useMutation({
    mutationFn: ({ job, full }: { job: Job; full: boolean }) => post<Task>(`/api/jobs/${job.id}/run`, { active_full: full }),
    onSuccess: (t) => nav(`/tasks/${t.id}`),
    onError: (e) => setError(errorText(e)),
  });
  const toggle = useMutation({
    mutationFn: (job: Job) => post(`/api/jobs/${job.id}/enabled`, { enabled: !job.enabled }),
    onSuccess: refresh,
    onError: (e) => setError(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: (job: Job) => del(`/api/jobs/${job.id}`),
    onSuccess: () => {
      setDeleting(null);
      refresh();
    },
    onError: (e) => setError(errorText(e)),
  });

  const repoName = (id: number) => repos.data?.find((r) => r.id === id)?.name ?? `#${id}`;
  const ready = (repos.data?.length ?? 0) > 0 && (vcs.data?.length ?? 0) > 0;

  return (
    <>
      <PageHeader
        title="Backup jobs"
        subtitle="Which VMs are backed up, where to, when, and for how long"
        actions={
          can("admin") && (
            <Button variant="primary" onClick={() => setEditing("new")} disabled={!ready}>
              <Plus size={16} /> New job
            </Button>
          )
        }
      />
      {!ready && !repos.isLoading && !vcs.isLoading && (
        <Alert tone="info">Add a vCenter and a repository first; a job needs both.</Alert>
      )}
      <Alert>{error}</Alert>
      <Card pad={false}>
        {jobs.isLoading ? (
          <Loading />
        ) : !jobs.data?.length ? (
          <Empty>No backup jobs yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Job</th>
                  <th>VMs</th>
                  <th>Repository</th>
                  <th>Schedule</th>
                  <th>Retention</th>
                  <th>Last result</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {jobs.data.map((j) => (
                  <tr key={j.id}>
                    <td>
                      <div className="row gap-s">
                        <strong>{j.name}</strong>
                        {!j.enabled && <Badge>disabled</Badge>}
                      </div>
                      {j.description && <div className="muted small">{j.description}</div>}
                    </td>
                    <td title={j.vms.map((v) => v.name).join(", ")}>{j.vms.length}</td>
                    <td>{repoName(j.repository_id)}</td>
                    <td>
                      <div>{describeCron(j.schedule_cron)}</div>
                      {j.enabled && j.next_run_at && <div className="muted small">next {relative(j.next_run_at)}</div>}
                    </td>
                    <td className="small">
                      {j.retention_points ? `${j.retention_points} points` : ""}
                      {j.retention_points && j.retention_days ? " or " : ""}
                      {j.retention_days ? `${j.retention_days} days` : ""}
                    </td>
                    <td>
                      <StateBadge state={j.last_state} />
                      {j.last_run_at && <div className="muted small">{dateTime(j.last_run_at)}</div>}
                    </td>
                    <td className="right">
                      <div className="row gap-s" style={{ justifyContent: "flex-end" }}>
                        {can("operator") && (
                          <>
                            <Button onClick={() => run.mutate({ job: j, full: false })} busy={run.isPending && run.variables?.job.id === j.id}>
                              <Play size={14} /> Run
                            </Button>
                            <select
                              aria-label="More actions"
                              value=""
                              style={{ width: 36, padding: "7px 4px" }}
                              onChange={(e) => {
                                const a = e.target.value;
                                if (a === "full") run.mutate({ job: j, full: true });
                                if (a === "toggle") toggle.mutate(j);
                                if (a === "edit") setEditing(j);
                                if (a === "delete") setDeleting(j);
                              }}
                            >
                              <option value="">⋯</option>
                              <option value="full">Run active full</option>
                              <option value="toggle">{j.enabled ? "Disable" : "Enable"}</option>
                              {can("admin") && <option value="edit">Edit…</option>}
                              {can("admin") && <option value="delete">Delete…</option>}
                            </select>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {editing && (
        <JobEditor
          job={editing === "new" ? null : editing}
          vcenters={vcs.data ?? []}
          repositories={repos.data ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
      {deleting && (
        <Confirm
          title={`Delete job ${deleting.name}?`}
          message="The job definition is removed. Its restore points stay in the repository and can still be restored."
          confirmLabel="Delete job"
          danger
          busy={remove.isPending}
          onConfirm={() => remove.mutate(deleting)}
          onClose={() => setDeleting(null)}
        />
      )}
    </>
  );
}

const SCHEDULES = [
  { id: "daily", label: "Daily" },
  { id: "weekdays", label: "Weekdays" },
  { id: "weekly", label: "Specific days" },
  { id: "custom", label: "Cron" },
  { id: "none", label: "Manual" },
] as const;
type ScheduleKind = (typeof SCHEDULES)[number]["id"];

function parseSchedule(cron: string | null): { kind: ScheduleKind; time: string; days: number[]; custom: string } {
  const base = { time: "22:00", days: [1, 3, 5], custom: cron ?? "0 22 * * *" };
  if (!cron) return { kind: "none", ...base };
  const p = cron.split(/\s+/);
  if (p.length === 5 && /^\d+$/.test(p[0]) && /^\d+$/.test(p[1]) && p[2] === "*" && p[3] === "*") {
    const time = `${p[1].padStart(2, "0")}:${p[0].padStart(2, "0")}`;
    if (p[4] === "*") return { ...base, kind: "daily", time };
    if (p[4] === "1-5") return { ...base, kind: "weekdays", time };
    if (/^[0-6](,[0-6])*$/.test(p[4])) return { ...base, kind: "weekly", time, days: p[4].split(",").map(Number) };
  }
  return { ...base, kind: "custom" };
}

function buildCron(kind: ScheduleKind, time: string, days: number[], custom: string): string | null {
  const [h, m] = time.split(":").map((x) => String(Number(x)));
  switch (kind) {
    case "daily":
      return `${m} ${h} * * *`;
    case "weekdays":
      return `${m} ${h} * * 1-5`;
    case "weekly":
      return `${m} ${h} * * ${[...days].sort().join(",") || "1"}`;
    case "custom":
      return custom.trim();
    default:
      return null;
  }
}

function JobEditor({ job, vcenters, repositories, onClose, onSaved }: {
  job: Job | null;
  vcenters: VCenter[];
  repositories: Repository[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const steps = ["General", "Virtual machines", "Storage", "Schedule", "Summary"];
  const [step, setStep] = useState(0);
  const [name, setName] = useState(job?.name ?? "");
  const [description, setDescription] = useState(job?.description ?? "");
  const [vcenterId, setVcenterId] = useState(job?.vcenter_id ?? vcenters[0]?.id ?? 0);
  const [selected, setSelected] = useState<Map<string, string>>(new Map(job?.vms.map((v) => [v.moref, v.name]) ?? []));
  const [repoId, setRepoId] = useState(job?.repository_id ?? repositories[0]?.id ?? 0);
  const [points, setPoints] = useState(job?.retention_points ?? 14);
  const [days, setDays] = useState(job?.retention_days ?? 0);
  const [quiesce, setQuiesce] = useState(job?.quiesce ?? true);
  const [fullDays, setFullDays] = useState(job?.active_full_days ?? 0);
  const [enabled, setEnabled] = useState(job?.enabled ?? true);
  const initial = parseSchedule(job ? job.schedule_cron : "0 22 * * *");
  const [schedKind, setSchedKind] = useState<ScheduleKind>(initial.kind);
  const [time, setTime] = useState(initial.time);
  const [weekDays, setWeekDays] = useState<number[]>(initial.days);
  const [custom, setCustom] = useState(initial.custom);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const vms = useQuery({
    queryKey: ["vms", vcenterId],
    queryFn: () => get<VmSummary[]>(`/api/vcenters/${vcenterId}/vms`),
    enabled: !!vcenterId && step === 1,
  });
  const visible = useMemo(
    () => (vms.data ?? []).filter((v) => !v.is_template && v.name.toLowerCase().includes(filter.toLowerCase())),
    [vms.data, filter],
  );
  const cron = buildCron(schedKind, time, weekDays, custom);

  function validate(s: number): string {
    if (s === 0 && !name.trim()) return "Give the job a name";
    if (s === 1 && selected.size === 0) return "Select at least one VM";
    if (s === 2 && !repoId) return "Choose a repository";
    if (s === 2 && !points && !days) return "Keep restore points by count, by days, or both";
    if (s === 3 && schedKind === "weekly" && weekDays.length === 0) return "Pick at least one day";
    return "";
  }

  function next() {
    const err = validate(step);
    setError(err);
    if (!err) setStep(step + 1);
  }

  async function save() {
    setSaving(true);
    setError("");
    const body = {
      name: name.trim(),
      description,
      vcenter_id: vcenterId,
      repository_id: repoId,
      vms: [...selected].map(([moref, n]) => ({ moref, name: n })),
      schedule_cron: cron,
      enabled,
      retention_points: points,
      retention_days: days,
      quiesce,
      active_full_days: fullDays,
    };
    try {
      if (job) await put(`/api/jobs/${job.id}`, body);
      else await post("/api/jobs", body);
      onSaved();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  }

  const toggleVm = (v: VmSummary) =>
    setSelected((prev) => {
      const n = new Map(prev);
      if (n.has(v.moref)) n.delete(v.moref);
      else n.set(v.moref, v.name);
      return n;
    });

  return (
    <Modal
      title={job ? `Edit job ${job.name}` : "New backup job"}
      onClose={onClose}
      wide
      footer={
        <>
          {step > 0 && <Button onClick={() => setStep(step - 1)}>Back</Button>}
          <div className="grow" />
          <Button onClick={onClose}>Cancel</Button>
          {step < steps.length - 1 ? (
            <Button variant="primary" onClick={next}>
              Next
            </Button>
          ) : (
            <Button variant="primary" onClick={save} busy={saving}>
              {job ? "Save job" : "Create job"}
            </Button>
          )}
        </>
      }
    >
      <div className="steps">
        {steps.map((s, i) => (
          <div key={s} className={`step ${i === step ? "on" : i < step ? "done" : ""}`}>
            {i + 1}. {s}
          </div>
        ))}
      </div>
      <div className="stack">
        <Alert>{error}</Alert>
        {step === 0 && (
          <>
            <Field label="Name">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Production nightly" maxLength={128} />
            </Field>
            <Field label="Description">
              <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
            <Field label="vCenter">
              <select
                value={vcenterId}
                onChange={(e) => {
                  setVcenterId(Number(e.target.value));
                  setSelected(new Map());
                }}
              >
                {vcenters.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name} ({v.host})
                  </option>
                ))}
              </select>
            </Field>
          </>
        )}
        {step === 1 && (
          <>
            <div className="row gap-s">
              <input className="search" placeholder="Filter VMs…" value={filter} onChange={(e) => setFilter(e.target.value)} />
              <span className="muted">{selected.size} selected</span>
              <div className="grow" />
              <Button onClick={() => vms.refetch()} busy={vms.isFetching}>
                Refresh
              </Button>
            </div>
            {vms.error && <Alert>{errorText(vms.error)}</Alert>}
            <div className="pick-list">
              {vms.isLoading ? (
                <Loading />
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th style={{ width: 32 }} />
                      <th>VM</th>
                      <th>Power</th>
                      <th>Disks</th>
                      <th>Size</th>
                      <th>Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((v) => (
                      <tr key={v.moref} className={`clickable ${selected.has(v.moref) ? "selected" : ""}`} onClick={() => toggleVm(v)}>
                        <td>
                          <input type="checkbox" readOnly checked={selected.has(v.moref)} />
                        </td>
                        <td>
                          <div>{v.name}</div>
                          <div className="muted small">{v.guest_os}</div>
                        </td>
                        <td className="small">{v.power_state.replace("powered", "")}</td>
                        <td>{v.disks}</td>
                        <td className="nowrap">{bytes(v.provisioned_bytes)}</td>
                        <td className="small">
                          {!v.cbt_enabled && <Badge tone="info">CBT will be enabled</Badge>}{" "}
                          {v.has_snapshots && <Badge tone="warn">has snapshots</Badge>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
        {step === 2 && (
          <>
            <Field label="Repository">
              <select value={repoId} onChange={(e) => setRepoId(Number(e.target.value))}>
                {repositories.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name} — {r.kind === "nfs" ? `${r.nfs_server}:${r.nfs_export}` : r.path}
                    {r.encrypted ? " (encrypted)" : ""}
                  </option>
                ))}
              </select>
            </Field>
            <div className="form-grid">
              <Field label="Keep restore points" hint="Newest N points per VM; 0 to keep by age only">
                <input type="number" min={0} value={points} onChange={(e) => setPoints(Number(e.target.value))} />
              </Field>
              <Field label="Keep for days" hint="Points younger than this are kept; 0 to ignore age">
                <input type="number" min={0} value={days} onChange={(e) => setDays(Number(e.target.value))} />
              </Field>
              <Field label="Active full every (days)" hint="Ignore CBT and re-read everything periodically; 0 = never">
                <input type="number" min={0} value={fullDays} onChange={(e) => setFullDays(Number(e.target.value))} />
              </Field>
            </div>
            <label className="check">
              <input type="checkbox" checked={quiesce} onChange={(e) => setQuiesce(e.target.checked)} />
              <span>
                Application-consistent snapshots (quiesce via VMware Tools)
                <div className="muted small">Recommended for databases and Windows servers. Requires VMware Tools in the guest.</div>
              </span>
            </label>
          </>
        )}
        {step === 3 && (
          <>
            <div className="segmented">
              {SCHEDULES.map((s) => (
                <button key={s.id} type="button" className={schedKind === s.id ? "on" : ""} onClick={() => setSchedKind(s.id)}>
                  {s.label}
                </button>
              ))}
            </div>
            {["daily", "weekdays", "weekly"].includes(schedKind) && (
              <Field label="Time" hint="Server local time">
                <input type="time" value={time} onChange={(e) => setTime(e.target.value)} style={{ width: 140 }} />
              </Field>
            )}
            {schedKind === "weekly" && (
              <div className="row gap-m wrap">
                {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d, i) => (
                  <label key={d} className="check">
                    <input
                      type="checkbox"
                      checked={weekDays.includes(i)}
                      onChange={(e) => setWeekDays(e.target.checked ? [...weekDays, i] : weekDays.filter((x) => x !== i))}
                    />
                    {d}
                  </label>
                ))}
              </div>
            )}
            {schedKind === "custom" && (
              <Field label="Cron expression" hint="minute hour day-of-month month day-of-week, e.g. 30 1 * * 1-5">
                <input className="mono" value={custom} onChange={(e) => setCustom(e.target.value)} />
              </Field>
            )}
            {schedKind === "none" && <Alert tone="info">The job will only run when started by hand.</Alert>}
            <label className="check">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              Job enabled
            </label>
          </>
        )}
        {step === 4 && (
          <dl className="kv">
            <dt>Name</dt>
            <dd>{name}</dd>
            <dt>vCenter</dt>
            <dd>{vcenters.find((v) => v.id === vcenterId)?.name}</dd>
            <dt>VMs</dt>
            <dd>{[...selected.values()].join(", ")}</dd>
            <dt>Repository</dt>
            <dd>{repositories.find((r) => r.id === repoId)?.name}</dd>
            <dt>Retention</dt>
            <dd>
              {points ? `${points} restore points` : ""}
              {points && days ? ", or " : ""}
              {days ? `${days} days` : ""}
            </dd>
            <dt>Schedule</dt>
            <dd>
              {describeCron(cron)} {cron && <code className="muted">({cron})</code>}
            </dd>
            <dt>Quiesce</dt>
            <dd>{quiesce ? "Yes" : "No"}</dd>
            <dt>Active full</dt>
            <dd>{fullDays ? `Every ${fullDays} days` : "Never (CBT incrementals forever)"}</dd>
          </dl>
        )}
      </div>
    </Modal>
  );
}
