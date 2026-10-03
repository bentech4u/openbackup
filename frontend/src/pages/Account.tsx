import { useAuth } from "../auth";
import { Card, PageHeader } from "../components/ui";
import { dateTime } from "../format";
import { PasswordForm } from "./Login";

export default function Account() {
  const { user } = useAuth();
  if (!user) return null;
  return (
    <>
      <PageHeader title="Account" />
      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <Card title="Profile">
          <dl className="kv">
            <dt>Username</dt>
            <dd>{user.username}</dd>
            <dt>Name</dt>
            <dd>{user.full_name || "—"}</dd>
            <dt>Role</dt>
            <dd style={{ textTransform: "capitalize" }}>{user.role}</dd>
            <dt>Last sign-in</dt>
            <dd>{dateTime(user.last_login_at)}</dd>
          </dl>
        </Card>
        <Card title="Change password">
          <PasswordForm />
        </Card>
      </div>
    </>
  );
}
