import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { get, post, type Task, type TaskLog, type TaskState } from "../api";
import { useAuth } from "../auth";
import { Alert, Button, Card, Loading, PageHeader, Progress, StateBadge, errorText } from "../components/ui";
import { bytes, dateTime, duration, throughput } from "../format";
import { TaskTable } from "./Dashboard";

const STATES: (TaskState | "")[] = ["", "running", "success", "warning", "failed", "cancelled"];

export default function Tasks() {
  const [state, setState] = useState<TaskState | "">("");
  const [kind, setKind] = useState("");
  const { data, isLoading } = useQuery({
    queryKey: ["tasks", state, kind],
    queryFn: () => {
      const q = new URLSearchParams({ limit: "200" });
      if (state) q.set("state", state);
      if (kind) q.set("kind", kind);
      return get<Task[]>(`/api/tasks?${q}`);
    },
    refetchInterval: 5000,
  });
  return (
    <>
      <PageHeader title="History" subtitle="Every backup, restore, verification and maintenance run" />
      <Card
        pad={false}
        title={
          <div className="row gap-s">
            <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 150 }}>
              <option value="">All kinds</option>
              <option value="backup">Backups</option>
              <option value="restore">Restores</option>
              <option value="verify">Verifications</option>
              <option value="gc">Maintenance</option>
            </select>
            <select value={state} onChange={(e) => setState(e.target.value as TaskState)} style={{ width: 150 }}>
              {STATES.map((s) => (
                <option key={s} value={s}>
                  {s ? s[0].toUpperCase() + s.slice(1) : "All states"}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {isLoading ? <Loading /> : <TaskTable tasks={data ?? []} empty="No matching tasks." />}
      </Card>
    </>
  );
}

function useTaskStream(id: number) {
  const [task, setTask] = useState<Task | null>(null);
  const [logs, setLogs] = useState<TaskLog[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    setLogs([]);
    setTask(null);
    let es: EventSource | null = null;
    let stopped = false;
    // Initial load over plain fetch so errors (403/404) are visible.
    get<Task>(`/api/tasks/${id}`)
      .then((t) => {
        if (stopped) return;
        setTask(t);
        es = new EventSource(`/api/tasks/${id}/stream`);
        es.addEventListener("task", (e) => setTask(JSON.parse((e as MessageEvent).data)));
        es.addEventListener("logs", (e) => {
          const batch: TaskLog[] = JSON.parse((e as MessageEvent).data);
          setLogs((prev) => {
            const seen = new Set(prev.map((l) => l.id));
            return [...prev, ...batch.filter((l) => !seen.has(l.id))];
          });
        });
        es.addEventListener("end", () => es?.close());
        es.onerror = () => {
          // The browser retries on its own; once the task is over the
          // stream ends and the retry is pointless.
        };
      })
      .catch((e) => setError(errorText(e)));
    return () => {
      stopped = true;
      es?.close();
    };
  }, [id]);

  return { task, logs, error };
}

export function TaskDetail() {
  const id = Number(useParams().id);
  const { task, logs, error } = useTaskStream(id);
  const { can } = useAuth();
  const qc = useQueryClient();
  const [cancelling, setCancelling] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 80) el.scrollTop = el.scrollHeight;
  }, [logs]);

  if (error) return <Alert>{error}</Alert>;
  if (!task) return <Loading />;
  const active = task.state === "running" || task.state === "queued";
  const moved = Math.max(task.bytes_read, task.bytes_written);

  return (
    <>
      <PageHeader
        title={task.title}
        subtitle={
          <span className="row gap-s">
            <StateBadge state={task.state} />
            <span>
              Task #{task.id} · requested by {task.requested_by} · {dateTime(task.created_at)}
            </span>
          </span>
        }
        actions={
          <>
            {task.job_id && <Link to="/jobs">View job</Link>}
            {active && can("operator") && (
              <Button
                variant="danger"
                busy={cancelling}
                disabled={task.cancel_requested}
                onClick={async () => {
                  setCancelling(true);
                  try {
                    await post(`/api/tasks/${task.id}/cancel`);
                    qc.invalidateQueries({ queryKey: ["tasks"] });
                  } finally {
                    setCancelling(false);
                  }
                }}
              >
                {task.cancel_requested ? "Stopping…" : "Stop"}
              </Button>
            )}
          </>
        }
      />
      {task.summary && <Alert tone={task.state === "failed" ? "bad" : task.state === "warning" ? "warn" : "good"}>{task.summary}</Alert>}
      <div className="grid grid-4" style={{ margin: "20px 0" }}>
        <div className="stat">
          <div className="stat-label">Progress</div>
          <div className="stat-value">{Math.round(task.progress * 100)}%</div>
          <Progress value={task.progress} state={task.state} />
        </div>
        <div className="stat">
          <div className="stat-label">Duration</div>
          <div className="stat-value">{duration(task.started_at, task.finished_at)}</div>
          <div className="stat-sub">{task.started_at ? `started ${dateTime(task.started_at)}` : "waiting for the worker"}</div>
        </div>
        <div className="stat">
          <div className="stat-label">{task.kind === "restore" ? "Written" : "Read"}</div>
          <div className="stat-value">{bytes(moved)}</div>
          <div className="stat-sub">{task.bytes_total ? `of ${bytes(task.bytes_total)}` : ""}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Throughput</div>
          <div className="stat-value">{throughput(moved, task.started_at, task.finished_at)}</div>
          {task.kind === "backup" && <div className="stat-sub">{bytes(task.bytes_written)} stored after dedup</div>}
        </div>
      </div>
      {task.items.length > 0 && (
        <Card title="Items" pad={false}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Item</th>
                  <th>State</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {task.items.map((it) => (
                  <tr key={it.name}>
                    <td>{it.name}</td>
                    <td>
                      <StateBadge state={it.state === "done" ? "success" : it.state === "pending" ? "queued" : it.state} />
                    </td>
                    <td className="muted small">{itemDetails(it)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      <Card title="Log">
        <div className="log" ref={logRef}>
          {logs.length === 0 && <span className="t">No log lines yet.</span>}
          {logs.map((l) => (
            <div key={l.id} className={l.level}>
              <span className="t">{new Date(l.at).toLocaleTimeString()}</span>
              {l.message}
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}

function itemDetails(it: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof it.mode === "string") parts.push(it.mode);
  if (typeof it.kind === "string" && it.kind !== "vm") parts.push(it.kind);
  if (typeof it.to_read === "number") parts.push(`${bytes(it.read as number)} of ${bytes(it.to_read)} read`);
  else if (typeof it.read === "number") parts.push(`${bytes(it.read)} read`);
  if (typeof it.new === "number") parts.push(`${bytes(it.new)} new`);
  if (typeof it.written === "number") parts.push(`${bytes(it.written)} written`);
  if (typeof it.point_id === "string") parts.push(`point ${it.point_id}`);
  if (typeof it.error === "string") parts.push(it.error);
  if (Array.isArray(it.errors) && it.errors.length) parts.push(it.errors.join("; "));
  return parts.join(" · ");
}
