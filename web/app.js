// RockSentinel Console v0: review proposals, browse the golden set, see eval results.
// All text is inserted with textContent (never innerHTML) because logs are untrusted.
"use strict";

const state = { proposals: [], categories: [], selected: null };

const $ = (sel) => document.querySelector(sel);
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
async function loadEvals() {
  $("#evals-summary").textContent = "Running…";
  const confirmed = $("#confirmed-only").checked ? "?confirmed=1" : "";
  const r = await api(`/api/evals${confirmed}`);
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

loadProposals().catch((e) => toast(`Error: ${e.message}`));
refreshGoldenCount().catch(() => {});
