// RockSentinel Console v0: review proposals, browse the golden set, see eval results.
// All text is inserted with textContent (never innerHTML) because logs are untrusted.
"use strict";

const state = { proposals: [], categories: [], selected: null };

const $ = (sel) => document.querySelector(sel);

// Theme: dark by default (design system); light is opt-in and remembered.
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = document.querySelector("#theme-toggle");
  if (btn) btn.textContent = theme === "dark" ? "Light theme" : "Dark theme";
}
applyTheme(localStorage.getItem("rs-theme") || "dark");
document.querySelector("#theme-toggle")?.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("rs-theme", next);
  applyTheme(next);
});
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 2200);
}
const badge = (cat) => el("span", { class: `badge ${cat || "unknown"}` }, cat || "unknown");
const where = (c) => [c.component, c.gpu_family, c.platform].map((x) => x || "?").join(" · ");

// --- tabs ---------------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === `view-${tab.dataset.view}`));
  if (tab.dataset.view === "golden") loadGolden();
  if (tab.dataset.view === "evals") loadEvals();
  if (tab.dataset.view === "runs") loadRuns();
  if (tab.dataset.view === "signatures") loadSignatures();
}));

// --- review -------------------------------------------------------------------------------
async function loadProposals(selectId) {
  const data = await api("/api/proposals");
  state.proposals = data.items; state.categories = data.categories;
  $("#count-review").textContent = data.count;
  const list = $("#proposal-list"); list.replaceChildren();
  if (!data.items.length) {
    list.append(el("p", { class: "empty", style: "padding:12px" },
      "No pending proposals. Run: python -m rocksentinel.evals.labeler propose"));
    $("#proposal-detail").replaceChildren(el("p", { class: "empty" }, "Nothing to review."));
    return;
  }
  for (const p of data.items) {
    list.append(el("div", { class: "item", "data-id": p.case_id, onclick: () => select(p.case_id) },
      el("div", { class: "title" }, badge(p.category), " ", p.component || p.workflow),
      el("div", { class: "sub" }, `${p.gpu_family || "?"} · ${p.platform || "?"} · run ${p.run_id}`),
      el("div", { class: "sub" }, el("span", { class: `chip ${p.label_strength}` },
        p.label_strength === "strong" ? "outcome label" : "rule label: verify"))));
  }
  const keep = data.items.find((p) => p.case_id === selectId) || data.items[0];
  select(keep.case_id);
}

async function select(caseId) {
  state.selected = state.proposals.find((p) => p.case_id === caseId);
  document.querySelectorAll(".item").forEach((i) =>
    i.classList.toggle("selected", i.dataset.id === caseId));
  const p = state.selected;
  const detail = $("#proposal-detail");
  const category = el("select", { id: "category" },
    state.categories.map((c) => el("option", c === p.category ? { value: c, selected: "" } : { value: c }, c)));
  const note = el("input", { type: "text", id: "note", placeholder: "Review note (what you checked)" });
  const ctx = p.context || {};
  detail.replaceChildren(
    el("h2", {}, badge(p.category), " ", p.component || "(no component)", " ",
      el("span", { class: "muted" }, `· ${p.gpu_family || "?"} · ${p.platform || "?"}`)),
    el("div", { class: "meta" }, `${p.job_name} (${p.workflow}) · run ${p.run_id} · job ${p.job_id} · `,
      el("a", { href: p.job_url, target: "_blank", rel: "noopener" }, "full log on GitHub ↗")),
    el("span", { class: `chip ${p.label_strength}` }, p.label_source || ""),
    el("dl", { class: "facts" },
      el("dt", {}, "Failing test"), el("dd", {}, el("code", {}, p.failing_test || "—")),
      el("dt", {}, "Faulting kernel"), el("dd", {}, el("code", {}, p.failing_kernel || "—")),
      el("dt", {}, "Failing since"), el("dd", {}, ctx.failing_since
        ? `${ctx.failing_since.finished_at} (${ctx.failing_since.streak_runs} runs)` : "—"),
      el("dt", {}, "Same-run co-failures"), el("dd", {}, `${ctx.co_failures ?? "—"}`,
        ctx.platform_event_hint ? el("span", { class: "chip weak", style: "margin-left:6px" },
          "platform-event hint") : null)),
    el("h3", {}, "Evidence (highlighted = must-cite lines)"),
    el("div", { class: "log", id: "log" }, el("p", { class: "empty", style: "padding:8px" }, "Loading…")),
    el("div", { class: "actions" },
      el("label", {}, "Category ", category), note,
      el("button", { class: "btn primary", id: "accept", onclick: () => decide("accept") }, "Accept"),
      el("button", { class: "btn danger", id: "reject", onclick: () => decide("reject") }, "Reject"),
      el("span", { class: "kbd" }, "keys: A accept · R reject · J/K next/prev")));
  try {
    const ev = await api(`/api/cases/${encodeURIComponent(caseId)}/evidence`);
    if (state.selected?.case_id !== caseId) return;
    const log = $("#log"); log.replaceChildren();
    if (ev.fallback) {
      log.append(el("p", { class: "fallback" },
        "No error lines recognised in this log, so this is just the end of the log (not evidence). " +
        "Open the full log on GitHub; if it shows a real failure, the extractor needs a new pattern."));
    }
    for (const seg of ev.segments) {
      log.append(el("div", { class: "seg" }, seg.lines.map((l) =>
        el("div", { class: `ln${l.cite ? " cite" : ""}` }, el("span", { class: "n" }, l.n),
          el("span", {}, l.text)))));
    }
    if (!ev.segments.length) log.append(el("p", { class: "empty", style: "padding:8px" }, "No evidence lines."));
  } catch (e) { $("#log").replaceChildren(el("p", { class: "empty", style: "padding:8px" }, e.message)); }
}

async function decide(action) {
  const p = state.selected; if (!p) return;
  const idx = state.proposals.findIndex((x) => x.case_id === p.case_id);
  const next = state.proposals[idx + 1] || state.proposals[idx - 1];
  document.querySelectorAll(".actions button").forEach((b) => (b.disabled = true));
  try {
    if (action === "accept") {
      const chosen = $("#category").value;
      const body = { note: $("#note").value };
      if (chosen !== p.category) body.category = chosen;
      const res = await api(`/api/proposals/${encodeURIComponent(p.case_id)}/accept`,
        { method: "POST", body: JSON.stringify(body) });
      toast(`Accepted as ${res.case_id} [${res.category}]`);
    } else {
      await api(`/api/proposals/${encodeURIComponent(p.case_id)}/reject`, { method: "POST", body: "{}" });
      toast(`Rejected ${p.case_id}`);
    }
    await loadProposals(next?.case_id);
    refreshGoldenCount();
  } catch (e) {
    toast(`Error: ${e.message}`);
    document.querySelectorAll(".actions button").forEach((b) => (b.disabled = false));
  }
}

document.addEventListener("keydown", (e) => {
  if (!$("#view-review").classList.contains("active")) return;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  const idx = state.proposals.findIndex((x) => x.case_id === state.selected?.case_id);
  if (e.key === "a") decide("accept");
  else if (e.key === "r") decide("reject");
  else if (e.key === "j" && state.proposals[idx + 1]) select(state.proposals[idx + 1].case_id);
  else if (e.key === "k" && idx > 0) select(state.proposals[idx - 1].case_id);
});

// --- golden ---------------------------------------------------------------------------------
async function refreshGoldenCount() {
  const g = await api("/api/golden"); $("#count-golden").textContent = g.count; return g;
}
async function loadGolden() {
  const g = await refreshGoldenCount();
  $("#golden-summary").textContent = `${g.count} cases · ${g.confirmed} confirmed by a human`;
  const head = el("tr", {}, ["Case", "Status", "Category", "Where", "Failing test / kernel", "Label source", "Log"]
    .map((h) => el("th", {}, h)));
  const rows = g.items.map((c) => el("tr", {},
    el("td", {}, c.case_id), el("td", {}, c.status), el("td", {}, badge(c.category)),
    el("td", {}, where(c)), el("td", {}, el("code", {}, c.failing_test || c.failing_kernel || "—")),
    el("td", { class: "muted" }, c.review_note || c.label_source || ""),
    el("td", {}, c.job_url ? el("a", { href: c.job_url, target: "_blank", rel: "noopener" }, "log ↗") : "—")));
  $("#golden-table").replaceChildren(el("thead", {}, head), el("tbody", {}, rows));
}

// --- evals ----------------------------------------------------------------------------------
$("#confirmed-only").addEventListener("change", loadEvals);
$("#split").addEventListener("change", loadEvals);
async function loadEvals() {
  $("#evals-summary").textContent = "Running…";
  const params = new URLSearchParams({ split: $("#split").value });
  if ($("#confirmed-only").checked) params.set("confirmed", "1");
  const r = await api(`/api/evals?${params}`);
  $("#evals-summary").textContent = `${r.n_cases} cases (${r.n_confirmed} confirmed)` +
    (r.n_cases < 30 ? " · small set: treat numbers as indicative only" : "");
  const systems = Object.keys(r.systems);
  const metrics = systems.length ? Object.keys(r.systems[systems[0]]) : [];
  const head = el("tr", {}, el("th", {}, "Metric"), systems.map((s) => el("th", {}, s)));
  const rows = metrics.map((m) => {
    const vals = systems.map((s) => r.systems[s][m]);
    const best = Math.max(...vals.filter((v) => v !== null));
    return el("tr", {}, el("td", {}, m), vals.map((v) =>
      el("td", { class: `num${v !== null && v === best && vals.filter((x) => x === best).length === 1 ? " best" : ""}` },
        v === null ? "—" : v.toFixed(2))));
  });
  $("#evals-table").replaceChildren(el("thead", {}, head), el("tbody", {}, rows));
  const chead = el("tr", {}, ["Case", "Expected", ...systems.map((s) => `${s} (grounding)`)].map((h) => el("th", {}, h)));
  const crows = r.cases.map((c) => el("tr", {}, el("td", {}, c.case_id), el("td", {}, badge(c.expected_category)),
    systems.map((s) => el("td", {}, badge(c[s].category), ` ${c[s].grounding.toFixed(2)}`))));
  $("#evals-cases").replaceChildren(el("thead", {}, chead), el("tbody", {}, crows));
}

// --- runs: workflow cards → a workflow's runs → run detail → case ---------------------------
// Dates are UTC days; "On a day" sends since = until, "Between dates" sends either or both.
const runs = { level: "workflows", workflow: null, runId: null, mode: "day",
  dataRange: {}, wfItems: [], runItems: [], timer: null };

const when = (iso) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");
function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const utcDay = (offsetDays = 0) =>
  new Date(Date.now() - offsetDays * 86400000).toISOString().slice(0, 10);
const tone = (failed, rate) => (!failed ? "good" : rate >= 0.5 ? "bad" : "warn");

function dayParams() {
  const from = $("#date-from").value || null;
  const to = runs.mode === "day" ? from : ($("#date-to").value || null);
  const p = new URLSearchParams();
  if (from) p.set("since", from);
  if (to) p.set("until", to);
  return p;
}
function describeDays() {
  const from = $("#date-from").value, to = $("#date-to").value;
  if (runs.mode === "day" || (from && from === to)) return from ? `on ${from}` : "all dates";
  if (from && to) return `${from} → ${to}`;
  if (from) return `from ${from}`;
  return to ? `until ${to}` : "all dates";
}
function setMode(mode) {
  runs.mode = mode;
  document.querySelectorAll(".seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
    b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  });
  $("#field-to").hidden = mode === "day";
  $("#lbl-from").textContent = mode === "day" ? "Day" : "From";
}
function setDateBounds(range) {
  runs.dataRange = range || {};
  for (const id of ["#date-from", "#date-to"]) {
    if (range?.first) $(id).min = range.first;
    if (range?.last) $(id).max = utcDay() > range.last ? utcDay() : range.last;
  }
}

document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  setMode(b.dataset.mode);
  if (b.dataset.mode === "day") $("#date-to").value = "";
  loadRuns();
}));
$("#date-from").addEventListener("change", () => {
  const f = $("#date-from"), t = $("#date-to");
  if (runs.mode === "range" && t.value && f.value > t.value) t.value = f.value;
  loadRuns();
});
$("#date-to").addEventListener("change", () => {
  const f = $("#date-from"), t = $("#date-to");
  if (f.value && t.value && t.value < f.value) f.value = t.value;
  loadRuns();
});
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
  const p = b.dataset.preset;
  if (p === "all") { setMode("day"); $("#date-from").value = ""; $("#date-to").value = ""; }
  else if (p === "latest") { setMode("day"); $("#date-from").value = runs.dataRange.last || utcDay(); }
  else { setMode("range"); $("#date-from").value = utcDay(Number(p) - 1); $("#date-to").value = utcDay(); }
  loadRuns();
}));
$("#runs-search").addEventListener("input", () =>
  (runs.level === "workflows" ? renderWorkflows() : renderRunList()));
$("#failing-only").addEventListener("change", () =>
  (runs.level === "workflows" ? renderWorkflows() : renderRunList()));

// Auto-refresh: re-read the job store (the poller keeps adding runs) without losing the
// open run or case.
function scheduleRefresh() {
  clearInterval(runs.timer);
  runs.timer = setInterval(() => {
    if (!$("#auto-refresh").checked || document.hidden) return;
    if (!$("#view-runs").classList.contains("active")) return;
    loadRuns({ refresh: true }).catch(() => {});
  }, 60000);
}
scheduleRefresh();

function renderCrumbs() {
  const c = $("#runs-crumbs");
  const link = (label, onclick) => el("button", { type: "button", class: "crumb", onclick }, label);
  const parts = [runs.level === "workflows"
    ? el("span", { class: "crumb current", "aria-current": "page" }, "All workflows")
    : link("All workflows", () => goWorkflows())];
  if (runs.workflow) {
    parts.push(el("span", { class: "sep", "aria-hidden": "true" }, "›"),
      runs.runId ? link(runs.workflow, () => openWorkflow(runs.workflow))
        : el("span", { class: "crumb current", "aria-current": "page" }, runs.workflow));
  }
  if (runs.runId) {
    parts.push(el("span", { class: "sep", "aria-hidden": "true" }, "›"),
      el("span", { class: "crumb current", "aria-current": "page" }, `run ${runs.runId}`));
  }
  if (runs.level !== "workflows") parts.push(el("span", { class: "kbd" }, "Esc: back"));
  c.replaceChildren(...parts);
  $("#runs-search").placeholder = runs.level === "workflows"
    ? "Filter workflows…" : "Filter runs (id, branch, commit)…";
}

function stat(label, value, cls = "") {
  return el("div", { class: `stat ${cls}` }, el("div", { class: "stat-v" }, value),
    el("div", { class: "stat-l" }, label));
}
const emptyBox = (...msg) => el("div", { class: "panel empty-state" }, ...msg);

async function loadRuns(opts = {}) {
  renderCrumbs();
  try {
    if (runs.level === "workflows") await loadWorkflows();
    else await loadWorkflowRuns(opts.refresh);
    $("#runs-updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) { toast(`Error: ${e.message}`); }
}

function goWorkflows() {
  runs.level = "workflows"; runs.workflow = null; runs.runId = null;
  $("#runs-search").value = "";
  loadRuns();
}
function openWorkflow(workflow, runId = null) {
  const fresh = runs.workflow !== workflow || runs.level !== "workflow";
  runs.level = "workflow"; runs.workflow = workflow; runs.runId = runId;
  if (fresh) {
    $("#runs-search").value = "";
    $("#runs-body").replaceChildren(el("div", { class: "split" },
      el("aside", { class: "list", id: "run-list", "aria-label": `Runs of ${workflow}` }),
      el("article", { class: "detail", id: "run-detail" },
        el("p", { class: "empty" }, "Select a run to see which jobs failed, grouped by runner pool."))));
  }
  loadRuns();
}

// Level 1: one card per workflow.
async function loadWorkflows() {
  const r = await api(`/api/workflows?${dayParams()}`);
  setDateBounds(r.data_range);
  runs.wfItems = r.items;
  renderWorkflows();
}

function renderWorkflows() {
  const all = runs.wfItems;
  const q = $("#runs-search").value.trim().toLowerCase();
  const failingOnly = $("#failing-only").checked;
  const items = all.filter((w) => (!q || w.workflow.toLowerCase().includes(q)) &&
    (!failingOnly || w.failed_runs));
  const t = all.reduce((a, w) => ({ runs: a.runs + w.runs, failed: a.failed + w.failed_runs,
    jobs: a.jobs + w.jobs, fj: a.fj + w.failed_jobs }), { runs: 0, failed: 0, jobs: 0, fj: 0 });
  $("#runs-stats").replaceChildren(
    stat("Workflows", all.length), stat("Runs", t.runs),
    stat("Runs with failures", t.failed, t.failed ? "bad" : "good"),
    stat("Failed jobs", `${t.fj} / ${t.jobs}`), stat("Dates (UTC)", describeDays()));
  const body = $("#runs-body");
  if (!all.length) {
    const dr = runs.dataRange;
    body.replaceChildren(emptyBox(el("h3", {}, "No runs " + describeDays()),
      el("p", { class: "muted" }, dr.first
        ? `The job store has runs from ${dr.first} to ${dr.last} (UTC). Pick a day in that range or choose "All dates".`
        : "The job store is empty. Run: python -m rocksentinel.ingest.poller")));
    return;
  }
  if (!items.length) {
    body.replaceChildren(emptyBox(el("p", { class: "muted" }, "No workflows match the filter.")));
    return;
  }
  body.replaceChildren(el("div", { class: "cards" }, items.map(workflowCard)));
}

function workflowCard(w) {
  const pct = Math.round(w.failure_rate * 100);
  const cls = tone(w.failed_runs, w.failure_rate);
  const open = () => openWorkflow(w.workflow);
  const ticks = [...w.recent].reverse().map((r) => el("button", {
    type: "button", class: `tick ${r.failed ? "bad" : "good"}`,
    title: `run ${r.run_id} · ${r.failed}/${r.jobs} jobs failed · ${when(r.started_at)}`,
    "aria-label": `Open run ${r.run_id}: ${r.failed ? `${r.failed} jobs failed` : "passed"}`,
    onclick: (e) => { e.stopPropagation(); openWorkflow(w.workflow, r.run_id); } }));
  return el("article", { class: `wf-card ${cls}`, tabindex: "0", "aria-label": `Open ${w.workflow}`,
    onclick: open,
    onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); } } },
    el("div", { class: "wf-head" }, el("h3", {}, w.workflow),
      el("span", { class: `rate ${cls}` }, w.failed_runs ? `${pct}% failing` : "all passing")),
    el("div", { class: "bar", role: "img", "aria-label": `${pct}% of runs had failures` },
      el("span", { style: `width:${pct}%` })),
    el("div", { class: "wf-nums" },
      el("span", {}, el("b", {}, w.runs), " runs"),
      el("span", {}, el("b", {}, w.failed_runs), " with failures"),
      el("span", {}, el("b", {}, w.failed_jobs), ` / ${w.jobs} jobs failed`)),
    el("div", { class: "strip-label" }, `Last ${ticks.length} runs (oldest → newest, click one to open)`),
    el("div", { class: "strip" }, ticks),
    el("div", { class: "sub" }, `last run ${ago(w.last_run_at)} · ${when(w.last_run_at)}`));
}

// Level 2: the runs of one workflow, grouped by day; detail on the right.
async function loadWorkflowRuns(refresh = false) {
  const p = dayParams(); p.set("workflow", runs.workflow);
  const r = await api(`/api/runs?${p}`);
  runs.runItems = r.items;
  renderRunList();
  if (!refresh && runs.runId) showRun(runs.runId);
}

function renderRunList() {
  const all = runs.runItems;
  const q = $("#runs-search").value.trim().toLowerCase();
  const failingOnly = $("#failing-only").checked;
  const items = all.filter((r) => (!failingOnly || r.failed) &&
    (!q || `${r.run_id} ${r.branch || ""} ${r.head_sha || ""}`.toLowerCase().includes(q)));
  const failed = all.filter((r) => r.failed).length;
  $("#runs-stats").replaceChildren(
    stat("Runs", all.length), stat("With failures", failed, failed ? "bad" : "good"),
    stat("Passed", all.length - failed, "good"),
    stat("Failure rate", all.length ? `${Math.round((failed / all.length) * 100)}%` : "—"),
    stat("Dates (UTC)", describeDays()));
  const list = $("#run-list");
  if (!list) return;
  list.replaceChildren();
  if (!items.length) {
    list.append(el("p", { class: "empty", style: "padding:12px" },
      all.length ? "No runs match the filter." : `No runs ${describeDays()}.`));
    return;
  }
  let day = null;
  for (const r of items) {
    const d = (r.started_at || "").slice(0, 10) || "unknown date";
    if (d !== day) {
      day = d;
      const n = items.filter((x) => (x.started_at || "").slice(0, 10) === d);
      list.append(el("div", { class: "day-head" }, d,
        el("span", { class: "muted" }, ` · ${n.length} runs, ${n.filter((x) => x.failed).length} with failures`)));
    }
    list.append(el("div", { class: `item${String(r.run_id) === String(runs.runId) ? " selected" : ""}`,
      "data-id": r.run_id, tabindex: "0", role: "button",
      onclick: () => selectRun(r.run_id),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectRun(r.run_id); } } },
      el("div", { class: "title" }, el("span", { class: `dot ${r.failed ? "bad" : "good"}`, "aria-hidden": "true" }),
        `run ${r.run_id}`, " ",
        el("span", { class: `status ${r.failed ? "bad" : "good"}` },
          r.failed ? `${r.failed}/${r.jobs} failed` : `passed · ${r.jobs} jobs`)),
      el("div", { class: "sub" }, `${(r.started_at || "").slice(11, 16)} UTC · ${r.branch || "?"} @ ${(r.head_sha || "").slice(0, 8)}` +
        (r.attempts > 1 ? ` · ${r.attempts} attempts` : ""))));
  }
}

function selectRun(runId) {
  runs.runId = runId;
  renderCrumbs();
  showRun(runId);
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !$("#view-runs").classList.contains("active")) return;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  if (runs.runId) { runs.runId = null; openWorkflow(runs.workflow); }
  else if (runs.level === "workflow") goWorkflows();
});

async function showRun(runId) {
  document.querySelectorAll("#run-list .item").forEach((i) =>
    i.classList.toggle("selected", i.dataset.id === String(runId)));
  const d = $("#run-detail");
  if (!d) return;
  d.replaceChildren(el("p", { class: "empty" }, "Loading run…"));
  let r;
  try { r = await api(`/api/runs/${runId}`); }
  catch (e) { d.replaceChildren(el("p", { class: "empty" }, e.message)); return; }
  if (String(runs.runId) !== String(runId)) return;
  d.replaceChildren(...[
    el("h2", {}, `${r.workflow} · run ${r.run_id}`),
    el("div", { class: "meta" }, `${r.failed}/${r.jobs} jobs failed · ${r.branch} @ ${(r.head_sha || "").slice(0, 8)} · `,
      el("a", { href: r.run_url, target: "_blank", rel: "noopener" }, "run on GitHub ↗")),
    r.platform_event_hint ? el("span", { class: "chip weak" },
      "platform-event hint: ≥3 components failed on one runner pool") : null,
    r.failed ? null : el("p", { class: "muted" }, `All ${r.jobs} jobs passed in this run.`),
    ...r.by_runner_pool.map((g) => el("div", {},
      el("h3", {}, `${g.runner_label} — ${g.failed.length} failed`),
      el("table", {}, el("tbody", {}, g.failed.map((j) => el("tr", {},
        el("td", {}, el("a", { href: "#", onclick: (e) => { e.preventDefault(); showCase(j.job_id); } },
          j.component || j.job_name)),
        el("td", {}, j.gpu_family || "?"), el("td", {}, j.platform || "?"),
        el("td", {}, j.conclusion), el("td", { class: "muted" }, `attempt ${j.attempt}`))))))),
    el("div", { id: "case-panel" })].filter(Boolean));
}

async function showCase(jobId) {
  const panel = $("#case-panel");
  panel.replaceChildren(el("p", { class: "empty" }, "Loading case (fetching the log)…"));
  try {
    const c = await api(`/api/jobs/${jobId}`);
    const h = c.history, f = h.flake_evidence, co = h.same_run_co_failures;
    const lines = (c.evidence?.windows || []).flatMap((w) =>
      [el("div", { class: "ln cite" }, el("span", { class: "n" }, w.anchor.line_no),
        el("span", {}, `${w.anchor.text}${w.anchor.count > 1 ? `   (×${w.anchor.count})` : ""}`))]);
    panel.replaceChildren(
      el("h2", {}, "Case: ", c.b1 ? badge(c.b1.category) : "", " ", c.job.component || c.job.job_name),
      el("div", { class: "meta" }, `${c.job.job_name} · `,
        el("a", { href: c.job_url, target: "_blank", rel: "noopener" }, "full log ↗")),
      el("dl", { class: "facts" },
        el("dt", {}, "B1 verdict (no LLM)"), el("dd", {}, c.b1 ? `${c.b1.category}: ${c.b1.reason}` : "—"),
        el("dt", {}, "Failing test"), el("dd", {}, el("code", {}, c.evidence?.failing_test || "—")),
        el("dt", {}, "Faulting kernel"), el("dd", {}, el("code", {}, c.evidence?.faulting_kernel || "—")),
        el("dt", {}, "Failing since"), el("dd", {}, h.failing_since ? `${h.failing_since.finished_at} (${h.failing_since.streak_runs} runs)` : "—"),
        el("dt", {}, "Flaky?"), el("dd", {}, f.likely_flaky ? "likely (passed on rerun / same commit passed)" : "no evidence"),
        el("dt", {}, "Same-run co-failures"), el("dd", {}, `${co.count}${co.platform_event_hint ? " · platform-event hint" : ""}`),
        el("dt", {}, "Memory"), el("dd", {}, c.memory?.match
          ? `seen ${c.memory.match.count}× · instinct: ${c.memory.match.instinct?.cause || "none yet"}`
          : "new signature (not seen before)")),
      el("h3", {}, "Top error lines (grouped repeats shown as ×N)"),
      el("div", { class: "log" }, lines.length ? lines : el("p", { class: "empty", style: "padding:8px" }, "No error lines.")));
  } catch (e) { panel.replaceChildren(el("p", { class: "empty" }, e.message)); }
}

// --- signatures -----------------------------------------------------------------------------
async function loadSignatures() {
  const r = await api("/api/signatures");
  $("#sig-summary").textContent = r.count
    ? `${r.count} signatures` : "Memory is empty: the Memory Curator fills it once agents run.";
  const head = el("tr", {}, ["Signature", "Component", "Seen", "GPU families", "Instinct", "Confidence", "Example"]
    .map((x) => el("th", {}, x)));
  const rows = r.items.map((s) => el("tr", {}, el("td", {}, el("code", {}, s.signature)),
    el("td", {}, s.component || "—"), el("td", { class: "num" }, s.count),
    el("td", {}, s.families.join(", ")), el("td", {}, s.instinct?.cause || "—"),
    el("td", { class: "num" }, s.instinct ? s.instinct.confidence.toFixed(2) : "—"),
    el("td", { class: "muted" }, s.example.slice(0, 120))));
  $("#sig-table").replaceChildren(el("thead", {}, head), el("tbody", {}, rows));
}

loadProposals().catch((e) => toast(`Error: ${e.message}`));
refreshGoldenCount().catch(() => {});
