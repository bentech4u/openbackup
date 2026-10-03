import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { get, type Repository, type Task } from "../api";
import { Card, Empty, Loading, PageHeader, Progress, Stat, StateBadge } from "../components/ui";
import { bytes, dateTime, relative } from "../format";

interface DashboardData {
  backups_24h: Record<string, number>;
  success_rate_24h: number | null;
  protected_vms: number;
  stale_vms: number;
  restore_points: number;
  jobs: number;
  jobs_enabled: number;
  running: Task[];
  recent: Task[];
  repositories: Repository[];
  upcoming: { job_id: number; name: string; next_run_at: string }[];
}

export function TaskTable({ tasks, empty }: { tasks: Task[]; empty: string }) {
  const nav = useNavigate();
  if (!tasks.length) return <Empty>{empty}</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>State</th>
            <th>Progress</th>
            <th>Started</th>
            <th>By</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => (
            <tr key={t.id} className="clickable" onClick={() => nav(`/tasks/${t.id}`)}>
              <td>
                <div>{t.title}</div>
                {t.summary && <div className="muted small">{t.summary}</div>}
              </td>
              <td>
                <StateBadge state={t.state} />
              </td>
              <td style={{ width: 140 }}>
                {t.state === "running" ? <Progress value={t.progress} /> : <span className="muted small">{Math.round(t.progress * 100)}%</span>}
              </td>
              <td className="nowrap">{t.started_at ? relative(t.started_at) : relative(t.created_at)}</td>
              <td className="muted">{t.requested_by}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function capacityMeter(r: Repository) {
  if (!r.capacity_bytes) return <span className="muted small">unknown</span>;
  const used = r.capacity_bytes - (r.free_bytes ?? 0);
  const frac = used / r.capacity_bytes;
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className={`meter ${frac > 0.9 ? "full" : frac > 0.75 ? "high" : ""}`}>
        <div style={{ width: `${frac * 100}%` }} />
      </div>
      <span className="muted small">
        {bytes(r.free_bytes)} free of {bytes(r.capacity_bytes)}
      </span>
    </div>
  );
}

export default function Dashboard() {
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => get<DashboardData>("/api/dashboard"),
    refetchInterval: 5000,
  });
  if (isLoading || !data) return <Loading />;
  const failed = (data.backups_24h.failed ?? 0) + (data.backups_24h.warning ?? 0);
  const rate = data.success_rate_24h;
  return (
    <>
      <PageHeader title="Dashboard" subtitle="Backup health at a glance" />
      <div className="grid grid-4" style={{ marginBottom: 20 }}>
        <Stat
          label="Success rate (24h)"
          value={rate == null ? "—" : `${Math.round(rate * 100)}%`}
          sub={`${data.backups_24h.success ?? 0} succeeded, ${failed} with problems`}
          tone={rate == null ? undefined : rate === 1 ? "good" : rate > 0.8 ? "warn" : "bad"}
        />
        <Stat label="Protected VMs" value={data.protected_vms} sub={`${data.restore_points} restore points`} />
        <Stat
          label="Not backed up in 48h"
          value={data.stale_vms}
          tone={data.stale_vms ? "warn" : "good"}
          sub="VMs whose newest point is older than 2 days"
        />
        <Stat label="Backup jobs" value={data.jobs} sub={`${data.jobs_enabled} enabled`} />
      </div>
      <Card title="Running and queued">
        <TaskTable tasks={data.running} empty="Nothing is running right now." />
      </Card>
      <div className="grid grid-2">
        <Card title="Recent tasks" actions={<Link to="/tasks">All history</Link>} pad={false}>
          <TaskTable tasks={data.recent} empty="No tasks have run yet." />
        </Card>
        <div>
          <Card title="Repositories" actions={<Link to="/repositories">Manage</Link>}>
            {data.repositories.length ? (
              <div className="stack">
                {data.repositories.map((r) => (
                  <div key={r.id}>
                    <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                      <strong>{r.name}</strong>
                      <span className="muted small">{r.kind === "nfs" ? `${r.nfs_server}:${r.nfs_export}` : r.path}</span>
                    </div>
                    {capacityMeter(r)}
                  </div>
                ))}
              </div>
            ) : (
              <Empty>
                No repositories yet. <Link to="/repositories">Add one</Link>.
              </Empty>
            )}
          </Card>
          <Card title="Upcoming runs">
            {data.upcoming.length ? (
              <dl className="kv">
                {data.upcoming.map((u) => (
                  <div key={u.job_id} style={{ display: "contents" }}>
                    <dt>{u.name}</dt>
                    <dd>
                      {dateTime(u.next_run_at)} <span className="muted">({relative(u.next_run_at)})</span>
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <Empty>No scheduled jobs.</Empty>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
