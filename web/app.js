// RockSentinel Console: one flow from "what's failing" to "why".
//   Home (#/)  ->  repo (#/repo/<owner>/<name>)  ->  workflow (#/wf/<owner>/<name>/<file>)
//   ->  run (#/run/<id>)  ->  case (#/job/<id>, root cause first)
//   Root causes (#/signatures, #/sig/<signature>) · Quality (#/quality)
// Every screen has a URL, so Back, reload and links work. Date filters live in the URL query.
// All text is inserted with textContent (never innerHTML): logs and job names are untrusted.
"use strict";

const $ = (sel) => document.querySelector(sel);

// --- helpers --------------------------------------------------------------------------------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
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
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}
const badge = (cat) => el("span", { class: `badge ${cat || "unknown"}` }, cat || "unknown");
const when = (iso) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");
function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const utcDay = (offsetDays = 0) => new Date(Date.now() - offsetDays * 86400000).toISOString().slice(0, 10);
const tone = (failed, rate) => (!failed ? "good" : rate >= 0.5 ? "bad" : "warn");
const enc = encodeURIComponent;
const link = (href, ...kids) => el("a", { href }, ...kids);
const repoHref = (repo) => `#/repo/${repo.split("/").map(enc).join("/")}`;
const wfHref = (repo, wf) => `${repoHref(repo).replace("#/repo/", "#/wf/")}/${enc(wf)}`;
function stat(label, value, cls = "") {
  return el("div", { class: `stat ${cls}` }, el("div", { class: "stat-v" }, value),
    el("div", { class: "stat-l" }, label));
}
// replaceChildren would print null/undefined as text, so drop empty pieces first
const show = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
const panel = (...kids) => el("section", { class: "panel" }, ...kids);
const emptyNote = (...kids) => el("p", { class: "empty" }, ...kids);

// --- theme ------------------------------------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#theme-toggle").textContent = theme === "dark" ? "Light theme" : "Dark theme";
}
try { applyTheme(localStorage.getItem("rs-theme") || "dark"); } catch { applyTheme("dark"); }
$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("rs-theme", next); } catch { /* private mode */ }
  applyTheme(next);
});

// --- routing ------------------------------------------------------------------------------------
function parseRoute() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, params: new URLSearchParams(query) };
}
function dateQuery() {
  const from = $("#date-from").value, to = $("#date-to").value;
  const q = new URLSearchParams();
  if (from) q.set("since", from);
  const until = dateMode() === "day" ? from : to;
  if (until) q.set("until", until);
  if (dateMode() === "range") q.set("mode", "range");
  return q;
}
const withDates = (href) => { const q = dateQuery().toString(); return q ? `${href}?${q}` : href; };
function go(href) { location.hash = withDates(href).replace(/^#/, ""); }

// date controls <-> URL
const dateMode = () => (document.querySelector(".seg-btn.active")?.dataset.mode || "day");
function setMode(mode) {
  document.querySelectorAll(".seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
    b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  });
  $("#field-to").hidden = mode === "day";
  $("#lbl-from").textContent = mode === "day" ? "Day" : "From";
}
function syncDatesFromUrl(params) {
  const mode = params.get("mode") === "range" || (params.get("since") !== params.get("until")
    && params.get("until")) ? "range" : "day";
  setMode(mode);
  $("#date-from").value = params.get("since") || "";
  $("#date-to").value = mode === "range" ? params.get("until") || "" : "";
}
function describeDays(params) {
  const s = params.get("since"), u = params.get("until");
  if (!s && !u) return "all dates";
  if (s && s === u) return `on ${s}`;
  if (s && u) return `${s} → ${u}`;
  return s ? `from ${s}` : `until ${u}`;
}
function applyDates() { const { parts } = parseRoute(); go(`#/${parts.map(enc).join("/")}`); }
document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  setMode(b.dataset.mode);
  if (b.dataset.mode === "day") $("#date-to").value = "";
  applyDates();
}));
$("#date-from").addEventListener("change", () => {
  if (dateMode() === "range" && $("#date-to").value && $("#date-from").value > $("#date-to").value)
    $("#date-to").value = $("#date-from").value;
  applyDates();
});
$("#date-to").addEventListener("change", () => {
  if ($("#date-from").value && $("#date-to").value < $("#date-from").value)
    $("#date-from").value = $("#date-to").value;
  applyDates();
});
let dataRange = {};
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
  const p = b.dataset.preset;
  if (p === "all") { setMode("day"); $("#date-from").value = ""; $("#date-to").value = ""; }
  else if (p === "latest") { setMode("day"); $("#date-from").value = dataRange.last || utcDay(); }
  else { setMode("range"); $("#date-from").value = utcDay(Number(p) - 1); $("#date-to").value = utcDay(); }
  applyDates();
}));
$("#search").addEventListener("input", () => current.filter?.());
$("#failing-only").addEventListener("change", () => current.filter?.());

function crumbs(items) {
  const nav = $("#crumbs");
  nav.replaceChildren(...items.flatMap((c, i) => [
    i ? el("span", { class: "sep", "aria-hidden": "true" }, "›") : null,
    c.href && i < items.length - 1
      ? el("a", { class: "crumb", href: withDates(c.href) }, c.label)
      : el("span", { class: "crumb current", "aria-current": "page" }, c.label),
  ]).filter(Boolean), ...(items.length > 1 ? [el("span", { class: "kbd" }, "Esc: up one level")] : []));
  current.parent = items.length > 1 ? items[items.length - 2].href : null;
}

const current = { filter: null, parent: null, refresh: null };
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  if (current.parent) go(current.parent);
});

async function route() {
  const { parts, params } = parseRoute();
  syncDatesFromUrl(params);
  current.filter = null; current.refresh = null;
  const [head, ...rest] = parts;
  const tab = head === "signatures" || head === "sig" ? "signatures" : head === "quality" ? "quality" : "home";
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  const datesApply = !["job", "run", "sig", "quality"].includes(head);
  $("#filters").classList.toggle("dates-off", !datesApply);
  const view = $("#view");
  show(view, emptyNote("Loading…"));
  try {
    if (!head) await homeView(view, params);
    else if (head === "repo") await repoView(view, params, rest.join("/"));
    else if (head === "wf") await workflowView(view, params, `${rest[0]}/${rest[1]}`, rest[2]);
    else if (head === "run") await runView(view, Number(rest[0]));
    else if (head === "job") await caseView(view, Number(rest[0]));
    else if (head === "signatures") await signaturesView(view, params);
    else if (head === "sig") await signatureView(view, rest[0]);
    else if (head === "quality") await qualityView(view);
    else { crumbs([{ label: "Not found" }]); view.replaceChildren(emptyNote("Unknown page.")); }
    $("#updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    view.replaceChildren(panel(el("h2", {}, "Could not load this page"), emptyNote(e.message)));
  }
}
window.addEventListener("hashchange", route);
setInterval(() => {
  if ($("#auto-refresh").checked && !document.hidden && current.refresh) current.refresh();
}, 60000);

// --- shared pieces ------------------------------------------------------------------------------
function failingList(groups, { title, emptyText }) {
  const box = el("div", { class: "failing" });
  const render = () => {
    const q = $("#search").value.trim().toLowerCase();
    const items = groups.filter((g) => !q || [g.headline, g.root_line, ...g.components,
      ...g.repos, ...g.workflows, ...g.families].join(" ").toLowerCase().includes(q));
    box.replaceChildren(...(items.length ? items.map(failRow)
      : [emptyNote(groups.length ? "Nothing matches the filter." : emptyText)]));
  };
  render();
  return { node: panel(el("h2", {}, title), box), render };
}
function failRow(g) {
  const meta = [g.repos.join(", "), g.components.slice(0, 4).join(", ") || null,
    g.families.slice(0, 4).join(", ") || null].filter(Boolean).join(" · ");
  return el("a", { class: `fail-row ${g.new ? "is-new" : ""}`, href: `#/job/${g.latest_job_id}` },
    el("div", { class: "fail-head" },
      g.new ? el("span", { class: "badge new" }, "new") : null, badge(g.category),
      el("span", { class: "fail-title" }, g.headline)),
    el("div", { class: "sub" }, meta),
    el("div", { class: "sub" },
      `${g.jobs} job${g.jobs === 1 ? "" : "s"} in ${g.runs} run${g.runs === 1 ? "" : "s"} · ` +
      `first seen ${ago(g.first_seen)} · last ${ago(g.last_seen)}`));
}
function tickStrip(recent) {
  return el("div", { class: "strip" }, [...recent].reverse().map((r) => el("a", {
    class: `tick ${r.failed ? "bad" : "good"}`, href: `#/run/${r.run_id}`,
    title: `run ${r.run_id} · ${r.failed}/${r.jobs} jobs failed · ${when(r.started_at)}`,
    "aria-label": `Run ${r.run_id}: ${r.failed ? `${r.failed} jobs failed` : "passed"}`,
    onclick: (e) => e.stopPropagation() })));
}
function cardShell(href, cls, label, ...kids) {
  return el("article", { class: `wf-card ${cls}`, tabindex: "0", role: "link", "aria-label": label,
    onclick: () => go(href),
    onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); go(href); } } },
  ...kids);
}
function rateBar(pct) {
  return el("div", { class: "bar", role: "img", "aria-label": `${pct}% of runs had failures` },
    el("span", { style: `width:${pct}%` }));
}
function cardsGrid(items, card, emptyText) {
  const grid = el("div", { class: "cards" });
  const render = () => {
    const q = $("#search").value.trim().toLowerCase();
    const failingOnly = $("#failing-only").checked;
    const shown = items.filter((x) => (!failingOnly || x.failed_runs)
      && (!q || `${x.repo} ${x.workflow || ""}`.toLowerCase().includes(q)));
    grid.replaceChildren(...(shown.length ? shown.map(card) : [emptyNote(items.length ? "Nothing matches the filter." : emptyText)]));
  };
  render();
  return { node: grid, render };
}

// --- Home ---------------------------------------------------------------------------------------
async function homeView(view, params) {
  crumbs([{ label: "All repositories" }]);
  const q = params.toString();
  const [ov, repos] = await Promise.all([api(`/api/overview?${q}`), api(`/api/repos?${q}`)]);
  dataRange = ov.data_range;
  const s = ov.summary;
  const failing = failingList(ov.failing, {
    title: "What's failing (grouped by root cause)",
    emptyText: `No triaged failures ${describeDays(params)}.` });
  const cards = cardsGrid(repos.items, repoCard, "No repositories configured.");
  current.filter = () => { failing.render(); cards.render(); };
  current.refresh = route;
  show(view, 
    el("div", { class: "stats" },
      stat("Repositories", s.repos), stat("Runs", s.runs),
      stat("Runs with failures", s.failed_runs, s.failed_runs ? "bad" : "good"),
      stat("Root causes", `${s.root_causes}${s.new_root_causes ? ` (${s.new_root_causes} new)` : ""}`),
      stat("Dates (UTC)", describeDays(params))),
    untriagedNote(s),
    failing.node,
    el("h2", { class: "section-title" }, "Repositories"),
    cards.node);
}
function untriagedNote(s) {
  const missing = s.failed_jobs - s.triaged;
  if (!missing) return null;
  return el("p", { class: "note" },
    `${s.triaged} of ${s.failed_jobs} failed jobs have a root cause. The other ${missing} have no log ` +
    "saved locally: open one (repository → workflow → run → job) and use “Fetch log & triage”.");
}
function repoCard(r) {
  const pct = Math.round(r.failure_rate * 100);
  const cls = r.runs ? tone(r.failed_runs, r.failure_rate) : "idle";
  return cardShell(repoHref(r.repo), cls, `Open ${r.repo}`,
    el("div", { class: "wf-repo" }, r.tenant),
    el("div", { class: "wf-head" }, el("h3", {}, r.repo.split("/")[1]),
      el("span", { class: `rate ${cls}` }, !r.runs ? "no runs yet" : r.failed_runs ? `${pct}% failing` : "all passing")),
    r.runs ? rateBar(pct) : null,
    el("div", { class: "wf-nums" },
      el("span", {}, el("b", {}, r.workflows), " workflows"),
      el("span", {}, el("b", {}, r.runs), " runs"),
      el("span", {}, el("b", {}, r.failed_runs), " with failures")),
    r.recent.length ? tickStrip(r.recent) : null,
    el("div", { class: "chips" }, r.packs.map((p) => el("span", { class: "chip" }, `pack: ${p}`))),
    el("div", { class: "sub" }, r.runs ? `last run ${ago(r.last_run_at)}` : "configured; not polled yet"));
}

// --- Repo -> workflows --------------------------------------------------------------------------
async function repoView(view, params, repo) {
  crumbs([{ label: "All repositories", href: "#/" }, { label: repo }]);
  const q = new URLSearchParams(params); q.set("repo", repo);
  const [ov, wfs] = await Promise.all([api(`/api/overview?${q}`), api(`/api/workflows?${q}`)]);
  dataRange = ov.data_range;
  const failing = failingList(ov.failing, {
    title: "What's failing in this repository", emptyText: `No triaged failures ${describeDays(params)}.` });
  const cards = cardsGrid(wfs.items, workflowCard, `No runs ${describeDays(params)}.`);
  current.filter = () => { failing.render(); cards.render(); };
  current.refresh = route;
  const s = ov.summary;
  show(view, 
    el("div", { class: "stats" }, stat("Workflows", s.workflows), stat("Runs", s.runs),
      stat("Runs with failures", s.failed_runs, s.failed_runs ? "bad" : "good"),
      stat("Failed jobs", s.failed_jobs), stat("Dates (UTC)", describeDays(params))),
    untriagedNote(s), failing.node,
    el("h2", { class: "section-title" }, "Workflows"), cards.node);
}
function workflowCard(w) {
  const pct = Math.round(w.failure_rate * 100);
  const cls = tone(w.failed_runs, w.failure_rate);
  return cardShell(wfHref(w.repo, w.workflow), cls, `Open ${w.workflow}`,
    el("div", { class: "wf-head" }, el("h3", {}, w.workflow),
      el("span", { class: `rate ${cls}` }, w.failed_runs ? `${pct}% failing` : "all passing")),
    rateBar(pct),
    el("div", { class: "wf-nums" },
      el("span", {}, el("b", {}, w.runs), " runs"),
      el("span", {}, el("b", {}, w.failed_runs), " with failures"),
      el("span", {}, el("b", {}, w.failed_jobs), ` / ${w.jobs} jobs failed`)),
    el("div", { class: "strip-label" }, `Last ${w.recent.length} runs (oldest → newest)`),
    tickStrip(w.recent),
    el("div", { class: "sub" }, `last run ${ago(w.last_run_at)} · ${when(w.last_run_at)}`));
}

// --- Workflow -> runs ---------------------------------------------------------------------------
async function workflowView(view, params, repo, wf) {
  crumbs([{ label: "All repositories", href: "#/" }, { label: repo, href: repoHref(repo) }, { label: wf }]);
  const q = new URLSearchParams(params); q.set("repo", repo); q.set("workflow", wf);
  const r = await api(`/api/runs?${q}`);
  const list = el("div", { class: "run-list" });
  const render = () => {
    const s = $("#search").value.trim().toLowerCase();
    const failingOnly = $("#failing-only").checked;
    const items = r.items.filter((x) => (!failingOnly || x.failed)
      && (!s || `${x.run_id} ${x.branch || ""} ${x.head_sha || ""}`.toLowerCase().includes(s)));
    const out = []; let day = null;
    for (const x of items) {
      const d = (x.started_at || "").slice(0, 10) || "unknown date";
      if (d !== day) {
        day = d;
        const n = items.filter((y) => (y.started_at || "").slice(0, 10) === d);
        out.push(el("div", { class: "day-head" }, d,
          el("span", { class: "muted" }, ` · ${n.length} runs, ${n.filter((y) => y.failed).length} with failures`)));
      }
      out.push(el("a", { class: "item", href: `#/run/${x.run_id}` },
        el("div", { class: "title" }, el("span", { class: `dot ${x.failed ? "bad" : "good"}`, "aria-hidden": "true" }),
          `run ${x.run_id} `, el("span", { class: `status ${x.failed ? "bad" : "good"}` },
            x.failed ? `${x.failed}/${x.jobs} failed` : `passed · ${x.jobs} jobs`)),
        el("div", { class: "sub" }, `${(x.started_at || "").slice(11, 16)} UTC · ${x.branch || "?"} @ ${(x.head_sha || "").slice(0, 8)}`
          + (x.attempts > 1 ? ` · ${x.attempts} attempts` : ""))));
    }
    list.replaceChildren(...(out.length ? out : [emptyNote(r.items.length ? "No runs match the filter." : `No runs ${describeDays(params)}.`)]));
  };
  render();
  current.filter = render; current.refresh = route;
  const failed = r.items.filter((x) => x.failed).length;
  show(view, 
    el("div", { class: "stats" }, stat("Runs", r.items.length),
      stat("With failures", failed, failed ? "bad" : "good"),
      stat("Failure rate", r.items.length ? `${Math.round((failed / r.items.length) * 100)}%` : "—"),
      stat("Dates (UTC)", describeDays(params))),
    panel(el("h2", {}, `Runs of ${wf}`), list));
}

// --- Run ----------------------------------------------------------------------------------------
async function runView(view, runId) {
  const r = await api(`/api/runs/${runId}`);
  crumbs([{ label: "All repositories", href: "#/" }, { label: r.repo, href: repoHref(r.repo) },
    { label: r.workflow, href: wfHref(r.repo, r.workflow) }, { label: `run ${runId}` }]);
  const pools = r.by_runner_pool.map((g) => el("div", { class: "pool" },
    el("h3", {}, `${g.runner_label} — ${g.failed.length} failed`),
    el("div", { class: "table-wrap" }, el("table", {}, el("tbody", {}, g.failed.map((j) => el("tr", {},
      el("td", {}, link(`#/job/${j.job_id}`, j.component || j.job_name)),
      el("td", {}, j.gpu_family || "—"), el("td", {}, j.platform || "—"),
      el("td", {}, j.triage ? badge(j.triage.category) : el("span", { class: "muted" }, "not triaged")),
      el("td", { class: "cause" }, j.triage ? j.triage.headline : el("span", { class: "muted" }, "open the job to fetch its log")))))))));
  show(view, panel(
    el("h2", {}, `${r.workflow} · run ${runId}`),
    el("div", { class: "meta" }, `${r.failed}/${r.jobs} jobs failed · ${r.branch} @ ${(r.head_sha || "").slice(0, 8)} · ${r.event || ""} · ${when(r.started_at)}`
      + (r.attempts > 1 ? ` · ${r.attempts} attempts` : ""), " · ",
      el("a", { href: r.run_url, target: "_blank", rel: "noopener" }, "run on GitHub ↗")),
    r.platform_event_hint ? el("p", { class: "note" }, "Platform-event hint: 3 or more components failed on one runner pool, which points to the machine rather than the code.") : null,
    r.failed ? pools : emptyNote(`All ${r.jobs} jobs passed in this run.`)));
}

// --- Case: root cause first ---------------------------------------------------------------------
async function caseView(view, jobId) {
  const c = await api(`/api/jobs/${jobId}`);
  const j = c.job, t = c.triage, h = c.history, f = h.flake_evidence, co = h.same_run_co_failures;
  crumbs([{ label: "All repositories", href: "#/" }, { label: j.repo, href: repoHref(j.repo) },
    { label: j.workflow, href: wfHref(j.repo, j.workflow) }, { label: `run ${j.run_id}`, href: `#/run/${j.run_id}` },
    { label: j.component || j.job_name }]);
  const where = [j.component, j.gpu_family, j.platform, j.shard && `shard ${j.shard}`].filter(Boolean).join(" · ");
  const seen = c.same_signature.length;
  const root = !c.failed
    ? el("section", { class: "root-cause ok" }, el("div", { class: "rc-label" }, "Result"),
      el("p", { class: "rc-headline" }, `This job ${j.conclusion || "did not fail"}.`))
    : t ? el("section", { class: `root-cause ${t.category}` },
      el("div", { class: "rc-label" }, "Root cause"),
      el("p", { class: "rc-headline" }, t.headline),
      el("div", { class: "rc-verdict" }, badge(t.category), " ", el("span", {}, t.reason)),
      el("div", { class: "chips" },
        el("span", { class: `chip ${seen ? "" : "strong"}` }, seen ? `seen in ${seen} other job${seen === 1 ? "" : "s"}` : "first time seen"),
        f.likely_flaky ? el("span", { class: "chip weak" }, "likely flaky (passed on rerun or same commit passed)") : null,
        h.failing_since ? el("span", { class: "chip" }, `failing since ${when(h.failing_since.finished_at)} (${h.failing_since.streak_runs} runs)`) : null,
        co.platform_event_hint ? el("span", { class: "chip weak" }, "platform-event hint") : null),
      t.root_line ? el("pre", { class: "rc-line" }, t.root_line) : null,
      el("p", { class: "muted small-text" }, `Deterministic triage (no LLM) from the ${t.source} · `,
        "agents will add a deeper analysis here"))
      : el("section", { class: "root-cause pending" },
        el("div", { class: "rc-label" }, "Root cause"),
        el("p", { class: "rc-headline" }, "Not triaged yet: no log is saved locally for this job."),
        el("p", { class: "muted" }, "Fetching downloads this one job's log from GitHub (read-only), finds the error lines and stores the result."),
        el("button", { class: "btn primary", type: "button", onclick: (e) => fetchAndTriage(e.currentTarget, jobId) }, "Fetch log & triage"));

  const evidence = t ? panel(el("h2", {}, "Evidence"),
    el("p", { class: "muted small-text" }, `Ranked error lines: root cause first, consequences after. Line numbers are from the ${t.source}.`
      + (t.ignored_noise ? ` ${t.ignored_noise} error-looking lines from tests that passed were ignored.` : "")),
    el("div", { class: "log" }, t.evidence.windows.map((w) => el("div", { class: "lseg" },
      el("div", { class: "lseg-head" }, el("span", { class: "kind" }, w.anchor.kind),
        w.anchor.count > 1 ? ` ×${w.anchor.count}` : ""),
      w.lines.map((l) => el("div", { class: `ln${l.line_no === w.anchor.line_no ? " cite" : ""}` },
        el("span", { class: "n" }, l.line_no), el("span", {}, l.text))))))) : null;

  const outcomes = (h.recent || h.recent_outcomes || []).slice(0, 20);
  const context = panel(el("h2", {}, "Context"),
    el("dl", { class: "facts" },
      el("dt", {}, "Job"), el("dd", {}, j.job_name),
      el("dt", {}, "Where"), el("dd", {}, where || "—"),
      el("dt", {}, "Run"), el("dd", {}, link(`#/run/${j.run_id}`, `run ${j.run_id}`), ` · attempt ${j.attempt} · ${j.branch} @ ${(j.head_sha || "").slice(0, 8)}`),
      el("dt", {}, "Runner"), el("dd", {}, j.runner_label || "—"),
      el("dt", {}, "When"), el("dd", {}, when(j.started_at)),
      el("dt", {}, "Same-run co-failures"), el("dd", {}, `${co.count}${co.platform_event_hint ? " · platform-event hint" : ""}`),
      el("dt", {}, "Flaky?"), el("dd", {}, f.likely_flaky ? "likely (passed on rerun or the same commit passed)" : "no evidence"),
      el("dt", {}, "Links"), el("dd", {}, el("a", { href: c.job_url, target: "_blank", rel: "noopener" }, "full log on GitHub ↗"), " · ",
        el("a", { href: c.run_url, target: "_blank", rel: "noopener" }, "run on GitHub ↗"))),
    outcomes.length ? el("div", {}, el("div", { class: "strip-label" }, "This job's recent outcomes (oldest → newest)"),
      el("div", { class: "strip" }, [...outcomes].reverse().map((o) => el("span", {
        class: `tick ${o.failed || o.conclusion === "failure" ? "bad" : "good"}`,
        title: `${o.conclusion || ""} · run ${o.run_id || ""}` })))) : null,
    seen ? el("div", {}, el("h3", {}, "Same root cause in other jobs"),
      el("ul", { class: "plain" }, c.same_signature.map((s) => el("li", {},
        link(`#/job/${s.job_id}`, `${s.repo} · ${s.workflow}`), ` · ${[s.gpu_family, s.platform].filter(Boolean).join(" · ")} · ${when(s.started_at)}`))),
      t?.signature ? link(`#/sig/${t.signature}`, "all jobs with this root cause →") : null) : null);

  show(view, root, evidence, context);
}
async function fetchAndTriage(btn, jobId) {
  btn.disabled = true; btn.textContent = "Fetching log…";
  try { await api(`/api/jobs/${jobId}/triage`, { method: "POST", body: "{}" }); toast("Triaged"); route(); }
  catch (e) { toast(e.message); btn.disabled = false; btn.textContent = "Fetch log & triage"; }
}

// --- Root causes (signatures) -------------------------------------------------------------------
async function signaturesView(view, params) {
  crumbs([{ label: "Root causes" }]);
  const r = await api(`/api/signatures?${params}`);
  const list = failingList(r.items, { title: `${r.count} root causes ${describeDays(params)}`,
    emptyText: "No triaged failures yet." });
  current.filter = list.render; current.refresh = route;
  show(view, list.node);
}
async function signatureView(view, sig) {
  const g = await api(`/api/signatures/${sig}`);
  crumbs([{ label: "Root causes", href: "#/signatures" }, { label: sig }]);
  show(view, 
    el("section", { class: `root-cause ${g.category}` }, el("div", { class: "rc-label" }, "Root cause"),
      el("p", { class: "rc-headline" }, g.headline), el("div", { class: "rc-verdict" }, badge(g.category)),
      g.root_line ? el("pre", { class: "rc-line" }, g.root_line) : null,
      el("p", { class: "muted small-text" }, `${g.jobs} jobs in ${g.runs} runs · first seen ${when(g.first_seen)} · last ${when(g.last_seen)}`)),
    panel(el("h2", {}, "Jobs with this root cause"), el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Job", "Repository", "Workflow", "Family", "Platform", "When"].map((x) => el("th", {}, x)))),
      el("tbody", {}, g.items.map((x) => el("tr", {},
        el("td", {}, link(`#/job/${x.job_id}`, x.component || x.job_name)), el("td", {}, x.repo),
        el("td", {}, x.workflow), el("td", {}, x.gpu_family || "—"), el("td", {}, x.platform || "—"),
        el("td", {}, when(x.started_at)))))))));
}

// --- Quality ------------------------------------------------------------------------------------
async function qualityView(view) {
  crumbs([{ label: "Quality" }]);
  const split = el("select", { id: "split", "aria-label": "Eval split" },
    el("option", { value: "test" }, "test (held out)"), el("option", { value: "dev" }, "dev (may be tuned on)"),
    el("option", { value: "all" }, "all"));
  const summary = el("p", { class: "muted" }), table = el("table"), cases = el("table");
  async function loadEvals() {
    summary.textContent = "Running…";
    const r = await api(`/api/evals?split=${split.value}`);
    summary.textContent = `${r.n_cases} cases (${r.n_confirmed} confirmed by a human)` + (r.n_cases < 30 ? " · small set: indicative only" : "");
    const systems = Object.keys(r.systems);
    const metrics = systems.length ? Object.keys(r.systems[systems[0]]) : [];
    table.replaceChildren(el("thead", {}, el("tr", {}, el("th", {}, "Metric"), systems.map((s) => el("th", {}, s)))),
      el("tbody", {}, metrics.map((m) => {
        const vals = systems.map((s) => r.systems[s][m]);
        const best = Math.max(...vals.filter((v) => v !== null));
        return el("tr", {}, el("td", {}, m), vals.map((v) => el("td", {
          class: `num${v !== null && v === best && vals.filter((x) => x === best).length === 1 ? " best" : ""}` },
        v === null ? "—" : v.toFixed(2))));
      })));
    cases.replaceChildren(el("thead", {}, el("tr", {}, ["Case", "Expected", ...systems].map((x) => el("th", {}, x)))),
      el("tbody", {}, r.cases.map((c) => el("tr", {}, el("td", {}, c.case_id), el("td", {}, badge(c.expected_category)),
        systems.map((s) => el("td", {}, badge(c[s].category), ` ${c[s].grounding.toFixed(2)}`))))));
  }
  split.addEventListener("change", loadEvals);
  const g = await api("/api/golden");
  const golden = el("table", {}, el("thead", {}, el("tr", {}, ["Case", "Split", "Category", "Repository", "Where", "Test / kernel"].map((x) => el("th", {}, x)))),
    el("tbody", {}, g.items.map((c) => el("tr", {},
      el("td", {}, c.in_store ? link(`#/job/${c.job_id}`, c.case_id)
        : c.job_url ? el("a", { href: c.job_url, target: "_blank", rel: "noopener" }, `${c.case_id} ↗`) : c.case_id),
      el("td", {}, c.split || "—"), el("td", {}, badge(c.category)), el("td", {}, c.repo),
      el("td", {}, [c.component, c.gpu_family, c.platform].filter(Boolean).join(" · ") || "—"),
      el("td", {}, el("code", {}, c.failing_test || c.failing_kernel || "—"))))));
  show(view, 
    panel(el("div", { class: "row-between" }, el("h2", {}, "How accurate is the root-cause finder?"), el("label", {}, "Split ", split)),
      el("p", { class: "muted" }, "Scored against the golden set: real failures labelled by a human. B0 = keyword excerpt (what CI shows today); "
        + "B1 = RockSentinel's deterministic triage. The AI agents must beat both. Headline numbers come from the held-out test split."),
      summary, el("div", { class: "table-wrap" }, table), el("h3", {}, "Per case"), el("div", { class: "table-wrap" }, cases)),
    panel(el("h2", {}, `Golden set: ${g.count} cases, ${g.confirmed} confirmed`),
      el("p", { class: "muted small-text" }, "Labelling is done from the command line: python -m rocksentinel.evals.labeler"),
      el("div", { class: "table-wrap" }, golden)));
  loadEvals().catch((e) => { summary.textContent = e.message; });
}

route();
