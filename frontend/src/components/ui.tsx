import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Clock, Loader2, MinusCircle, X, XCircle } from "lucide-react";
import type { TaskState } from "../api";

export function Button({
  variant = "secondary",
  busy,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "ghost";
  busy?: boolean;
}) {
  return (
    <button className={`btn btn-${variant}`} disabled={busy || rest.disabled} {...rest}>
      {busy && <Loader2 size={14} className="spin" />}
      {children}
    </button>
  );
}

export function Card({ title, actions, children, pad = true }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  pad?: boolean;
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <h2>{title}</h2>
          <div className="row gap-s">{actions}</div>
        </header>
      )}
      <div className={pad ? "card-body" : ""}>{children}</div>
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      <div className="row gap-s">{actions}</div>
    </div>
  );
}

const stateIcon: Record<TaskState, ReactNode> = {
  queued: <Clock size={14} />,
  running: <Loader2 size={14} className="spin" />,
  success: <CheckCircle2 size={14} />,
  warning: <AlertTriangle size={14} />,
  failed: <XCircle size={14} />,
  cancelled: <MinusCircle size={14} />,
};

export function StateBadge({ state }: { state: TaskState | string | null | undefined }) {
  if (!state) return <span className="muted">—</span>;
  const s = state as TaskState;
  return (
    <span className={`badge badge-state badge-${s}`}>
      {stateIcon[s]}
      {s}
    </span>
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "info" | "good" | "warn" | "bad" }) {
  return <span className={`badge badge-tone-${tone}`}>{children}</span>;
}

export function Progress({ value, state }: { value: number; state?: string }) {
  return (
    <div className={`progress ${state === "failed" ? "progress-bad" : ""}`}>
      <div style={{ width: `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%` }} />
    </div>
  );
}

export function Modal({ title, onClose, children, footer, wide }: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    ref.current?.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <header className="modal-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-foot">{footer}</footer>}
      </div>
    </div>
  );
}

export function Field({ label, hint, children, error }: { label: string; hint?: ReactNode; children: ReactNode; error?: string }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && !error && <span className="field-hint">{hint}</span>}
      {error && <span className="field-error">{error}</span>}
    </label>
  );
}

export function Alert({ tone = "bad", children }: { tone?: "bad" | "warn" | "info" | "good"; children: ReactNode }) {
  if (!children) return null;
  return <div className={`alert alert-${tone}`}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading() {
  return (
    <div className="loading">
      <Loader2 className="spin" size={18} /> Loading…
    </div>
  );
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : e ? String(e) : "";
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "good" | "warn" | "bad" }) {
  return (
    <div className={`stat ${tone ? `stat-${tone}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function Confirm({ title, message, confirmLabel = "Confirm", danger, onConfirm, onClose, busy, requireText }: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  requireText?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant={danger ? "danger" : "primary"}
            busy={busy}
            onClick={() => {
              if (requireText && inputRef.current?.value !== requireText) {
                inputRef.current?.focus();
                return;
              }
              onConfirm();
            }}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="stack">
        <div>{message}</div>
        {requireText && (
          <Field label={`Type "${requireText}" to confirm`}>
            <input ref={inputRef} autoComplete="off" />
          </Field>
        )}
      </div>
    </Modal>
  );
}
