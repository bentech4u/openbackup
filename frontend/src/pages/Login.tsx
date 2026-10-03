import { useState, type FormEvent } from "react";
import { useAuth } from "../auth";
import { Alert, Button, Field, errorText } from "../components/ui";
import { post } from "../api";

export function Login() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(username.trim(), password);
    } catch (err) {
      setError(errorText(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card stack" onSubmit={submit}>
        <div className="brand">
          <img src="/favicon.svg" alt="" />
          OpenBackup
        </div>
        <Alert>{error}</Alert>
        <Field label="Username">
          <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </Field>
        <Field label="Password">
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        <Button variant="primary" type="submit" busy={busy}>
          Sign in
        </Button>
      </form>
    </div>
  );
}

export function PasswordForm({ forced, onDone }: { forced?: boolean; onDone?: () => void }) {
  const { refresh, logout } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setOk("");
    if (next !== repeat) {
      setError("The new passwords do not match");
      return;
    }
    setBusy(true);
    try {
      await post("/api/auth/password", { current_password: current, new_password: next });
      setCurrent("");
      setNext("");
      setRepeat("");
      setOk("Password changed. Other sessions have been signed out.");
      await refresh();
      onDone?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit}>
      {forced && <Alert tone="info">You must choose a new password before continuing.</Alert>}
      <Alert>{error}</Alert>
      <Alert tone="good">{ok}</Alert>
      <Field label="Current password">
        <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
      </Field>
      <Field label="New password" hint="At least 12 characters. A passphrase of several words works well.">
        <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={12} />
      </Field>
      <Field label="Repeat new password">
        <input type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required />
      </Field>
      <div className="row gap-s">
        <Button variant="primary" type="submit" busy={busy}>
          Change password
        </Button>
        {forced && (
          <Button type="button" variant="ghost" onClick={() => logout()}>
            Sign out
          </Button>
        )}
      </div>
    </form>
  );
}

export function ForcedPasswordChange() {
  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="brand">
          <img src="/favicon.svg" alt="" />
          OpenBackup
        </div>
        <PasswordForm forced />
      </div>
    </div>
  );
}
