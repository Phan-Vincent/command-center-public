/* Command Center frontend — vanilla JS, zero dependencies. */
"use strict";

const REFRESH_MS = 15000;
const PREFS_KEY = "command-center.ui.v1";
const DEFAULTS = { density: "comfortable", collapsed: [], sort: {}, autoRefresh: true };
const MOBILE_MQ = "(max-width: 700px)";

const state = {
  status: null,
  token: null,
  mode: "all",              // session-only: "all" | "attention"
  hostFilter: "all",        // session-only: "all" | host id
  mobileHost: null,         // session-only: host shown by the mobile selector
  issues: [],
  issuesByHost: new Map(),
  issueIndex: new Map(),
  prefs: loadPrefs(),
  refreshing: false,
  queuedForce: false,
  confirmPending: false,
  countdown: REFRESH_MS / 1000,
  countdownTimer: null,
  auditOpen: false,
  logTarget: null,
  lastFocus: null,
  mq: window.matchMedia(MOBILE_MQ),
};

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* --- preferences ----------------------------------------------------------- */
function loadPrefs() {
  const base = { ...DEFAULTS, collapsed: [], sort: {} };
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return base;
    const p = JSON.parse(raw);
    if (!p || typeof p !== "object") return base;
    if (p.density === "compact" || p.density === "comfortable") base.density = p.density;
    if (Array.isArray(p.collapsed)) {
      base.collapsed = p.collapsed.filter((x) => typeof x === "string").slice(0, 500);
    }
    if (p.sort && typeof p.sort === "object") {
      for (const [k, v] of Object.entries(p.sort)) {
        if (v && typeof v === "object" && typeof v.key === "string" &&
            (v.dir === "asc" || v.dir === "desc")) {
          base.sort[k] = { key: v.key, dir: v.dir };
        }
      }
    }
    if (typeof p.autoRefresh === "boolean") base.autoRefresh = p.autoRefresh;
  } catch (e) {
    /* corrupted storage -> defaults */
  }
  return base;
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      density: state.prefs.density,
      collapsed: state.prefs.collapsed,
      sort: state.prefs.sort,
      autoRefresh: state.prefs.autoRefresh,
    }));
  } catch (e) {
    /* storage unavailable or full — keep UI working in-memory */
  }
}

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
  scanner: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M3 10h6M15 10h6"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/>',
};
function icon(name, size = 14) {
  const inner = ICONS[name] || "";
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex:none" aria-hidden="true">${inner}</svg>`;
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
  const cls = ["active", "running", "healthy", "unhealthy", "failed", "inactive", "waiting", "dead", "error", "scheduled", "critical"].includes(s) ? s : "";
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

/* --- issue derivation ------------------------------------------------------ */
const SEV_RANK = { crit: 0, warn: 1, info: 2 };

function makeIssue(key, kind, hostId, hostLabel, title, severity, sectionKey) {
  return { key, kind, hostId, hostLabel, title, severity, sectionKey };
}

function alertTitle(hostId, hostLabel, message) {
  const m = message || "alert";
  if (m.includes(hostId) || m.includes(hostLabel)) return m;
  return `${hostLabel}: ${m}`;
}

function svcFailedUnit(hostId, message) {
  const m = message || "";
  const prefix = `${hostId}: `;
  if (!m.startsWith(prefix)) return null;
  const rest = m.slice(prefix.length);
  const suffix = " is failed";
  if (!rest.endsWith(suffix) || rest.length === suffix.length) return null;
  return rest.slice(0, -suffix.length);
}

function containerIssue(c) {
  const st = String(c.status || "").toLowerCase();
  if (st.includes("unhealthy")) return true;
  return !st.startsWith("up");
}

function containerChipState(c) {
  const st = String(c.status || "").toLowerCase();
  if (st.includes("unhealthy")) return "unhealthy";
  if (st.includes("(healthy)")) return "healthy";
  if (st.startsWith("up")) return "running";
  return "inactive";
}

function hostIssues(id, h) {
  const label = h.label || id;
  const out = [];
  const add = (key, kind, title, severity, sectionKey) =>
    out.push(makeIssue(key, kind, id, label, title, severity, sectionKey));

  if (!h.reachable) add(`unreachable:${id}`, "unreachable", `${label} unreachable`, "crit", "system");
  if (h.stale) add(`stale:${id}`, "stale", `${label} stale (showing last-known-good)`, "warn", "system");
  (h.errors || []).forEach((msg) => {
    add(`error:${id}:${msg}`, "host-error", `${label}: ${msg}`, "crit", "errors");
  });
  (h.services || []).forEach((u) => {
    if (u.state === "failed") add(`svc:${id}:${u.name}`, "service", `${label} · ${u.name} failed`, "crit", "services");
  });
  (h.docker || []).forEach((c) => {
    if (containerIssue(c)) {
      const st = String(c.status || "").toLowerCase();
      const kind = st.includes("unhealthy") ? "unhealthy" : "stopped";
      add(`container:${id}:${c.name}:${kind}`, "container", `${label} · ${c.name} ${kind}`, kind === "unhealthy" ? "crit" : "warn", "docker");
    }
  });
  return out;
}

function deriveIssues(s) {
  const issues = [];
  const seen = new Set();
  const add = (iss) => {
    if (seen.has(iss.key)) return;
    seen.add(iss.key);
    issues.push(iss);
  };
  (s.alerts || []).forEach((a) => {
    const hostId = a.host || "?";
    const hostLabel = (s.hosts && s.hosts[hostId] && (s.hosts[hostId].label || hostId)) || hostId;
    const sev = a.severity === "critical" ? "crit" : a.severity === "warning" ? "warn" : "info";
    if (a.rule === "host-down") {
      add(makeIssue(`unreachable:${hostId}`, "alert", hostId, hostLabel, alertTitle(hostId, hostLabel, a.message), sev, "system"));
      return;
    }
    if (a.rule === "svc-failed") {
      const unit = svcFailedUnit(hostId, a.message);
      if (unit) {
        add(makeIssue(`svc:${hostId}:${unit}`, "alert", hostId, hostLabel, alertTitle(hostId, hostLabel, a.message), sev, "services"));
        return;
      }
    }
    add(makeIssue(`alert:${hostId}:${a.severity || ""}:${a.message || ""}`, "alert", hostId, hostLabel, alertTitle(hostId, hostLabel, a.message), sev, "system"));
  });
  for (const [id, h] of Object.entries(s.hosts || {})) {
    hostIssues(id, h).forEach(add);
  }
  issues.sort((a, b) =>
    (SEV_RANK[a.severity] - SEV_RANK[b.severity]) ||
    String(a.hostLabel).localeCompare(String(b.hostLabel)) ||
    String(a.title).localeCompare(String(b.title)));
  state.issues = issues;
  state.issuesByHost = new Map();
  state.issueIndex = new Map();
  for (const iss of issues) {
    if (!state.issuesByHost.has(iss.hostId)) state.issuesByHost.set(iss.hostId, []);
    state.issuesByHost.get(iss.hostId).push(iss);
    state.issueIndex.set(iss.key, iss);
  }
}

/* --- main render pipeline --------------------------------------------------- */
async function refreshStatus(force = false) {
  if (state.refreshing) {
    if (force) state.queuedForce = true;
    return;
  }
  state.refreshing = true;
  updateRefreshUI();
  try {
    state.status = await api(force ? "/api/status?force=1" : "/api/status");
    state.countdown = REFRESH_MS / 1000;
    render();
  } catch (e) {
    toast(`status fetch failed: ${e}`, true);
  } finally {
    state.refreshing = false;
    updateRefreshUI();
    if (state.queuedForce) {
      state.queuedForce = false;
      refreshStatus(true);
    }
  }
}

function render() {
  if (state.confirmPending) return; // never re-render while a confirmation is up
  const s = state.status;
  if (!s) return;
  $("last-updated").textContent = `updated ${new Date(s.generated_at).toLocaleTimeString()}`;
  renderSummary(s.summary);
  renderAlertBanner(s.alerts || []);
  deriveIssues(s);
  renderHostChips(s);
  renderMobileSelect(s);
  renderIssueQueue();
  const fleet = $("fleet");
  fleet.innerHTML = "";
  for (const [id, h] of Object.entries(s.hosts)) fleet.appendChild(hostCard(id, h));
  restoreSortState();
  applyFilters();
  updateRefreshUI();
}

function renderSummary(sum) {
  if (!sum) return;
  const health = sum.health || "ok";
  $("summary").innerHTML = `
    <div class="stat-pill pill-health ${esc(health)}"><span aria-hidden="true">●</span><b>${esc(health.toUpperCase())}</b></div>
    <div class="stat-pill"><b>${esc(sum.hosts_up)}/${esc(sum.hosts_total)}</b><span class="cap">hosts up</span></div>
    <div class="stat-pill"><b>${esc(sum.failed_services)}</b><span class="cap">failed svc</span></div>
    <div class="stat-pill"><b>${esc(sum.alerts_total)}</b><span class="cap">alerts</span></div>`;
}

function renderAlertBanner(alerts) {
  const el = $("alert-banner");
  $("alert-count").textContent = alerts.length || "";
  if (!alerts.length) { el.hidden = true; return; }
  const crit = alerts.filter((a) => a.severity === "critical").length;
  el.className = "alert-banner" + (crit ? " critical" : "");
  el.innerHTML = `<span aria-hidden="true">${icon("services")}</span><span><b>${esc(alerts.length)} active alert${alerts.length > 1 ? "s" : ""}</b> — ${esc(alerts.slice(0, 3).map((a) => a.message).join(" · "))}${alerts.length > 3 ? " · …" : ""}</span>`;
  el.hidden = false;
}

function renderHostChips(s) {
  const hosts = Object.keys(s.hosts);
  const all = `<button type="button" class="host-chip${state.hostFilter === "all" ? " active" : ""}" data-host-chip="all" aria-pressed="${state.hostFilter === "all"}">all</button>`;
  const rest = hosts.map((id) =>
    `<button type="button" class="host-chip${state.hostFilter === id ? " active" : ""}" data-host-chip="${esc(id)}" aria-pressed="${state.hostFilter === id}" title="${esc((s.hosts[id] && s.hosts[id].label) || id)}">${esc(id)}</button>`).join("");
  $("host-chips").innerHTML = all + rest;
}

function renderMobileSelect(s) {
  const hosts = Object.keys(s.hosts);
  if (!state.mobileHost || (state.mobileHost !== "all" && !s.hosts[state.mobileHost])) state.mobileHost = hosts[0] || "all";
  const sel = $("host-select");
  sel.innerHTML = `<option value="all"${state.mobileHost === "all" ? " selected" : ""}>all hosts</option>` +
    hosts.map((id) =>
      `<option value="${esc(id)}"${state.mobileHost === id ? " selected" : ""}>${esc((s.hosts[id] && s.hosts[id].label) || id)}</option>`).join("");
}

function renderIssueQueue() {
  const list = $("issues-list");
  $("issues-count").textContent = String(state.issues.length);
  if (!state.issues.length) {
    list.innerHTML = `<li class="issues-empty">no active issues</li>`;
    return;
  }
  list.innerHTML = state.issues.map((iss) =>
    `<li><button type="button" class="issue-row" data-issue-key="${esc(iss.key)}" data-issue-host="${esc(iss.hostId)}"
       aria-label="Focus ${esc(iss.hostLabel)}: ${esc(iss.title)}">
      <span class="issue-sev ${esc(iss.severity)}" aria-hidden="true"></span>
      <span class="issue-host">${esc(iss.hostLabel)}</span>
      <span class="issue-text">${esc(iss.title)}</span>
      <span class="issue-focus" aria-hidden="true">→</span>
    </button></li>`).join("");
}

/* --- host card -------------------------------------------------------------- */
function secId(hostId, key) { return `sec-${hostId}-${key}`; }

function hostStatus(h) {
  if (!h.reachable) return "bad";
  const failed = (h.services || []).filter((u) => u.state === "failed").length;
  const scanner = h.scanner_health || {};
  const scannerFailed = scanner.available && (
    scanner.source_health?.ok === false ||
    Object.values(scanner.services || {}).some((value) => value !== "active")
  );
  if (failed || scannerFailed || h.stale || h.errors?.length) return "degraded";
  return "ok";
}

function hostCard(id, h) {
  const el = document.createElement("div");
  const st = hostStatus(h);
  const issues = state.issuesByHost.get(id) || [];
  el.className = "card" + (h.reachable ? "" : " unreachable") + (h.stale ? " stale" : "") + (issues.length ? " has-issues" : "");
  el.dataset.host = id;
  el.tabIndex = -1;

  el.innerHTML = `
    <div class="card-head">
      <span class="dot ${st}" aria-hidden="true"></span>
      <h2>${esc(h.label || id)}</h2>
      <span class="role">${esc(h.role || "")}</span>
      <div class="host-meta">
        <div>${esc(h.os || "")}</div>
        <div>${esc(h.hostname || id)}</div>
        <div>ts ${esc(h.tailscale_ip || "—")}${h.rtt_ms ? ` · <span class="badge rtt">${esc(h.rtt_ms)}ms</span>` : ""}${h.stale ? ` · <span class="badge stale">stale</span>` : ""}</div>
      </div>
      <span class="search-hint" hidden></span>
      <button type="button" class="btn small host-actions-btn" data-host-actions="${esc(id)}"
        aria-haspopup="dialog" aria-label="Open actions for ${esc(h.label || id)}">
        ${icon("control", 14)}<span class="host-actions-label">Actions</span>
      </button>
    </div>
    <div class="card-body">
      ${h.errors?.length ? section(secId(id, "errors"), `host errors (${esc(h.errors.length)})`, "error", `<div class="errbox">${esc(h.errors.join("\n"))}</div>`) : ""}
      ${renderSystem(h, id)}
      ${renderScanner(h, id)}
      ${renderServices(h, id)}
      ${renderDocker(h, id)}
      ${renderTopProcs(h, id)}
      ${renderAgents(h, id)}
      ${renderCron(h, id)}
      ${renderPorts(h, id)}
      ${renderBackups(h, id)}
      ${renderProjects(h, id)}
      ${renderLinks(h, id)}
    </div>`;
  loadSpark(id);
  return el;
}

function loadSpark(id) {
  const metrics = [["load1", "#4cc2ff"], ["mem", "#a78bfa"], ["disk", "#fbbf24"]];
  for (const [m, color] of metrics) {
    const metricKey = m === "mem" ? "mem_used_gb" : m === "disk" ? "disk_use_pct" : "load1";
    api(`/api/metrics?host=${encodeURIComponent(id)}&metric=${metricKey}&limit=60`)
      .then((d) => {
        const el = $(`spark-${id}-${m}`);
        if (el) el.innerHTML = sparkline((d.series || []).map((p) => p.v), color);
      })
      .catch(() => {});
  }
}

/* --- sections --------------------------------------------------------------- */
function section(id, title, iconName, content) {
  const collapsed = state.prefs.collapsed.includes(id);
  const contentId = `${id}-content`;
  return `<section id="${esc(id)}" class="section${collapsed ? " collapsed" : ""}" data-section-id="${esc(id)}">
    <h3><button type="button" class="section-toggle" aria-expanded="${collapsed ? "false" : "true"}" aria-controls="${esc(contentId)}">
      ${icon(iconName)}<span>${esc(title)}</span><span class="chev" aria-hidden="true">▾</span>
    </button></h3>
    <div class="section-content" id="${esc(contentId)}">${content}</div>
  </section>`;
}

function renderSystem(h, id) {
  const load = h.loadavg ? h.loadavg.map((x) => x.toFixed(2)).join(" / ") : "—";
  const mem = h.mem ? `${h.mem.used_gb} / ${h.mem.total_gb} GB` : "—";
  const memPct = h.mem && h.mem.total_gb ? (h.mem.used_gb / h.mem.total_gb) * 100 : 0;
  const disk0 = (h.disk || [{}])[0];
  const disk = disk0 ? `${disk0.used_gb} / ${disk0.size_gb} GB` : "—";
  return section(secId(id, "system"), "System", "system", `
    <div class="stat-grid">
      <div class="stat-row"><span class="muted">uptime</span><span>${fmtUptime(h.uptime_secs)}</span></div>
      <div class="stat-row"><span class="muted">load 1/5/15</span><span>${esc(load)}</span></div>
      <div class="stat-row"><span class="muted">cpu</span><span>${esc(h.cpu?.cores || "—")}× ${esc(h.cpu?.model || "")}</span></div>
      <div class="stat-row"><span class="muted">mem</span><span>${esc(mem)}</span></div>
    </div>
    <div style="margin-top:6px"><span class="spark-label">load</span><div id="spark-${esc(id)}-load1">${sparkline(null)}</div></div>
    <div><span class="spark-label">mem %</span><div id="spark-${esc(id)}-mem">${sparkline(null)}</div></div>
    <div><span class="spark-label">disk %</span><div id="spark-${esc(id)}-disk">${sparkline(null)}</div></div>
    <div style="margin-top:6px"><div class="stat-row"><span class="muted">mem</span></div>${bar(memPct)}</div>
    <div class="stat-row" style="margin-top:4px"><span class="muted">disk</span><span>${esc(disk)}</span></div>
    ${bar(Number(disk0?.use_pct) || 0)}`);
}

function renderScanner(h, id) {
  const p = h.scanner_health;
  if (!p) return "";
  if (!p.available) {
    return section(secId(id, "scanner"), "Scanner", "scanner",
      `<div class="errbox">status unavailable${p.errors?.length ? `: ${esc(p.errors.join(" · "))}` : ""}</div>`);
  }
  const run = p.last_run || {};
  const source = p.source_health || {};
  const sourceRows = Object.entries(source.sources || {}).map(([name, row]) => {
    const active = Number(row.attempted || 0) - Number(row.ignored || 0);
    const usable = Number(row.usable || 0);
    const rate = active ? usable / active : null;
    const latest = (source.latest || {})[name] || {};
    const state = active >= 10 && rate < 0.10 ? "critical" : "healthy";
    return `<tr><td class="mono">${esc(name)}</td><td>${chip(state)}</td>
      <td class="mono">${esc(usable)}/${esc(active)}${rate == null ? "" : ` (${esc((rate * 100).toFixed(1))}%)`}</td>
      <td class="mono">${esc(latest.status || "—")}</td></tr>`;
  }).join("");
  const failures = (p.source_failures || []).map((row) =>
    `${row.source}:${row.status}×${row.count}`).join(" · ") || "none";
  const drift = p.drift || {};
  const rr = drift.repository_runtime || {};
  const rs = drift.repository_skill || {};
  const paper = p.paper_experiment || {};
  const services = p.services || {};
  return section(secId(id, "scanner"), "Scanner health", "scanner", `
    <div class="stat-grid">
      <div class="stat-row"><span class="muted">last successful run</span><span class="mono">${esc(p.last_success_at || "none")}</span></div>
      <div class="stat-row"><span class="muted">next timer</span><span class="mono">${esc(p.next_scheduled_run || "unknown")}</span></div>
      <div class="stat-row"><span class="muted">last run</span><span>${chip(run.status || "unknown")} <span class="mono">${esc(run.ended_at || run.started_at || "")}</span></span></div>
      <div class="stat-row"><span class="muted">candidates / abstentions</span><span><b>${esc(p.candidate_count ?? 0)}</b> / <b>${esc(p.abstention_count ?? 0)}</b></span></div>
      <div class="stat-row"><span class="muted">Interaction handler / tunnel</span><span>${chip(services.interaction_handler || "unknown")} ${chip(services.tunnel || "unknown")}</span></div>
      <div class="stat-row"><span class="muted">Marketplace browser</span><span>${chip(p.marketplace_auth?.state || "unknown")} ${esc(p.marketplace_auth?.basis || "")}</span></div>
      <div class="stat-row"><span class="muted">paper experiment</span><span>${chip(paper.status || "none")} <span class="mono">${esc(paper.experiment_id || "")}</span></span></div>
      <div class="stat-row"><span class="muted">source failures</span><span class="mono ellipsis">${esc(failures)}</span></div>
      <div class="stat-row"><span class="muted">repo ↔ runtime drift</span><span>${esc(rr.divergent || 0)} divergent · ${esc(rr.repository_only || 0)} repo-only · ${esc(rr.runtime_only || 0)} runtime-only</span></div>
      <div class="stat-row"><span class="muted">repo ↔ skill drift</span><span>${esc(rs.divergent || 0)} divergent · ${esc(rs.repository_only || 0)} repo-only · ${esc(rs.runtime_only || 0)} runtime-only</span></div>
    </div>
    <div class="tbl-wrap" style="margin-top:8px"><table class="tbl"><thead><tr>
      <th>pricing source</th><th>health</th><th>usable / active</th><th>latest</th>
    </tr></thead><tbody>${sourceRows}</tbody></table></div>
    ${p.errors?.length ? `<div class="errbox" style="margin-top:8px">${esc(p.errors.join(" · "))}</div>` : ""}`);
}

function renderServices(h, id) {
  if (!h.services?.length) return "";
  const sortKey = `${id}:services`;
  const rows = h.services.map((u) =>
    `<tr><td class="mono">${esc(u.name)}</td><td>${chip(u.state)}</td><td class="ellipsis">${esc(u.desc || "")}</td>
     <td><button type="button" class="logbtn" data-log-host="${esc(id)}" data-log-type="unit" data-log-target="${esc(u.name)}">log</button></td></tr>`).join("");
  return section(secId(id, "services"), `systemd user units (${esc(h.services.length)})`, "services",
    `<div class="tbl-wrap"><table class="tbl" data-sort="${esc(sortKey)}"><thead><tr>
      <th><button type="button" class="th-sort" data-k="name">unit</button></th>
      <th><button type="button" class="th-sort" data-k="state">state</button></th>
      <th>description</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function renderDocker(h, id) {
  if (!h.docker?.length) return "";
  const sortKey = `${id}:docker`;
  const stats = Object.fromEntries((h.docker_stats || []).map((s) => [s.name, s]));
  const rows = h.docker.map((c) => {
    const st = stats[c.name];
    const state = containerChipState(c);
    return `<tr><td class="mono">${esc(c.name)}</td><td>${chip(state)}</td>
      <td class="mono">${esc(st ? st.cpu : "—")}</td><td class="mono">${esc(st ? st.mem_pct : "—")}</td>
      <td><button type="button" class="logbtn" data-log-host="${esc(id)}" data-log-type="container" data-log-target="${esc(c.name)}">log</button></td></tr>`;
  }).join("");
  return section(secId(id, "docker"), `docker containers (${esc(h.docker.length)})`, "containers",
    `<div class="tbl-wrap"><table class="tbl" data-sort="${esc(sortKey)}"><thead><tr>
      <th><button type="button" class="th-sort" data-k="name">container</button></th>
      <th><button type="button" class="th-sort" data-k="state">status</button></th>
      <th>cpu</th><th>mem</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function renderTopProcs(h, id) {
  if (!h.top_procs?.length) return "";
  const sortKey = `${id}:procs`;
  const rows = h.top_procs.map((p) =>
    `<tr><td class="mono">${esc(p.pid)}</td><td class="mono" data-v="${esc(p.cpu)}">${esc(p.cpu)}</td><td class="mono" data-v="${esc(p.mem)}">${esc(p.mem)}</td><td class="ellipsis">${esc(p.comm)}</td></tr>`).join("");
  return section(secId(id, "procs"), "top processes", "procs",
    `<div class="tbl-wrap"><table class="tbl" data-sort="${esc(sortKey)}"><thead><tr>
      <th><button type="button" class="th-sort" data-k="pid">pid</button></th>
      <th><button type="button" class="th-sort" data-k="cpu">cpu%</button></th>
      <th><button type="button" class="th-sort" data-k="mem">mem%</button></th>
      <th>command</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function renderAgents(h, id) {
  if (!h.agents?.length) return "";
  const chips = h.agents.map((a) =>
    `<span class="agent-chip"><b>${esc(a.name)}</b>${a.port ? `<span class="pid">:${esc(a.port)}</span>` : ""}</span>`).join("");
  return section(secId(id, "agents"), "agents", "agents", `<div>${chips}</div>`);
}

function renderCron(h, id) {
  if (!h.hermes_cron_jobs?.length && !h.crontab?.length) return "";
  const sortKey = `${id}:cron`;
  const rows = (h.hermes_cron_jobs || []).map((j) =>
    `<tr><td class="ellipsis">${esc(j.name || "?")}</td><td class="mono">${esc(j.schedule || "")}</td>
     <td>${chip(j.enabled ? (j.state || "enabled") : "paused")}</td><td class="mono">${esc(j.last_status || "—")}</td></tr>`).join("");
  const crontab = h.crontab?.length
    ? `<div class="muted" style="font-size:10.5px;margin-top:4px">${h.crontab.slice(0, 6).map((l) => `<div class="mono">${esc(l)}</div>`).join("")}</div>` : "";
  return section(secId(id, "cron"), `hermes cron jobs (${esc((h.hermes_cron_jobs || []).length)})`, "cron",
    `<div class="tbl-wrap"><table class="tbl" data-sort="${esc(sortKey)}"><thead><tr>
      <th><button type="button" class="th-sort" data-k="name">job</button></th>
      <th><button type="button" class="th-sort" data-k="schedule">schedule</button></th>
      <th>state</th><th>last</th></tr></thead><tbody>${rows}</tbody></table></div>${crontab}`);
}

function renderPorts(h, id) {
  if (!h.ports?.length) return "";
  const sortKey = `${id}:ports`;
  const interesting = h.ports.filter((p) => !String(p.addr || "").startsWith("127.0.0.53"));
  const rows = interesting.slice(0, 14).map((p) =>
    `<tr><td class="mono">${esc(p.addr)}</td><td class="ellipsis">${esc(p.proc)}</td></tr>`).join("");
  return section(secId(id, "ports"), `listening ports${interesting.length > 14 ? ` (${esc(interesting.length)})` : ""}`, "ports",
    `<div class="tbl-wrap"><table class="tbl" data-sort="${esc(sortKey)}"><thead><tr>
      <th><button type="button" class="th-sort" data-k="addr">addr</button></th>
      <th>process</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function renderBackups(h, id) {
  if (!h.restic_ages?.length) return "";
  const rows = h.restic_ages.map((r) =>
    `<tr><td class="mono">${esc(r.repo)}</td><td class="mono">${esc(r.latest ? new Date(r.latest).toLocaleString() : "unknown")}</td></tr>`).join("");
  return section(secId(id, "backups"), "restic backups", "backup",
    `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>repo</th><th>latest snapshot</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function renderProjects(h, id) {
  if (!h.projects?.length) return "";
  const sortKey = `${id}:projects`;
  const rows = h.projects.map((p) =>
    `<tr><td class="ellipsis"><b>${esc(p.name)}</b><div class="muted" style="font-size:10px">${esc(p.note || p.path || "")}</div></td>
     <td class="mono">${esc(p.branch || "—")}</td><td class="mono ellipsis">${esc(p.commit ? p.commit.slice(0, 30) : "—")}</td>
     <td>${p.dirty ? `<span class="chip dirty">dirty ${esc(p.dirty)}</span>` : ""}</td></tr>`).join("");
  return section(secId(id, "projects"), `projects (${esc(h.projects.length)})`, "projects",
    `<div class="tbl-wrap"><table class="tbl" data-sort="${esc(sortKey)}"><thead><tr>
      <th><button type="button" class="th-sort" data-k="name">project</button></th>
      <th><button type="button" class="th-sort" data-k="branch">branch</button></th>
      <th>head</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function safeExternalLink(l) {
  const label = l.label || l.url || "link";
  const reachHtml = l.reach ? `<span class="reach">${esc(l.reach)}</span>` : "";
  try {
    const u = new URL(String(l.url || ""), document.baseURI);
    if (u.protocol === "http:" || u.protocol === "https:") {
      return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">${esc(label)}${reachHtml}</a>`;
    }
  } catch (e) {
    /* invalid URL -> non-clickable text below */
  }
  return `<span class="linktext">${esc(label)}${reachHtml}</span>`;
}

function renderLinks(h, id) {
  if (!h.links?.length) return "";
  const links = h.links.map((l) => safeExternalLink(l)).join("");
  return section(secId(id, "links"), "links", "links", `<div class="links">${links}</div>`);
}

/* --- host action drawer ------------------------------------------------------ */
function openActionDrawer(hostId, trigger) {
  const h = state.status?.hosts?.[hostId];
  if (!h) return;
  state.lastFocus = {
    node: trigger || document.activeElement,
    hostId: trigger ? trigger.dataset.hostActions : null,
  };
  renderActionDrawer(hostId);
  setActionOutput("", false);
  $("action-backdrop").hidden = false;
  $("action-drawer").hidden = false;
  document.body.classList.add("drawer-open");
  $("action-drawer-close").focus();
}

function closeActionDrawer() {
  $("action-drawer").hidden = true;
  $("action-backdrop").hidden = true;
  document.body.classList.remove("drawer-open");
  const saved = state.lastFocus;
  let target = saved && saved.node;
  if (saved && saved.hostId) {
    const current = [...document.querySelectorAll("[data-host-actions]")]
      .find((b) => b.dataset.hostActions === saved.hostId);
    if (current) target = current;
  }
  if (target && typeof target.focus === "function" && document.contains(target)) target.focus();
  state.lastFocus = null;
}

function actionGroup(title, items) {
  return `<div class="action-group"><h3>${esc(title)}</h3><div class="actions">${items}</div></div>`;
}

function renderActionButton(hostId, a) {
  const danger = /restart|reboot|poweroff|shutdown/i.test(a.id);
  return `<button type="button" class="btn${danger ? " danger" : ""}" data-host="${esc(hostId)}" data-action="${esc(a.id)}" title="${esc(a.cmd)}">${esc(a.label)}</button>`;
}

function isProbeAction(a) {
  const cmd = String(a?.cmd || "").replace(/\s+/g, " ").trim().toLowerCase();
  return /^systemctl --user is-active(?: |$)/.test(cmd) || /^pgrep(?: |$)/.test(cmd);
}

function renderExecRow(hostId, h) {
  const unitNames = (h.services || []).map((u) => u.name);
  const containerNames = (h.docker || []).map((c) => c.name);
  const verbs = (h.exec || []).map((e) => `<option value="${esc(e.id)}" data-arg="${esc(e.arg || "none")}">${esc(e.label)}</option>`).join("");
  const targets = [...unitNames.map((n) => `unit:${n}`), ...containerNames.map((n) => `container:${n}`)]
    .map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  const hasTargets = unitNames.length + containerNames.length > 0;
  return `<div class="exec-row">
    <select id="exec-${esc(hostId)}-verb" aria-label="Exec command">${verbs}</select>
    <select id="exec-${esc(hostId)}-target" aria-label="Exec target"${hasTargets ? "" : " hidden"}>${targets}</select>
    <button type="button" class="btn small" data-exec="${esc(hostId)}">run</button>
  </div>`;
}

function renderActionDrawer(hostId) {
  const h = state.status?.hosts?.[hostId];
  if (!h) return;
  $("action-drawer-title").textContent = `Actions · ${h.label || hostId}`;
  const actions = h.actions || [];
  const probes = actions.filter(isProbeAction);
  const disruptive = actions.filter((a) => /reboot|poweroff|shutdown/i.test(a.id));
  const serviceActions = actions.filter((a) => !isProbeAction(a) && !/reboot|poweroff|shutdown/i.test(a.id));
  let html = "";
  if (probes.length) html += actionGroup("Read-only probes", probes.map((a) => renderActionButton(hostId, a)).join(""));
  if (h.exec?.length) html += actionGroup("Read-only exec", renderExecRow(hostId, h));
  if (serviceActions.length) html += actionGroup("Service actions", serviceActions.map((a) => renderActionButton(hostId, a)).join(""));
  if (disruptive.length) html += actionGroup("Disruptive host actions", disruptive.map((a) => renderActionButton(hostId, a)).join(""));
  if (!html) html = `<div class="empty">no actions or exec commands available</div>`;
  $("action-drawer-body").innerHTML = html;
}

function setActionOutput(msg, isErr) {
  const out = $("action-output");
  out.textContent = msg;
  out.classList.toggle("err", !!isErr);
}

/* --- control & exec actions ------------------------------------------------- */
async function runAction(host, actionId, btn) {
  const action = state.status?.hosts?.[host]?.actions?.find((a) => a.id === actionId);
  const label = action?.label || actionId;
  const isProbe = action ? isProbeAction(action) : false;
  if (!isProbe) {
    const warn = action?.disconnect_ok
      ? "\n⚠ This will REBOOT the host and drop its SSH connection.\n"
      : "";
    state.confirmPending = true;
    let ok = false;
    try {
      ok = confirm(`Run on ${host}: ${label}?\n\n${action?.cmd || actionId}\n${warn}\nLive control action — will be audited.`);
    } finally {
      state.confirmPending = false;
    }
    if (!ok) return;
  }
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  setActionOutput(`Running ${label} on ${host}…`, false);
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
    const msg = `${host} › ${label}: ${bits.join(" — ")}`;
    setActionOutput(msg, !res.ok);
    toast(msg, !res.ok);
  } catch (e) {
    const msg = `control failed: ${e}`;
    setActionOutput(msg, true);
    toast(msg, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    setTimeout(() => refreshStatus(true), 1200);
  }
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
    if (!arg) {
      const msg = "select a target";
      setActionOutput(msg, true);
      toast(msg, true);
      return;
    }
  }
  setActionOutput(`Running ${verbId} on ${host}…`, false);
  try {
    const res = await api("/api/exec", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, exec_id: verbId, arg }),
    });
    const out = res.stdout || res.stderr || res.error || "";
    const msg = `${host} › ${verbId}: ${res.ok ? "OK" : "FAILED"}\n${String(out).slice(0, 600)}`;
    setActionOutput(msg, !res.ok);
    toast(`${host} › ${verbId}: ${res.ok ? "OK" : "FAILED"}\n${String(out).slice(0, 600)}`, !res.ok);
  } catch (e) {
    const msg = `exec failed: ${e}`;
    setActionOutput(msg, true);
    toast(msg, true);
  }
}

/* --- log tail ---------------------------------------------------------------- */
async function openLogs(host, type, target, trigger) {
  state.logTarget = { host, type, target };
  state.lastFocus = { node: trigger || document.activeElement, hostId: null };
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

function openDrawer(id, trigger) {
  state.lastFocus = { node: trigger || document.activeElement, hostId: null };
  $(id).hidden = false;
}
function closeDrawers() {
  ["audit-drawer", "alerts-drawer", "logs-drawer"].forEach((id) => ($(id).hidden = true));
  const saved = state.lastFocus;
  state.lastFocus = null;
  if (saved && saved.node && typeof saved.node.focus === "function" && document.contains(saved.node)) {
    saved.node.focus();
  }
}

function renderAlertsDrawer() {
  const alerts = state.status?.alerts || [];
  $("alerts-body").innerHTML = alerts.length
    ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>sev</th><th>host</th><th>message</th><th>since</th></tr></thead><tbody>
        ${alerts.map((a) => `<tr><td>${chip(a.severity)}</td><td>${esc(a.host)}</td><td>${esc(a.message)}</td><td class="mono">${esc(a.since)}</td></tr>`).join("")}
      </tbody></table></div>`
    : `<div class="empty">no active alerts</div>`;
}

/* --- table sort ---------------------------------------------------------------- */
function sortTable(tbl, key, dir) {
  const tbody = tbl.querySelector("tbody");
  if (!tbody) return;
  const headButtons = [...tbl.querySelectorAll("button[data-k]")];
  const th = headButtons.find((b) => b.dataset.k === key)?.closest("th");
  if (!th) return;
  const colIndex = th.cellIndex;
  const rows = [...tbody.rows];
  rows.sort((a, b) => {
    const av = a.cells[colIndex]?.dataset.v ?? a.cells[colIndex]?.textContent.trim() ?? "";
    const bv = b.cells[colIndex]?.dataset.v ?? b.cells[colIndex]?.textContent.trim() ?? "";
    return av.localeCompare(bv, undefined, { numeric: true }) * (dir === "asc" ? 1 : -1);
  });
  rows.forEach((r) => tbody.appendChild(r));
  tbl.querySelectorAll("th").forEach((x) => { x.classList.remove("sorted-asc", "sorted-desc"); });
  th.classList.add(dir === "asc" ? "sorted-asc" : "sorted-desc");
}

function restoreSortState() {
  document.querySelectorAll("table[data-sort]").forEach((tbl) => {
    const sortKey = tbl.dataset.sort;
    const saved = state.prefs.sort[sortKey];
    if (saved && saved.key && (saved.dir === "asc" || saved.dir === "desc")) {
      sortTable(tbl, saved.key, saved.dir);
    }
  });
}

function handleSortClick(btn) {
  const tbl = btn.closest("table[data-sort]");
  if (!tbl) return;
  const key = btn.dataset.k;
  const sortKey = tbl.dataset.sort;
  const cur = state.prefs.sort[sortKey];
  const dir = (cur && cur.key === key && cur.dir === "asc") ? "desc" : "asc";
  state.prefs.sort[sortKey] = { key, dir };
  savePrefs();
  sortTable(tbl, key, dir);
}

/* --- sections: collapse / expand --------------------------------------------- */
function toggleSection(btn) {
  const sec = btn.closest(".section");
  if (!sec) return;
  const id = sec.dataset.sectionId;
  const collapsed = sec.classList.toggle("collapsed");
  btn.setAttribute("aria-expanded", String(!collapsed));
  if (collapsed) {
    if (!state.prefs.collapsed.includes(id)) state.prefs.collapsed.push(id);
  } else {
    state.prefs.collapsed = state.prefs.collapsed.filter((x) => x !== id);
  }
  savePrefs();
}

function collapseAll() {
  const sections = [...document.querySelectorAll(".section")];
  const anyOpen = sections.some((s) => !s.classList.contains("collapsed"));
  sections.forEach((s) => {
    s.classList.toggle("collapsed", anyOpen);
    const btn = s.querySelector(".section-toggle");
    if (btn) btn.setAttribute("aria-expanded", String(!anyOpen));
  });
  state.prefs.collapsed = anyOpen ? sections.map((s) => s.dataset.sectionId) : [];
  savePrefs();
}

/* --- search + filters ---------------------------------------------------------- */
function hostSearchText(id, h) {
  const parts = [id, h.label, h.role, h.os, h.hostname, h.tailscale_ip, h.cpu?.model, h.cpu?.cores];
  (h.actions || []).forEach((a) => parts.push(a.id, a.label, a.cmd));
  (h.exec || []).forEach((e) => parts.push(e.id, e.label, e.cmd));
  (h.projects || []).forEach((p) => parts.push(p.name, p.note, p.path, p.branch, p.commit));
  (h.links || []).forEach((l) => parts.push(l.label, l.url, l.reach));
  (h.agents || []).forEach((a) => parts.push(a.name, a.port, a.note));
  return parts.join(" ").toLowerCase();
}

function isAttentionSection(sec, hostId) {
  const id = sec.dataset.sectionId;
  if (!id) return true;
  if (id === secId(hostId, "system") || id === secId(hostId, "errors")) return true;
  const issues = state.issuesByHost.get(hostId) || [];
  return issues.some((iss) => secId(iss.hostId, iss.sectionKey) === id);
}

function filterSectionContent(sec, q) {
  sec.querySelectorAll("tbody tr").forEach((tr) => {
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
  });
  sec.querySelectorAll(".agent-chip, .links a").forEach((el) => {
    el.style.display = el.textContent.toLowerCase().includes(q) ? "" : "none";
  });
}

function resetSectionContent(sec) {
  sec.querySelectorAll("tbody tr, .agent-chip, .links a").forEach((el) => {
    el.style.display = "";
  });
}

function applyFilters() {
  const q = $("global-search").value.trim().toLowerCase();
  const attention = state.mode === "attention";
  const hostFilter = state.hostFilter;
  const isMobile = state.mq.matches;
  state.mobileHost = $("host-select").value || state.mobileHost;

  document.querySelectorAll(".card[data-host]").forEach((card) => {
    const hostId = card.dataset.host;
    const h = state.status?.hosts?.[hostId];
    if (!h) return;
    const issues = state.issuesByHost.get(hostId) || [];
    const cardText = card.textContent.toLowerCase();
    const extra = hostSearchText(hostId, h);
    const cardMatchesQ = !q || cardText.includes(q) || extra.includes(q);
    const hostVisible =
      (!attention || issues.length > 0) &&
      (hostFilter === "all" || hostFilter === hostId) &&
      (!isMobile || state.mobileHost === "all" || state.mobileHost === hostId) &&
      cardMatchesQ;
    card.style.display = hostVisible ? "" : "none";

    const hint = card.querySelector(".search-hint");
    if (hint) hint.hidden = !(q && !cardText.includes(q) && extra.includes(q));
    if (hint) hint.textContent = q && !cardText.includes(q) && extra.includes(q) ? "matches host actions" : "";

    card.querySelectorAll(".section").forEach((sec) => {
      const sectionText = sec.textContent.toLowerCase();
      const secHasQ = !q || sectionText.includes(q);
      const headerHasQ = !!q && (sec.querySelector(".section-toggle")?.textContent || "").toLowerCase().includes(q);
      let show;
      if (q) show = secHasQ;
      else if (attention) show = isAttentionSection(sec, hostId);
      else show = true;
      sec.style.display = show ? "" : "none";
      sec.classList.toggle("open-for-search", !!q && secHasQ);
      if (q && show && !headerHasQ) filterSectionContent(sec, q);
      else resetSectionContent(sec);
    });
  });

  document.querySelectorAll(".issue-row").forEach((row) => {
    const host = row.dataset.issueHost;
    // The mobile host selector scopes the card view, not the global triage queue.
    // Issue clicks switch the mobile card to the affected host in focusIssue().
    row.style.display = hostFilter === "all" || hostFilter === host ? "" : "none";
  });
}

/* --- focus an issue ------------------------------------------------------------- */
function focusIssue(key) {
  const iss = state.issueIndex.get(key);
  if (!iss) return;
  if (state.hostFilter !== "all" && state.hostFilter !== iss.hostId) setHostFilter(iss.hostId);
  if (state.mq.matches) {
    state.mobileHost = iss.hostId;
    if ($("host-select")) $("host-select").value = iss.hostId;
  }
  applyFilters();
  const card = [...document.querySelectorAll(".card[data-host]")].find((c) => c.dataset.host === iss.hostId);
  if (!card) return;
  if (iss.sectionKey) {
    const sec = document.getElementById(secId(iss.hostId, iss.sectionKey));
    if (sec) {
      sec.classList.remove("collapsed");
      const btn = sec.querySelector(".section-toggle");
      if (btn) btn.setAttribute("aria-expanded", "true");
      state.prefs.collapsed = state.prefs.collapsed.filter((x) => x !== sec.dataset.sectionId);
      savePrefs();
    }
  }
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  card.focus({ preventScroll: true });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1800);
}

/* --- view mode + host filter ------------------------------------------------------ */
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("[data-mode]").forEach((btn) => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  applyFilters();
}

function setHostFilter(hostId) {
  state.hostFilter = hostId;
  if (hostId !== "all") state.mobileHost = hostId;
  document.querySelectorAll("[data-host-chip]").forEach((btn) => {
    const active = btn.dataset.hostChip === hostId;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  const sel = $("host-select");
  if (sel && hostId !== "all") sel.value = hostId;
  applyFilters();
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

/* --- refresh lifecycle -------------------------------------------------------------- */
function updateRefreshUI() {
  const btn = $("btn-refresh");
  btn.disabled = state.refreshing;
  btn.classList.toggle("busy", state.refreshing);
  btn.setAttribute("aria-label", state.refreshing ? "Refreshing…" : "Refresh status now");
  const c = $("refresh-countdown");
  if (state.refreshing) c.textContent = "refreshing…";
  else if (state.prefs.autoRefresh) c.textContent = `auto ${Math.max(0, state.countdown)}s`;
  else c.textContent = "auto off";
}

function startAutoRefresh() {
  clearInterval(state.countdownTimer);
  state.countdownTimer = null;
  if (!state.prefs.autoRefresh) {
    updateRefreshUI();
    return;
  }
  state.countdown = REFRESH_MS / 1000;
  state.countdownTimer = setInterval(() => {
    if (state.refreshing || state.confirmPending || document.hidden) return;
    state.countdown -= 1;
    if (state.countdown <= 0) {
      state.countdown = REFRESH_MS / 1000;
      refreshStatus(false);
    } else {
      updateRefreshUI();
    }
  }, 1000);
}

/* --- focus trap for action drawer --------------------------------------------------- */
function trapActionDrawerFocus(ev) {
  if (ev.key !== "Tab") return;
  const drawer = $("action-drawer");
  if (drawer.hidden) return;
  const focusables = [...drawer.querySelectorAll(
    'button:not([disabled]), select:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')];
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault();
    first.focus();
  }
}

/* --- delegated events ---------------------------------------------------------------- */
document.addEventListener("click", (ev) => {
  const thSort = ev.target.closest("button[data-k]");
  if (thSort) return handleSortClick(thSort);

  const toggle = ev.target.closest(".section-toggle");
  if (toggle) return toggleSection(toggle);

  const issueBtn = ev.target.closest("[data-issue-key]");
  if (issueBtn) return focusIssue(issueBtn.dataset.issueKey);

  const actionBtn = ev.target.closest("button[data-action]");
  if (actionBtn) return runAction(actionBtn.dataset.host, actionBtn.dataset.action, actionBtn);

  const execBtn = ev.target.closest("button[data-exec]");
  if (execBtn) return runExec(execBtn.dataset.exec);

  const logbtn = ev.target.closest("button[data-log-host]");
  if (logbtn) return openLogs(logbtn.dataset.logHost, logbtn.dataset.logType, logbtn.dataset.logTarget, logbtn);

  const hostActions = ev.target.closest("[data-host-actions]");
  if (hostActions) return openActionDrawer(hostActions.dataset.hostActions, hostActions);

  const modeBtn = ev.target.closest("[data-mode]");
  if (modeBtn) return setMode(modeBtn.dataset.mode);

  const chipBtn = ev.target.closest("[data-host-chip]");
  if (chipBtn) return setHostFilter(chipBtn.dataset.hostChip);

  const drawerClose = ev.target.closest(".drawer-close");
  if (drawerClose) return closeDrawers();

  const alertBanner = ev.target.closest("#alert-banner");
  if (alertBanner) { renderAlertsDrawer(); openDrawer("alerts-drawer", alertBanner); }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    if (!$("action-drawer").hidden) return closeActionDrawer();
    if (!$("audit-drawer").hidden || !$("alerts-drawer").hidden || !$("logs-drawer").hidden) closeDrawers();
    return;
  }
  trapActionDrawerFocus(ev);
});

/* --- specific controls ---------------------------------------------------------------- */
$("btn-refresh").addEventListener("click", () => refreshStatus(true));
$("btn-audit").addEventListener("click", async () => { openDrawer("audit-drawer", $("btn-audit")); loadAudit(); });
$("btn-alerts").addEventListener("click", () => { renderAlertsDrawer(); openDrawer("alerts-drawer", $("btn-alerts")); });
$("btn-logs-refresh").addEventListener("click", fetchLogs);
$("btn-logs-close").addEventListener("click", closeDrawers);
$("btn-collapse").addEventListener("click", collapseAll);
$("global-search").addEventListener("input", applyFilters);
$("host-select").addEventListener("change", (e) => {
  state.mobileHost = e.target.value;
  if (state.hostFilter !== "all") setHostFilter(e.target.value);
  else applyFilters();
});
$("action-backdrop").addEventListener("click", closeActionDrawer);
$("action-drawer-close").addEventListener("click", closeActionDrawer);
$("btn-density").addEventListener("click", () => {
  state.prefs.density = state.prefs.density === "compact" ? "comfortable" : "compact";
  document.body.classList.toggle("compact", state.prefs.density === "compact");
  $("btn-density").textContent = state.prefs.density === "compact" ? "comfortable" : "compact";
  savePrefs();
});
$("auto-refresh").addEventListener("change", (e) => {
  state.prefs.autoRefresh = e.target.checked;
  savePrefs();
  startAutoRefresh();
});
function handleMqChange() {
  $("mobile-host-wrap").hidden = !state.mq.matches;
  $("host-chips").hidden = state.mq.matches;
  applyFilters();
}
if (state.mq.addEventListener) state.mq.addEventListener("change", handleMqChange);
else if (state.mq.addListener) state.mq.addListener(handleMqChange);

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearInterval(state.countdownTimer);
    state.countdownTimer = null;
  } else if (state.prefs.autoRefresh) {
    startAutoRefresh();
  }
});

/* --- init ------------------------------------------------------------------------------ */
document.body.classList.toggle("compact", state.prefs.density === "compact");
$("btn-density").textContent = state.prefs.density === "compact" ? "comfortable" : "compact";
$("auto-refresh").checked = state.prefs.autoRefresh;
$("mobile-host-wrap").hidden = !state.mq.matches;
$("host-chips").hidden = state.mq.matches;
refreshStatus(false);
startAutoRefresh();
