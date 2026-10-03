import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { del, get, patch, post, type Role, type User } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Field, Loading, Modal, PageHeader, errorText } from "../components/ui";
import { dateTime, relative } from "../format";

const ROLE_HELP: Record<Role, string> = {
  viewer: "Sees everything, changes nothing",
  operator: "Also runs and stops jobs, verifies, restores to new VMs",
  admin: "Full control: configuration, users, overwriting VMs, deleting backups",
};

export default function Users() {
  const { user: me } = useAuth();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => get<User[]>("/api/users") });
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const [deleting, setDeleting] = useState<User | null>(null);
  const [error, setError] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });

  async function act(fn: () => Promise<unknown>) {
    setError("");
    try {
      await fn();
      refresh();
    } catch (e) {
      setError(errorText(e));
    }
  }

  return (
    <>
      <PageHeader
        title="Users"
        subtitle="Who can sign in, and what they may do"
        actions={
          <Button variant="primary" onClick={() => setEditing("new")}>
            <Plus size={16} /> New user
          </Button>
        }
      />
      <Alert>{error}</Alert>
      <Card pad={false}>
        {users.isLoading ? (
          <Loading />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Last sign-in</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.data?.map((u) => {
                  const locked = u.locked_until && new Date(u.locked_until) > new Date();
                  return (
                    <tr key={u.id}>
                      <td>
                        <strong>{u.username}</strong>
                        {u.id === me?.id && <span className="muted"> (you)</span>}
                        {u.full_name && <div className="muted small">{u.full_name}</div>}
                      </td>
                      <td style={{ textTransform: "capitalize" }}>{u.role}</td>
                      <td>
                        <div className="row gap-s wrap">
                          {u.is_active ? <Badge tone="good">active</Badge> : <Badge>disabled</Badge>}
                          {locked && <Badge tone="bad">locked</Badge>}
                          {u.must_change_password && <Badge tone="warn">must change password</Badge>}
                        </div>
                      </td>
                      <td title={dateTime(u.last_login_at)}>{u.last_login_at ? relative(u.last_login_at) : "never"}</td>
                      <td className="right">
                        <div className="row gap-s" style={{ justifyContent: "flex-end" }}>
                          {locked && <Button onClick={() => act(() => patch(`/api/users/${u.id}`, { unlock: true }))}>Unlock</Button>}
                          <Button onClick={() => setEditing(u)}>Edit</Button>
                          {u.id !== me?.id && (
                            <>
                              <Button variant="ghost" onClick={() => act(() => post(`/api/users/${u.id}/revoke-sessions`))}>
                                Sign out everywhere
                              </Button>
                              <Button variant="ghost" onClick={() => setDeleting(u)}>
                                Delete
                              </Button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {editing && (
        <UserEditor
          user={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
      {deleting && (
        <Confirm
          title={`Delete ${deleting.username}?`}
          message="The account is removed and signed out. The audit log keeps its history."
          confirmLabel="Delete user"
          danger
          onClose={() => setDeleting(null)}
          onConfirm={() => {
            act(() => del(`/api/users/${deleting.id}`));
            setDeleting(null);
          }}
        />
      )}
    </>
  );
}

function UserEditor({ user, onClose, onDone }: { user: User | null; onClose: () => void; onDone: () => void }) {
  const [username, setUsername] = useState(user?.username ?? "");
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [role, setRole] = useState<Role>(user?.role ?? "viewer");
  const [active, setActive] = useState(user?.is_active ?? true);
  const [password, setPassword] = useState("");
  const [mustChange, setMustChange] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    setError("");
    try {
      if (user) {
        await patch(`/api/users/${user.id}`, {
          full_name: fullName,
          role,
          is_active: active,
          password: password || null,
        });
      } else {
        await post("/api/users", { username, full_name: fullName, role, password, must_change_password: mustChange });
      }
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={user ? `Edit ${user.username}` : "New user"}
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
        {!user && (
          <Field label="Username" hint="Letters, digits, . _ @ -">
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
          </Field>
        )}
        <Field label="Full name">
          <input value={fullName} onChange={(e) => setFullName(e.target.value)} />
        </Field>
        <Field label="Role" hint={ROLE_HELP[role]}>
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="viewer">Viewer</option>
            <option value="operator">Operator</option>
            <option value="admin">Admin</option>
          </select>
        </Field>
        <Field
          label={user ? "Reset password" : "Initial password"}
          hint={user ? "Leave empty to keep. A reset signs the user out and forces a change at next sign-in." : "At least 12 characters"}
        >
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </Field>
        {!user && (
          <label className="check">
            <input type="checkbox" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
            Require a password change at first sign-in
          </label>
        )}
        {user && (
          <label className="check">
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
            Account enabled
          </label>
        )}
      </div>
    </Modal>
  );
}
