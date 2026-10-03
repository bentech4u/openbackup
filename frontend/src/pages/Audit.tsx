import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { get, type AuditEntry } from "../api";
import { Badge, Button, Card, Empty, Loading, PageHeader } from "../components/ui";
import { dateTime } from "../format";

export default function Audit() {
  const [action, setAction] = useState("");
  const [username, setUsername] = useState("");
  const q = useInfiniteQuery({
    queryKey: ["audit", action, username],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams({ limit: "100" });
      if (pageParam) p.set("before_id", String(pageParam));
      if (action) p.set("action", action);
      if (username.trim()) p.set("username", username.trim());
      return get<AuditEntry[]>(`/api/audit?${p}`);
    },
    getNextPageParam: (last) => (last.length === 100 ? last[last.length - 1].id : undefined),
  });
  const rows = q.data?.pages.flat() ?? [];

  return (
    <>
      <PageHeader title="Audit log" subtitle="Sign-ins and every change, with who made it and from where" />
      <Card
        pad={false}
        title={
          <div className="row gap-s">
            <select value={action} onChange={(e) => setAction(e.target.value)} style={{ width: 170 }}>
              <option value="">All actions</option>
              <option value="auth.">Sign-ins</option>
              <option value="user.">Users</option>
              <option value="vcenter.">vCenters</option>
              <option value="repository.">Repositories</option>
              <option value="job.">Jobs</option>
              <option value="point.">Restore points</option>
              <option value="task.">Tasks</option>
            </select>
            <input className="search" placeholder="Username" value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
        }
      >
        {q.isLoading ? (
          <Loading />
        ) : !rows.length ? (
          <Empty>No entries.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>User</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Address</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.id}>
                    <td className="nowrap">{dateTime(e.at)}</td>
                    <td>{e.username || "—"}</td>
                    <td>
                      <span className="row gap-s">
                        <code>{e.action}</code>
                        {!e.success && <Badge tone="bad">failed</Badge>}
                      </span>
                    </td>
                    <td>{e.target}</td>
                    <td className="mono small">{e.ip}</td>
                    <td className="mono small muted">{Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {q.hasNextPage && (
          <div className="card-body">
            <Button onClick={() => q.fetchNextPage()} busy={q.isFetchingNextPage}>
              Load older entries
            </Button>
          </div>
        )}
      </Card>
    </>
  );
}
