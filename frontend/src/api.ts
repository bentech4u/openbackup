// Thin fetch wrapper: JSON in and out, the session cookie is sent by the
// browser, and every mutating request carries the CSRF token.

export type Role = "viewer" | "operator" | "admin";
export type TaskState = "queued" | "running" | "success" | "warning" | "failed" | "cancelled";

export interface User {
  id: number;
  username: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
  locked_until: string | null;
}

export interface VCenter {
  id: number;
  name: string;
  host: string;
  port: number;
  username: string;
  thumbprint: string;
  created_at: string;
}

export interface VmSummary {
  moref: string;
  name: string;
  instance_uuid: string;
  power_state: string;
  guest_os: string;
  cpu: number;
  memory_mb: number;
  provisioned_bytes: number;
  disks: number;
  cbt_enabled: boolean;
  has_snapshots: boolean;
  is_template: boolean;
  folder: string;
  host: string;
  datastores: string[];
  tools_status: string;
}

export interface Repository {
  id: number;
  name: string;
  kind: "local" | "nfs";
  path: string;
  nfs_server: string;
  nfs_export: string;
  nfs_options: string;
  encrypted: boolean;
  capacity_bytes: number | null;
  free_bytes: number | null;
  created_at: string;
  shares_datastore: string | null;
}

export interface Datastore {
  moref: string;
  name: string;
  type: string;
  capacity: number;
  free: number;
  accessible: boolean;
  remote_host: string;
  remote_path: string;
  direct_nfs: { nfs_server: string; nfs_export: string; nfs_options: string; enabled: boolean } | null;
}

export interface Job {
  id: number;
  name: string;
  description: string;
  kind: "vsphere" | "openshift" | "etcd";
  cluster_id: number | null;
  selection: Record<string, unknown>;
  vcenter_id: number | null;
  repository_id: number;
  vms: { moref: string; name: string }[];
  schedule_cron: string | null;
  enabled: boolean;
  retention_points: number;
  retention_days: number;
  quiesce: boolean;
  active_full_days: number;
  created_at: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_state: TaskState | null;
}

export interface TaskItem {
  name: string;
  state?: string;
  [k: string]: unknown;
}

export interface Task {
  id: number;
  kind: "backup" | "restore" | "verify" | "gc";
  state: TaskState;
  title: string;
  job_id: number | null;
  repository_id: number | null;
  requested_by: string;
  params: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress: number;
  bytes_total: number;
  bytes_read: number;
  bytes_written: number;
  cancel_requested: boolean;
  summary: string;
  items: TaskItem[];
}

export interface TaskLog {
  id: number;
  at: string;
  level: string;
  message: string;
}

export interface RestorePoint {
  id: string;
  repository_id: number;
  job_id: number | null;
  subject_kind: "vm" | "namespace" | "etcd";
  vm_uuid: string;
  vm_name: string;
  vm_moref: string;
  created_at: string;
  kind: string;
  logical_bytes: number;
  read_bytes: number;
  new_bytes: number;
  disks: { key: number; label: string; capacity: number }[];
}

export interface PointDetail extends RestorePoint {
  config: {
    name: string;
    guest_full_name?: string;
    cpu: number;
    memory_mb: number;
    firmware?: string;
    nics: { type: string; network: string; mac: string }[];
    disks: { key: number; label: string; capacity: number; datastore: string }[];
  };
  vcenter: string;
  warnings: string[];
  duration_s: number | null;
  disk_details: { key: number; label: string; capacity: number; mode: string; read_bytes: number }[];
}

export interface AuditEntry {
  id: number;
  at: string;
  username: string;
  ip: string;
  action: string;
  target: string;
  success: boolean;
  detail: Record<string, unknown>;
}

export interface Placement {
  datacenters: { moref: string; name: string; vm_folder: string }[];
  hosts: { moref: string; name: string; connected: boolean; maintenance: boolean }[];
  datastores: { moref: string; name: string; capacity: number; free: number; type: string; accessible: boolean }[];
  networks: { moref: string; name: string; kind: string }[];
  folders: { moref: string; name: string }[];
  resource_pools: { moref: string; name: string; owner: string }[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let csrfToken = "";
let onUnauthorized: () => void = () => {};

export function setCsrfToken(t: string) {
  csrfToken = t;
}

export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

function detail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((e: { loc?: unknown[]; msg?: string }) =>
          `${(e.loc ?? []).filter((x) => x !== "body").join(".")}: ${e.msg ?? ""}`)
        .join("; ");
    }
  }
  return fallback;
}

export async function api<T = unknown>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !url.endsWith("/auth/login")) onUnauthorized();
    throw new ApiError(res.status, detail(data, res.statusText));
  }
  return data as T;
}

export const get = <T,>(url: string) => api<T>("GET", url);
export const post = <T,>(url: string, body?: unknown) => api<T>("POST", url, body ?? {});
export const put = <T,>(url: string, body: unknown) => api<T>("PUT", url, body);
export const patch = <T,>(url: string, body: unknown) => api<T>("PATCH", url, body);
export const del = <T,>(url: string) => api<T>("DELETE", url);
