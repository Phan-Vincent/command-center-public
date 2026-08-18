/* Command Center frontend — vanilla JS, zero dependencies. */
"use strict";

const REFRESH_MS = 15000;
const state = { status: null, token: null, auditOpen: false, logTarget: null };

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* --- inline SVG icons (stroke) ------------------------------------------- */
const ICONS = {
  system: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  services: '<path d="M12 2l8 4.5-8 4.5-8-4.5L12 2z"/><path d="M4 12l8 4.5 8-4.5M4 16.5l8 4.5 8-4.5"/>',
  containers: '<path d="M21 8l-9-5-9 5v8l9 5 9-5V8z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
  agents: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/>',
  cron: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  ports: '<path d="M12 2a10 10 0 000 20M12 2a10 10 0 010 20M2 12h20M4 8a15 15 0 0116 0M4 16a15 15 0 0016 0"/>',
  projects: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>',
  links: '<path d="M10 14a4 4 0 006 0l3-3a4 4 0 00-6-6l-1.5 1.5"/><path d="M14 10a4 4 0 00-6 0l-3 3a4 4 0 006 6l1.5-1.5"/>',
  control: '<path d="M4 17l6-5-6-5M12 19h8"/>',
  backup: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  procs: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
};
function icon(name, size = 14) {
  const inner = ICONS[name] || "";
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex:none">${inner}</svg>`;
}

/* --- formatting helpers ---------------------------------------------------- */
function fmtUptime(secs) {
  if (secs == null) return "—";
  const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
  return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m`;
}
function bar(pct) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const cls = v >= 90 ? "crit" : v >= 70 ? "warn" : "";
  return `<div class="bar ${cls}"><i style="width:${v}%"></i></div>`;
}
function chip(st) {
  const s = String(st || "").toLowerCase();
  const cls = ["active", "running", "healthy", "failed", "inactive", "waiting", "dead", "error", "scheduled", "critical"].includes(s) ? s : "";
  return `<span class="chip ${cls}">${esc(st || "?")}</span>`;
}
function sparkline(values, color = "#4cc2ff") {
  if (!values || values.length < 2) return `<div class="muted" style="font-size:10px">collecting…</div>`;
  const w = 200, h = 34, pad = 2;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="1.5"
      stroke-linejoin="round" stroke-linecap="round"/>
    <polygon points="${pad},${h} ${pts.join(" ")} ${w - pad},${h}" fill="${color}" opacity="0.12"/>
  </svg>`;
}

/* --- API helper (handles optional token) ----------------------------------- */
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers["X-Auth-Token"] = state.token;
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) {
    state.token = prompt("This endpoint requires the Command Center auth token:");
    if (!state.token) throw new Error("unauthorized");
    const r2 = await fetch(path, { ...opts, headers: { ...opts.headers, "X-Auth-Token": state.token } });
    return r2.json();
  }
  return r.json();
}

/* --- main render pipeline --------------------------------------------------- */
async function refreshStatus() {
  try {
    state.status = await api("/api/status");
    render();
  } catch (e) {
    toast(`status fetch failed: ${e}`, true);
  }
}

function render() {
  const s = state.status;
  if (!s) return;
  $("last-updated").textContent = `updated ${new Date(s.generated_at).toLocaleTimeString()}`;
  renderSummary(s.summary);
  renderAlertBanner(s.alerts || []);
  const fleet = $("fleet");
  fleet.innerHTML = "";
  for (const [id, h] of Object.entries(s.hosts)) fleet.appendChild(hostCard(id, h));
  applyGlobalFilter();
}

function renderSummary(sum) {
  if (!sum) return;
  const health = sum.health || "ok";
  $("summary").innerHTML = `
    <div class="stat-pill pill-health ${esc(health)}"><span>●</span><b>${esc(health.toUpperCase())}</b></div>
    <div class="stat-pill"><b>${sum.hosts_up}/${sum.hosts_total}</b><span class="cap">hosts up</span></div>
    <div class="stat-pill"><b>${sum.failed_services}</b><span class="cap">failed svc</span></div>
    <div class="stat-pill"><b>${sum.alerts_total}</b><span class="cap">alerts</span></div>`;
}

function renderAlertBanner(alerts) {
  const el = $("alert-banner");
  $("alert-count").textContent = alerts.length || "";
  if (!alerts.length) { el.hidden = true; return; }
  const crit = alerts.filter((a) => a.severity === "critical").length;
  el.className = "alert-banner" + (crit ? " critical" : "");
  el.innerHTML = `<span>${icon("services")}</span><span><b>${alerts.length} active alert${alerts.length > 1 ? "s" : ""}</b> — ${esc(alerts.slice(0, 3).map((a) => a.message).join(" · "))}${alerts.length > 3 ? " · …" : ""}</span>`;
  el.hidden = false;
}

/* --- host card -------------------------------------------------------------- */
function hostStatus(h) {
  if (!h.reachable) return "bad";
  const failed = (h.services || []).filter((u) => u.state === "failed").length;
  if (failed || h.stale || h.errors?.length) return "degraded";
  return "ok";
}

function hostCard(id, h) {
  const el = document.createElement("div");
  const st = hostStatus(h);
  el.className = "card" + (h.reachable ? "" : " unreachable") + (h.stale ? " stale" : "");
  const load = h.loadavg ? h.loadavg.map((x) => x.toFixed(2)).join(" / ") : "—";
  const mem = h.mem ? `${h.mem.used_gb} / ${h.mem.total_gb} GB` : "—";
  const memPct = h.mem && h.mem.total_gb ? (h.mem.used_gb / h.mem.total_gb) * 100 : 0;
  const disk0 = (h.disk || [{}])[0];
  const disk = disk0 ? `${disk0.used_gb} / ${disk0.size_gb} GB` : "—";

  el.innerHTML = `
    <div class="card-head">
      <span class="dot ${st}"></span>
      <h2>${esc(h.label || id)}</h2>
      <span class="role">${esc(h.role || "")}</span>
      <div class="host-meta">
        <div>${esc(h.os || "")}</div>
        <div>${esc(h.hostname || id)}</div>
        <div>ts ${esc(h.tailscale_ip || "—")}${h.rtt_ms ? ` · <span class="badge rtt">${h.rtt_ms}ms</span>` : ""}${h.stale ? ` · <span class="badge stale">stale</span>` : ""}</div>
      </div>
    </div>
    <div class="card-body">
      ${h.errors?.length ? `<div class="errbox">${esc(h.errors.join("\n"))}</div>` : ""}

      <div class="section"><h3>${icon("system")}<span>System</span><span class="chev">▾</span></h3><div class="section-content">
        <div class="stat-grid">
          <div class="stat-row"><span class="muted">uptime</span><span>${fmtUptime(h.uptime_secs)}</span></div>
          <div class="stat-row"><span class="muted">load 1/5/15</span><span>${esc(load)}</span></div>
          <div class="stat-row"><span class="muted">cpu</span><span>${esc(h.cpu?.cores || "—")}× ${esc(h.cpu?.model || "")}</span></div>
          <div class="stat-row"><span class="muted">mem</span><span>${esc(mem)}</span></div>
        </div>
        <div style="margin-top:6px"><span class="spark-label">load</span><div id="spark-${id}-load1">${sparkline(null)}</div></div>
        <div><span class="spark-label">mem %</span><div id="spark-${id}-mem">${sparkline(null)}</div></div>
        <div><span class="spark-label">disk %</span><div id="spark-${id}-disk">${sparkline(null)}</div></div>
        <div style="margin-top:6px"><div class="stat-row"><span class="muted">mem</span></div>${bar(memPct)}</div>
        <div class="stat-row" style="margin-top:4px"><span class="muted">disk</span><span>${esc(disk)}</span></div>
        ${bar(Number(disk0?.use_pct) || 0)}
      </div></div>

      ${renderServices(h, id)}
      ${renderDocker(h, id)}
      ${renderTopProcs(h)}
      ${renderAgents(h)}
      ${renderCron(h)}
      ${renderPorts(h)}
      ${renderBackups(h)}
      ${renderProjects(h)}
      ${renderLinks(h)}
      ${renderActions(id, h)}
    </div>`;
  loadSpark(id);
  return el;
}

function loadSpark(id) {
  const metrics = [["load1", "#4cc2ff"], ["mem", "#a78bfa"], ["disk", "#fbbf24"]];
  for (const [m, color] of metrics) {
    const metricKey = m === "mem" ? "mem_used_gb" : m === "disk" ? "disk_use_pct" : "load1";
    api(`/api/metrics?host=${id}&metric=${metricKey}&limit=60`)
      .then((d) => {
        const el = $(`spark-${id}-${m}`);
        if (el) el.innerHTML = sparkline((d.series || []).map((p) => p.v), color);
      })
      .catch(() => {});
  }
}

/* --- sections --------------------------------------------------------------- */
function section(title, iconName, content) {
  return `<div class="section"><h3>${icon(iconName)}<span>${title}</span><span class="chev">▾</span></h3>
    <div class="section-content">${content}</div></div>`;
}

function renderServices(h, id) {
  if (!h.services?.length) return "";
  const rows = h.services.map((u) =>
    `<tr><td class="mono">${esc(u.name)}</td><td>${chip(u.state)}</td><td class="ellipsis">${esc(u.desc || "")}</td>
     <td><button class="logbtn" data-log="${esc(id)}|unit|${esc(u.name)}">log</button></td></tr>`).join("");
  return section(`systemd user units (${h.services.length})`, "services",
    `<table class="tbl" data-sort><thead><tr><th data-k="name">unit</th><th data-k="state">state</th><th>description</th><th></th></tr></thead><tbody>${rows}</tbody></table>`);
}

function renderDocker(h, id) {
  if (!h.docker?.length) return "";
  const stats = Object.fromEntries((h.docker_stats || []).map((s) => [s.name, s]));
  const rows = h.docker.map((c) => {
    const st = stats[c.name];
    const state = c.status.includes("healthy") ? "healthy" : c.status.startsWith("Up") ? "running" : "inactive";
    return `<tr><td class="mono">${esc(c.name)}</td><td>${chip(state)}</td>
      <td class="mono">${esc(st ? st.cpu : "—")}</td><td class="mono">${esc(st ? st.mem_pct : "—")}</td>
      <td><button class="logbtn" data-log="${esc(id)}|container|${esc(c.name)}">log</button></td></tr>`;
  }).join("");
  return section(`docker containers (${h.docker.length})`, "containers",
    `<table class="tbl" data-sort><thead><tr><th data-k="name">container</th><th data-k="state">status</th><th>cpu</th><th>mem</th><th></th></tr></thead><tbody>${rows}</tbody></table>`);
}

function renderTopProcs(h) {
  if (!h.top_procs?.length) return "";
  const rows = h.top_procs.map((p) =>
    `<tr><td class="mono">${esc(p.pid)}</td><td class="mono">${esc(p.cpu)}</td><td class="mono">${esc(p.mem)}</td><td class="ellipsis">${esc(p.comm)}</td></tr>`).join("");
  return section("top processes", "procs",
    `<table class="tbl" data-sort><thead><tr><th data-k="pid">pid</th><th data-k="cpu">cpu%</th><th data-k="mem">mem%</th><th>command</th></tr></thead><tbody>${rows}</tbody></table>`);
}

function renderAgents(h) {
  if (!h.agents?.length) return "";
  const chips = h.agents.map((a) =>
    `<span class="agent-chip"><b>${esc(a.name)}</b>${a.port ? `<span class="pid">:${esc(a.port)}</span>` : ""}</span>`).join("");
  return section("agents", "agents", `<div>${chips}</div>`);
}

function renderCron(h) {
  if (!h.hermes_cron_jobs?.length && !h.crontab?.length) return "";
  const rows = (h.hermes_cron_jobs || []).map((j) =>
    `<tr><td class="ellipsis">${esc(j.name || "?")}</td><td class="mono">${esc(j.schedule || "")}</td>
     <td>${chip(j.enabled ? (j.state || "enabled") : "paused")}</td><td class="mono">${esc(j.last_status || "—")}</td></tr>`).join("");
  const crontab = h.crontab?.length
    ? `<div class="muted" style="font-size:10.5px;margin-top:4px">${h.crontab.slice(0, 6).map((l) => `<div class="mono">${esc(l)}</div>`).join("")}</div>` : "";
  return section(`hermes cron jobs (${(h.hermes_cron_jobs || []).length})`, "cron",
    `<table class="tbl" data-sort><thead><tr><th data-k="name">job</th><th data-k="schedule">schedule</th><th>state</th><th>last</th></tr></thead><tbody>${rows}</tbody></table>${crontab}`);
}

function renderPorts(h) {
  if (!h.ports?.length) return "";
  const interesting = h.ports.filter((p) => !p.addr.startsWith("127.0.0.53"));
  const rows = interesting.slice(0, 14).map((p) =>
    `<tr><td class="mono">${esc(p.addr)}</td><td class="ellipsis">${esc(p.proc)}</td></tr>`).join("");
  return section(`listening ports${interesting.length > 14 ? ` (${interesting.length})` : ""}`, "ports",
    `<table class="tbl" data-sort><thead><tr><th data-k="addr">addr</th><th>process</th></tr></thead><tbody>${rows}</tbody></table>`);
}

function renderBackups(h) {
  if (!h.restic_ages?.length) return "";
  const rows = h.restic_ages.map((r) =>
    `<tr><td class="mono">${esc(r.repo)}</td><td class="mono">${esc(r.latest ? new Date(r.latest).toLocaleString() : "unknown")}</td></tr>`).join("");
  return section("restic backups", "backup",
    `<table class="tbl"><thead><tr><th>repo</th><th>latest snapshot</th></tr></thead><tbody>${rows}</tbody></table>`);
}

function renderProjects(h) {
  if (!h.projects?.length) return "";
  const rows = h.projects.map((p) =>
    `<tr><td class="ellipsis"><b>${esc(p.name)}</b><div class="muted" style="font-size:10px">${esc(p.note || p.path || "")}</div></td>
     <td class="mono">${esc(p.branch || "—")}</td><td class="mono ellipsis">${esc(p.commit ? p.commit.slice(0, 30) : "—")}</td>
     <td>${p.dirty ? `<span class="chip dirty">dirty ${p.dirty}</span>` : ""}</td></tr>`).join("");
  return section(`projects (${h.projects.length})`, "projects",
    `<table class="tbl" data-sort><thead><tr><th data-k="name">project</th><th data-k="branch">branch</th><th>head</th><th></th></tr></thead><tbody>${rows}</tbody></table>`);
}

function renderLinks(h) {
  if (!h.links?.length) return "";
  const links = h.links.map((l) =>
    `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}<span class="reach">${esc(l.reach)}</span></a>`).join("");
  return section("links", "links", `<div class="links">${links}</div>`);
}

function renderActions(hostId, h) {
  const btns = (h.actions || []).map((a) =>
    `<button data-host="${esc(hostId)}" data-action="${esc(a.id)}" class="${/restart|reboot|poweroff|shutdown/i.test(a.id) ? "danger" : ""}" title="${esc(a.cmd)}">${esc(a.label)}</button>`).join("");
  const exec = h.exec?.length ? renderExec(hostId, h) : "";
  return section("control", "control", `<div class="actions">${btns}</div>${exec}`);
}

function renderExec(hostId, h) {
  const unitNames = (h.services || []).map((u) => u.name);
  const containerNames = (h.docker || []).map((c) => c.name);
  const verbs = (h.exec || []).map((e) => `<option value="${esc(e.id)}" data-arg="${esc(e.arg || "none")}">${esc(e.label)}</option>`).join("");
  const targets = [...unitNames.map((n) => `unit:${n}`), ...containerNames.map((n) => `container:${n}`)]
    .map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  return `<div class="exec-row">
    <select id="exec-${esc(hostId)}-verb">${verbs}</select>
    <select id="exec-${esc(hostId)}-target" style="${unitNames.length + containerNames.length ? "" : "display:none"}">${targets}</select>
    <button class="btn small" data-exec="${esc(hostId)}">run</button>
  </div>`;
}

/* --- control & exec actions ------------------------------------------------- */
async function runAction(host, actionId, btn) {
  const action = state.status?.hosts?.[host]?.actions?.find((a) => a.id === actionId);
  if (!/probe|status/i.test(actionId) && action) {
    const warn = action.disconnect_ok
      ? "\n⚠ This will REBOOT the host and drop its SSH connection.\n"
      : "";
    if (!confirm(`Run on ${host}: ${action.label}?\n\n${action.cmd}\n${warn}\nLive control action — will be audited.`)) return;
  }
  btn.disabled = true; btn.textContent = "…";
  try {
    const res = await api("/api/control", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, action_id: actionId }),
    });
    const bits = [res.ok ? "OK" : "FAILED"];
    if (res.stdout) bits.push(String(res.stdout).slice(0, 200));
    if (res.stderr) bits.push(String(res.stderr).slice(0, 200));
    if (res.note) bits.push(res.note);
    if (res.error) bits.push(res.error);
    toast(`${host} › ${action?.label || actionId}: ${bits.join(" — ")}`, !res.ok);
  } catch (e) { toast(`control failed: ${e}`, true); }
  finally { btn.disabled = false; btn.textContent = action?.label || actionId; setTimeout(refreshStatus, 1200); }
}

async function runExec(host) {
  const verbSel = $(`exec-${host}-verb`);
  const targetSel = $(`exec-${host}-target`);
  if (!verbSel) return;
  const verbId = verbSel.value;
  const argType = verbSel.options[verbSel.selectedIndex].dataset.arg || "none";
  let arg = "";
  if (argType !== "none" && targetSel) {
    const [type, name] = (targetSel.value || "").split(":");
    arg = name || "";
    if (!arg) return toast("select a target", true);
  }
  try {
    const res = await api("/api/exec", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, exec_id: verbId, arg }),
    });
    const out = res.stdout || res.stderr || res.error || "";
    toast(`${host} › ${verbId}: ${res.ok ? "OK" : "FAILED"}\n${String(out).slice(0, 600)}`, !res.ok);
  } catch (e) { toast(`exec failed: ${e}`, true); }
}

/* --- log tail ---------------------------------------------------------------- */
async function openLogs(host, type, target) {
  state.logTarget = { host, type, target };
  $("logs-drawer").hidden = false;
  $("logs-title").textContent = `${host} · ${type} · ${target}`;
  await fetchLogs();
}
async function fetchLogs() {
  const t = state.logTarget;
  if (!t) return;
  const body = $("logs-body");
  body.textContent = "loading…";
  try {
    const res = await api(`/api/logs?host=${encodeURIComponent(t.host)}&type=${t.type}&target=${encodeURIComponent(t.target)}&tail=200`);
    body.textContent = res.lines?.join("\n") || res.error || "(no output)";
  } catch (e) { body.textContent = `log fetch failed: ${e}`; }
}

/* --- drawers ------------------------------------------------------------------ */
async function loadAudit() {
  try {
    const d = await api("/api/audit");
    $("audit-body").innerHTML = d.entries.map((e) =>
      `<tr><td class="mono">${esc(e.ts)}</td><td>${esc(e.host)}</td>
       <td class="ellipsis">${esc(e.label || e.action_id)}</td>
       <td class="mono ellipsis">${esc(e.cmd || "")}</td>
       <td class="mono">${e.rc != null ? esc(e.rc) : ""}</td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">no control actions yet</td></tr>`;
  } catch (e) { $("audit-body").innerHTML = `<tr><td colspan="5" class="empty">audit unavailable</td></tr>`; }
}

function openDrawer(id) { $(id).hidden = false; }
function closeDrawers() {
  ["audit-drawer", "alerts-drawer", "logs-drawer"].forEach((id) => ($(id).hidden = true));
}

function renderAlertsDrawer() {
  const alerts = state.status?.alerts || [];
  $("alerts-body").innerHTML = alerts.length
    ? `<table class="tbl"><thead><tr><th>sev</th><th>host</th><th>message</th><th>since</th></tr></thead><tbody>
        ${alerts.map((a) => `<tr><td>${chip(a.severity)}</td><td>${esc(a.host)}</td><td>${esc(a.message)}</td><td class="mono">${esc(a.since)}</td></tr>`).join("")}
      </tbody></table>`
    : `<div class="empty">no active alerts</div>`;
}

/* --- table sort + global filter ------------------------------------------------ */
function attachTableSort() {
  document.querySelectorAll(".tbl[data-sort]").forEach((tbl) => {
    tbl.querySelectorAll("th[data-k]").forEach((th) => {
      th.addEventListener("click", () => {
        const tbody = tbl.querySelector("tbody");
        const key = th.dataset.k;
        const dir = th.dataset.dir === "asc" ? "desc" : "asc";
        tbl.querySelectorAll("th").forEach((x) => { x.classList.remove("sorted-asc", "sorted-desc"); });
        th.classList.add(dir === "asc" ? "sorted-asc" : "sorted-desc");
        th.dataset.dir = dir;
        const rows = [...tbody.rows].sort((a, b) => {
          const av = a.cells[th.cellIndex].dataset.v ?? a.cells[th.cellIndex].textContent.trim();
          const bv = b.cells[th.cellIndex].dataset.v ?? b.cells[th.cellIndex].textContent.trim();
          return av.localeCompare(bv, undefined, { numeric: true }) * (dir === "asc" ? 1 : -1);
        });
        rows.forEach((r) => tbody.appendChild(r));
      });
    });
  });
}

function applyGlobalFilter() {
  const q = $("global-search").value.trim().toLowerCase();
  document.querySelectorAll(".card").forEach((card) => {
    const rows = card.querySelectorAll("table.tbl tbody tr");
    let visible = 0;
    rows.forEach((tr) => {
      const hit = !q || tr.textContent.toLowerCase().includes(q);
      tr.style.display = hit ? "" : "none";
      if (hit) visible++;
    });
  });
}

/* --- toast ----------------------------------------------------------------------- */
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 7000);
}

/* --- events ---------------------------------------------------------------------- */
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (btn) return runAction(btn.dataset.host, btn.dataset.action, btn);
  const exec = ev.target.closest("button[data-exec]");
  if (exec) return runExec(exec.dataset.exec);
  const logbtn = ev.target.closest("button[data-log]");
  if (logbtn) {
    const [host, type, target] = logbtn.dataset.log.split("|");
    return openLogs(host, type, target);
  }
  const drawerClose = ev.target.closest(".drawer-close");
  if (drawerClose) return closeDrawers();
  // collapse section headers
  const h3 = ev.target.closest(".section h3");
  if (h3) return h3.closest(".section").classList.toggle("collapsed");
  const alertBanner = ev.target.closest("#alert-banner");
  if (alertBanner) { renderAlertsDrawer(); openDrawer("alerts-drawer"); }
});

$("btn-refresh").addEventListener("click", () => refreshStatus());
$("btn-audit").addEventListener("click", async () => { openDrawer("audit-drawer"); loadAudit(); });
$("btn-alerts").addEventListener("click", () => { renderAlertsDrawer(); openDrawer("alerts-drawer"); });
$("btn-logs-refresh").addEventListener("click", fetchLogs);
$("btn-logs-close").addEventListener("click", closeDrawers);
$("btn-collapse").addEventListener("click", () => {
  const sections = document.querySelectorAll(".section");
  const allCollapsed = [...sections].every((s) => s.classList.contains("collapsed"));
  sections.forEach((s) => s.classList.toggle("collapsed", !allCollapsed));
});
$("global-search").addEventListener("input", applyGlobalFilter);
$("auto-refresh").addEventListener("change", (e) => {
  if (e.target.checked) startAutoRefresh(); else clearInterval(state.timer);
});

function startAutoRefresh() {
  clearInterval(state.timer);
  state.timer = setInterval(() => refreshStatus(), REFRESH_MS);
}

refreshStatus();
startAutoRefresh();
setInterval(attachTableSort, 16000); // re-attach sort after each re-render
attachTableSort();
