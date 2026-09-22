"use strict";

const $ = (sel) => document.querySelector(sel);

function human(n) {
  if (n === null || n === undefined) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function when(iso) {
  if (!iso) return "never";
  const then = new Date(iso);
  const mins = Math.floor((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

// -- state ------------------------------------------------------------------

let VMS = [];
let POINTS = [];

// -- rendering --------------------------------------------------------------

function renderAlerts(summary) {
  const out = [];
  if (summary.repository_error) {
    out.push(`<div class="alert"><strong>Repository unavailable.</strong>
      ${esc(summary.repository_error)}</div>`);
  }
  if (summary.vcenter_error) {
    out.push(`<div class="alert warn"><strong>vCenter unreachable.</strong>
      ${esc(summary.vcenter_error)} — repository data below is still accurate.</div>`);
  }
  $("#alerts").innerHTML = out.join("");
}

function renderCards(summary) {
  const repo = summary.repository || {};
  const unprotected = summary.vm_count - summary.protected_count;
  const cards = [
    {
      label: "Protected VMs",
      value: `${summary.protected_count} / ${summary.vm_count}`,
      sub: unprotected > 0
        ? `<span class="pill warn">${unprotected} with no restore point</span>`
        : `<span class="pill ok">all covered</span>`,
    },
    {
      label: "Restore points",
      value: summary.point_count,
      sub: summary.latest
        ? `latest ${esc(summary.latest.vm_name)} · ${when(summary.latest.created_at)}`
        : "none yet",
    },
    {
      label: "Repository",
      value: human(repo.pack_bytes),
      sub: `${repo.packs ?? 0} packs · ${(repo.chunks ?? 0).toLocaleString()} chunks`,
    },
    {
      label: "Free space",
      value: human(repo.free_space),
      sub: esc(repo.destination || "-"),
    },
  ];
  $("#cards").innerHTML = cards.map((c) => `
    <div class="card">
      <div class="label">${c.label}</div>
      <div class="value">${c.value}</div>
      <div class="sub">${c.sub}</div>
    </div>`).join("");
}

function vmRow(vm) {
  const blocked = vm.blockers && vm.blockers.length;
  const ready = blocked
    ? `<span class="pill warn" title="${esc(vm.blockers.join(", "))}">${esc(vm.blockers[0])}</span>`
    : `<span class="pill ok">ready</span>`;
  const backup = vm.protected
    ? `<span title="${esc(vm.last_backup)}">${when(vm.last_backup)}</span>`
    : `<span class="pill bad">never</span>`;
  return `
    <tr class="clickable" data-point="${esc(vm.last_point_id || "")}">
      <td>${esc(vm.name)}</td>
      <td><span class="pill mute">${esc(vm.power_state.replace("powered", ""))}</span></td>
      <td class="muted">${esc(vm.guest)}</td>
      <td class="num">${vm.disk_count}</td>
      <td class="num">${human(vm.provisioned)}</td>
      <td>${vm.cbt_enabled ? '<span class="pill ok">on</span>'
                           : '<span class="pill mute">off</span>'}</td>
      <td>${backup}</td>
      <td>${ready}</td>
    </tr>`;
}

function renderVms() {
  const needle = $("#vm-filter").value.trim().toLowerCase();
  const onlyUnprotected = $("#only-unprotected").checked;
  const rows = VMS.filter((vm) => {
    if (onlyUnprotected && vm.protected) return false;
    if (!needle) return true;
    return vm.name.toLowerCase().includes(needle) ||
           (vm.guest || "").toLowerCase().includes(needle);
  });
  if (!rows.length) {
    $("#vms").innerHTML = `<div class="empty">No virtual machines match.</div>`;
    return;
  }
  $("#vms").innerHTML = `
    <table><thead><tr>
      <th>VM</th><th>Power</th><th>Guest</th><th class="num">Disks</th>
      <th class="num">Provisioned</th><th>CBT</th><th>Last backup</th><th>Status</th>
    </tr></thead><tbody>${rows.map(vmRow).join("")}</tbody></table>`;
  $("#vms").querySelectorAll("tr[data-point]").forEach((tr) => {
    const id = tr.getAttribute("data-point");
    if (id) tr.addEventListener("click", () => showPoint(id));
    else tr.classList.remove("clickable");
  });
}

function renderPoints() {
  const needle = $("#point-filter").value.trim().toLowerCase();
  const rows = POINTS.filter((p) => !needle || p.vm_name.toLowerCase().includes(needle));
  if (!rows.length) {
    $("#points").innerHTML = `<div class="empty">No restore points yet.</div>`;
    return;
  }
  $("#points").innerHTML = `
    <table><thead><tr>
      <th>VM</th><th>Created</th><th>Type</th><th class="num">Disks</th>
      <th class="num">Provisioned</th><th class="num">Read</th>
      <th>Consistency</th><th>Restore point</th>
    </tr></thead><tbody>${rows.map((p) => `
      <tr class="clickable" data-point="${esc(p.id)}">
        <td>${esc(p.vm_name)}</td>
        <td title="${esc(p.created_at)}">${when(p.created_at)}</td>
        <td><span class="pill ${p.kind === "full" ? "mute" : "ok"}">${esc(p.kind)}</span></td>
        <td class="num">${p.disks.length}</td>
        <td class="num">${human(p.capacity)}</td>
        <td class="num">${human(p.bytes_read)}</td>
        <td>${p.quiesced ? '<span class="pill ok">application</span>'
                         : '<span class="pill warn">crash</span>'}</td>
        <td class="mono muted">${esc(p.id)}</td>
      </tr>`).join("")}</tbody></table>`;
  $("#points").querySelectorAll("tr[data-point]").forEach((tr) =>
    tr.addEventListener("click", () => showPoint(tr.getAttribute("data-point"))));
}

function renderRepo(repo) {
  if (!repo) {
    $("#repo").innerHTML = `<div class="empty">Repository unavailable.</div>`;
    return;
  }
  const ratio = repo.pack_bytes && POINTS.length
    ? (POINTS.reduce((a, p) => a + p.bytes_read, 0) / repo.pack_bytes)
    : null;
  $("#repo").innerHTML = `
    <dl class="kv">
      <dt>Destination</dt><dd class="mono">${esc(repo.destination)}</dd>
      <dt>Type</dt><dd>${esc(repo.kind)}</dd>
      <dt>Block size</dt><dd>${human(repo.chunk_size)}</dd>
      <dt>Pack size</dt><dd>${human(repo.pack_size)}</dd>
      <dt>Encrypted</dt><dd>${repo.encrypted ? "yes" : "no"}</dd>
      <dt>Pack files</dt><dd>${repo.packs.toLocaleString()}</dd>
      <dt>Stored</dt><dd>${human(repo.pack_bytes)}</dd>
      <dt>Chunks</dt><dd>${repo.chunks.toLocaleString()}</dd>
      <dt>Protected VMs</dt><dd>${repo.vms}</dd>
      <dt>Free space</dt><dd>${human(repo.free_space)}</dd>
      ${ratio ? `<dt>Reduction</dt><dd>${ratio.toFixed(1)}x vs bytes read</dd>` : ""}
    </dl>`;
}

function showPoint(id) {
  const point = POINTS.find((p) => p.id === id);
  if (!point) return;
  $("#drawer-body").innerHTML = `
    <h2>${esc(point.vm_name)}</h2>
    <div class="mono muted">${esc(point.id)}</div>
    <dl class="kv">
      <dt>Created</dt><dd>${esc(point.created_at)}</dd>
      <dt>Type</dt><dd>${esc(point.kind)}${
        point.parent_id ? ` (from ${esc(point.parent_id)})` : ""}</dd>
      <dt>Consistency</dt><dd>${point.quiesced
        ? "application-consistent (guest filesystems quiesced)"
        : "crash-consistent"}</dd>
      <dt>Guest</dt><dd>${esc(point.guest || "-")}</dd>
      <dt>Datacenter</dt><dd>${esc(point.datacenter || "-")}</dd>
      <dt>Read from VM</dt><dd>${human(point.bytes_read)}</dd>
    </dl>
    <h3>Disks</h3>
    ${point.disks.map((d) => `
      <dl class="kv">
        <dt>${esc(d.label)}</dt><dd>${human(d.capacity)}${d.thin ? " thin" : ""}</dd>
        <dt>Datastore</dt><dd>${esc(d.datastore)}</dd>
        <dt>File</dt><dd class="mono">${esc(d.flat_path)}</dd>
        <dt>Read</dt><dd>${human(d.bytes_read)} · ${d.blocks_changed.toLocaleString()} blocks</dd>
        <dt>changeId</dt><dd class="mono">${esc(d.change_id || "-")}</dd>
      </dl>`).join("")}
    <h3>Restore</h3>
    <p class="muted">This interface is read-only. To restore:</p>
    <p class="mono">openbackup restore ${esc(point.id)} --to-dir /path</p>`;
  $("#drawer").hidden = false;
}

// -- wiring -----------------------------------------------------------------

async function load() {
  try {
    const health = await api("/api/health");
    $("#status").innerHTML =
      `<span class="dot ${health.ok ? "ok" : "bad"}"></span>` +
      `${esc(health.repository)} · vCenter ${esc(health.vcenter)}`;
  } catch (err) {
    $("#status").innerHTML = `<span class="dot bad"></span>unreachable`;
  }

  try {
    const [summary, vms, points] = await Promise.all([
      api("/api/summary"), api("/api/vms"), api("/api/points"),
    ]);
    VMS = vms.vms || [];
    POINTS = points.points || [];
    renderAlerts(summary);
    renderCards(summary);
    renderVms();
    renderPoints();
    renderRepo(summary.repository);
    $("#footer-note").textContent =
      `${VMS.length} VMs · ${POINTS.length} restore points · refreshed ` +
      new Date().toLocaleTimeString();
  } catch (err) {
    $("#alerts").innerHTML =
      `<div class="alert"><strong>Could not load.</strong> ${esc(err.message)}</div>`;
  }
}

document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
  });
});

$("#vm-filter").addEventListener("input", renderVms);
$("#only-unprotected").addEventListener("change", renderVms);
$("#point-filter").addEventListener("input", renderPoints);
$("#refresh-vms").addEventListener("click", async () => {
  const btn = $("#refresh-vms");
  btn.disabled = true; btn.textContent = "Refreshing…";
  try { await api("/api/vms?refresh=true"); await load(); }
  finally { btn.disabled = false; btn.textContent = "Refresh inventory"; }
});
$("#drawer-close").addEventListener("click", () => { $("#drawer").hidden = true; });
$("#drawer").addEventListener("click", (e) => {
  if (e.target === $("#drawer")) $("#drawer").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#drawer").hidden = true;
});

load();
setInterval(load, 30000);
