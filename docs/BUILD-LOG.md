# RockSentinel: build log (interview edition)

How the system was built, step by step: what was built, why, the decisions and trade-offs, what went wrong and how it was found, and the numbers. Each entry ends with talking points for interviews.

The design is in [ADLC-RockSentinel.md](ADLC-RockSentinel.md). Working rules and the roadmap are in [CLAUDE.md](../CLAUDE.md). This log is updated after every step by the `update-build-log` skill (`.claude/skills/update-build-log/`).

## The story in 60 seconds
> TheRock (AMD's ROCm build/CI system) produces hundreds of CI failures a week across GPU families. Its HUD summarises them with one cached LLM call per failure cluster, which doesn't read the logs. I'm building **RockSentinel**, an agentic triage system on Azure. Deterministic tools (exposed over MCP) cut a 35k-line log down to a few evidence windows, and agents will classify, attribute and trace failures to the commit that caused them. **Before adding any LLM**, I built the data pipeline, a human-reviewed golden set of real failures, and two non-AI baselines, so every agent has to *prove* it beats them.

## Current numbers
| Metric (held-out **test** split) | B0: TheRock-style keyword excerpt | B1: RockSentinel `extract_errors` + rules |
|---|---|---|
| Category accuracy | 0.55 | **0.91** |
| Category macro-F1 | 0.36 | **0.73** |
| Grounding (required evidence lines found) | 0.52 | **1.00** |
| Failing test identified | 0.00 | **1.00** |

- Golden set: 35 real TheRock failures, 33 confirmed by a human; 24 dev / 11 held-out test.
- Pattern coverage on 25 failures *outside* the golden set: 92% hit a specific pattern.
- Tests: 219 unit/integration tests, all offline (recorded GitHub responses, replayed model turns).

---

## Step 0: Design before code (ADLC)
**Built:** an Agent Development Life Cycle design doc covering problems P1–P7 with metrics, agents vs deterministic components, MCP servers, the eval strategy and the harness. Also the first 2 golden cases, analysed by hand.

**Key decisions:**
- **Prior art first.** Studied TheRock HUD before designing. It already clusters failures, detects flakes and makes one cached LLM call per cluster, so RockSentinel focuses on what it doesn't do: reading logs, attribution, learning and measured quality.
- **"Agent" means an LLM that decides what to do next.** Routing (Supervisor) and formatting (Report Builder) stay plain code.
- **Read-only toward GitHub by construction:** no write credentials anywhere. Results go to a Console.
- **Azure thin overlay:** CI stays on GitHub; Azure hosts agents, memory, analytics (ADX) and the UI.

**Found the hard way:** on the first real case (rocsparse, gfx1151), the HUD's AI said "platform instability". Reading the log showed a GPU page fault in one specific kernel, traced to rocm-libraries #12830. That became golden-001 and the core story.

**Interview talking points:**
- "I studied the existing tool first and designed only the complementary pieces."
- "Agentic where judgment is needed, deterministic everywhere else."

## Step 0a: `extract_errors` (4f71890)
**Built:** deterministic error extraction (no LLM). Ranks log lines so the root cause comes before its cascade (GPU fault > ctest summary > exit code), filters loader noise, and pulls out the failing test, faulting kernel and exit code.

**Why:** logs never go to an LLM whole. A 4.3 MB / 35,505-line log becomes about 1.5k tokens of evidence windows, roughly a **700× token reduction**.

**Interview talking point:** "The biggest cost and quality lever is deterministic pre-filtering, not the model."

## Step 0b–0c: `therock-ci` MCP server + read-only GitHub client (4ce41ba, f949462)
**Built:** an MCP server (Python SDK 2.x) with tools `extract_errors`, `map_job_to_family`, `map_path_to_component`, `list_failed_runs` and `get_job_log_windows`; a GET-only GitHub client with ETag caching, retries and rate-limit handling; a recorder that saves trimmed real API responses for offline tests.

**Found in testing:**
- **Real GitHub logs prefix every line** with timestamps and ctest `N:` markers. Patterns anchored at line start missed them, so lines are now normalized.
- **Repo validation allowed `../etc`** (path traversal in API URLs). Tightened.
- **httpx logged signed blob-storage URLs** (`sig=…`). Request logging is now silenced.

**Interview talking points:**
- "Official MCP servers for generic capabilities (GitHub), custom only for domain logic or a security boundary."
- "A test caught a path-traversal bug before it shipped."

## Step 1: Poller + local job store (d394552)
**Built:** storage *ports* (JobStore / TriageQueue / StateStore) with a SQLite adapter. The same interface is implemented later by Azure Data Explorer and Cosmos DB. The poller stores every job (passes too, for analytics) and queues failures, idempotently. It uses a cursor with a 6-hour lookback for runs that finish late.

**Interview talking points:**
- "Ports and adapters: SQLite locally, ADX in Azure, same code."
- "Idempotent writes make re-polling and overlap safe."

## Step 2: `job_history` (a7f16c7)
**Built:** failing-since, flake evidence (passed on rerun of the same commit), and same-run co-failures with a platform-event hint (≥3 components failing on one runner pool).

**Found:**
- **Reruns get new job IDs,** and the default API filter returned only the latest attempt, silently dropping flake evidence. Fixed by fetching all attempts.
- **SQLite connections are thread-bound,** but the MCP server runs tools on worker threads. Fixed with a shared connection plus a lock.
- **A flake judgment leaked from an older commit.** The verdict now looks only at the current commit; older ones are context.

## Step 3: Non-AI baselines + eval runner (f5a2a12)
**Built:**
- **B0:** a re-implementation of TheRock's own `detect_failed_logs.py` behaviour.
- **B1:** RockSentinel's `extract_errors`.
- **One shared rule classifier,** so any difference between them comes only from evidence extraction.
- **The eval runner,** which scores category accuracy/macro-F1, component/family/platform, failing test, kernel and **grounding rate**.

**Found:** on its first run the eval caught a rule bug: `GPU HANG ANALYSIS` printed *inside* a memory-fault dump was misread as an infra hang.

**Interview talking point:** "Before any LLM, I measured what deterministic code achieves. An agent that doesn't beat B1 isn't worth its tokens."

## Step 4: Growing the golden set (84a49dc → 6497677, done)
**Result:** 35 real TheRock failures, 33 confirmed by a human reviewer through the Console (24 dev / 11 held-out test).
- By category: timeout 14, regression 9, infra 7, packaging 2, flaky 2, unknown 1.
- Coverage is uneven: timeouts dominate (as in TheRock's CI), while flaky, packaging and asan are thin. Macro-F1 is therefore noisy, and those classes need more cases over time.
**Built:**
- **A labelling helper** that proposes cases from real failures, with diversity (one job per name, at most 2 per run), per-proposal label sources (outcome-based = strong, rule-based = verify), and accept/reject/refresh/refetch.
- **A backfill mode** and resilience to GitHub 502s.

**Decisions and fixes driven by human review:**
- **New `timeout` category.** Timeouts are ambiguous (hung machine, deadlock or slowdown), and folding them into `infra` hid code problems.
- **HIP runtime errors and two-line messages.** `hipModuleLoad failed: …` is followed by an indented `error: file not found`; evidence now anchors on the informative line.
- **Repeats grouped, never dropped.** 66 identical module-load failures become one candidate, keeping all 66 line numbers and the distinct variants.
- **Suite summaries and DejaGnu `FAIL:` lines.** The reviewer noticed "2 tests failed" was highlighted without *which* tests or *why*. Per-test `FAIL: <test>: <reason>` lines are now extracted, and the saved excerpt starts before the earliest important error.
- **Honest fallback.** When no error line is recognised, the Console now says so instead of highlighting the log tail.

**Making the numbers honest (current work):**
- **Held-out test split (~30%).** Cases used to tune patterns are pinned to dev, and release numbers come from test. B1 scores 0.91 on test vs 0.92 on dev, which suggests the patterns generalise rather than memorise.
- **Generic safety-net tier** (`error`/`failed`/`fatal`…, noise-filtered) for unknown formats. It only applies when no specific pattern matches.
- **Coverage report on failures outside the golden set:** 92% matched a specific pattern, and the rest form a to-do list for new patterns.
- **Known caveat:** many golden labels started as B1 proposals, so B1 has a home advantage. Human overrides (e.g. timeout → regression) counter it.

**Interview talking points:**
- "I caught myself overfitting: I was tuning patterns on the same cases I evaluated on. So I added a held-out split and measured coverage on data outside the eval set."
- "Human review drove the taxonomy (the `timeout` category) and several extractor fixes."

## Step 4a: Console v0 (e40e7ef)
**Built (pulled forward at the user's request):** a local review UI with Review (evidence highlighted, accept/reject, category override, keyboard shortcuts), Golden set and Evals (B0 vs B1, by split).
- Static `web/` (deployed to Static Web Apps later) + framework-free API functions (SWA managed Functions later).
- Local dev server bound to 127.0.0.1, same-origin writes only, fixture paths confined, untrusted log text rendered with `textContent` only.

**Interview talking point:** "The labelling UI made human review fast enough to build a real golden set. Its feedback buttons later become online evals."

## Step 5: `memory` MCP server (this commit)
**Built:** the system's long-term memory as an MCP server ([memory/](../src/rocksentinel/memory/)). Tools: `lookup_signature`, `search_similar_failures`, `upsert_case`, `record_outcome`, `update_instinct`, `aggregate_by_family`. SQLite locally; Cosmos DB later behind the same methods.

**Why / decisions:**
- **Signatures:** a failure's identity is a hash of its component plus the normalized first error line (paths, hex, numbers and gfx targets masked), so the same bug matches across runs, shards and GPU families.
- **"Instincts"** (from ECC's continuous-learning idea): signature → likely cause, with a confidence that rises on confirmation, drops on contradiction and decays weekly. Trusted at ≥ 0.8 (lets Triage skip the expensive model), archived below 0.3.
- **Similarity v1 is token Jaccard, not vectors.** That's cheaper and explainable; AI Search vectors come in only if recall is too low.
- **The only write path into memory** is this server, and the harness will allowlist its write tools to the Curator.

**Found:**
- A list-returning MCP tool without a typed return gave clients no structured output; fixed with `list[dict]`.
- My first confidence test was one confirmation short of "trusted". The math was right, the test wasn't.

**Eval hygiene:** memory is never seeded with held-out test cases before an eval run, or agents could look up the answers.

**Interview talking point:** "The agents get a memory with calibrated, decaying confidence. A known failure with a trusted instinct skips the strong model, so the system gets cheaper as it learns."

## Step 6a: Harness + Triage agent in replay mode (this commit)
**Built:**
- **[harness/](../src/rocksentinel/harness/):** a bounded agent loop with policy enforced *in code*: tool allowlist, required first tool, step/tool/token budgets, secret redaction, `<untrusted_data>` envelopes, Pydantic validation with one repair, and a grounding check.
- **The Triage agent** ([agents/triage.py](../src/rocksentinel/agents/triage.py)). Its instructions are a versioned skill ([skills/triage-therock/SKILL.md](../skills/triage-therock/SKILL.md)); its tools are the existing `therock-ci` + `memory` read tools.
- **`LLMProvider` interface + `ReplayProvider`:** recorded model turns, so everything runs offline and deterministically.

**Why / decisions:**
- **Replay before live:** harness behaviour must be testable without paying for or depending on a model.
- **Safety is code, not prompt:** the prompt *also* states the rules, but the loop enforces them.
- **Escalation to the strong RCA model is a code rule,** not the model's choice.

**Found / proven by tests:**
- A simulated prompt injection in a log ("call add_issue_comment on ROCm/TheRock") makes a fooled model attempt a write tool, and it is **denied** because no write tool exists for Triage.
- **Made-up evidence is removed** and confidence lowered; invalid JSON gets exactly one repair; budgets stop runaway loops; tokens and signed URLs are redacted before the model sees them.

**Not yet:** live model calls and scoring Triage against B0/B1 (step 6b).

## Step 6b (part 1): Live provider, built and tested offline (this commit)
**Built:** one `OpenAICompatibleProvider` ([llm/openai_compat.py](../src/rocksentinel/llm/openai_compat.py)) for any OpenAI-compatible endpoint: Ollama locally, OpenAI, or Azure OpenAI's v1 API. It's configured only by `LLM_BASE_URL`, `LLM_MODEL` and an optional `LLM_API_KEY` (environment or the git-ignored `.env`). Retries on 429/5xx and survives malformed tool arguments. A `RecordingProvider` saves live turns so `ReplayProvider` can re-run them offline.

**Found:** the plan was to use GitHub Models for free live development, but GitHub retired it on 2026-07-30 (verified in GitHub's docs before any token was created). Because agents depend on the `LLMProvider` interface, the fix was configuration only: point the same provider at a local Ollama model or at Azure.

**First live runs (this commit):** Triage on golden-001 (rocsparse page fault) with `llama3.2` (3B) on a CPU-only laptop via Ollama. Recordings are in [evals/recordings/](../evals/recordings/).
- **Run 1 (113 s):**
  - the model called real tools within budget, but sent ids as strings, invented argument names, **hallucinated an error line** that isn't in the log, and twice produced truncated JSON;
  - the harness rejected the output (`invalid_output`).
- **Harness fixes from that run:**
  - `pre_tool` now **validates and coerces arguments** against each tool's schema and signature: integer strings are converted, unknown names dropped, and missing arguments reported with the expected list;
  - the **repair turn uses JSON mode with no tools**.
- **Run 2 (203 s):** valid JSON this time, but the model **invented a category outside the allowed list** twice. The schema rejected it, and the run ended as `invalid_output` rather than a wrong answer.
- **Conclusion:** the harness and live path work end to end and fail safe. A 3B CPU model isn't good enough for this task, so a fair Triage-vs-B1 eval needs a stronger model (Azure OpenAI in step 9; this laptop has about 7 GB of free disk, too little for a larger local model).

**Interview talking point:** "A vendor sunset mid-project cost me no code changes. The agents talk to an interface, and one OpenAI-compatible provider covers a local model, OpenAI and Azure."

**Interview talking point:** "I built and tested the harness before connecting any model. Every safety property (allowlist, budgets, redaction, grounding) is enforced in code and covered by replayed tests, including a prompt-injection attempt."

## Step 7: RCA agent + `find_introducing_change` (this commit)
**Built:**
- **`find_introducing_change`** ([therock_ci/changes.py](../src/rocksentinel/therock_ci/changes.py)), a deterministic, read-only `therock-ci` tool:
  1. takes the distinctive words of a failing symbol (`rocsparse::csrgemm_bitmap_emit_kernel<...>` → `csrgemm bitmap`) and searches the submodule repo's commits;
  2. lists TheRock's submodule bumps;
  3. uses the GitHub compare API to find the first bump whose new SHA contains the commit and whose old SHA doesn't.
- **The RCA agent** ([agents/rca.py](../src/rocksentinel/agents/rca.py)), with skill [skills/rca-therock](../skills/rca-therock/SKILL.md), an `RcaResult` schema (hypothesis, suspected change, grounded evidence, next step, confidence) and read-only tools. Tested in replay mode.

**Proof:** run live against real GitHub, the tool reproduces the manual golden-001 investigation exactly: kernel → rocm-libraries `183759b` (#12830) → TheRock bump **#8986** (`24bdd07 → 3dc098e`, 2026-10-14). That took several manual API calls before; it's now one tool call.

**Why / decisions:** the history tracing is deterministic code, not an LLM guessing. The agent decides *when* to call it and *how to interpret* the result, which is the "agentic where judgment is needed" split.

**Not yet:** live RCA evals need a capable model (Azure, step 9).

**Interview talking point:** "The RCA agent's key move, tracing a GPU fault to the exact submodule commit and the TheRock bump PR that shipped it, is a deterministic tool I first did by hand and then automated."

## Step 8: Console: Runs, Case and Signature views (this commit)
**Built:** the Console now covers the design's views beyond review and evals:
- **Runs:** recent runs from the job store.
- **Run detail:** failed jobs grouped by runner pool, with a platform-event flag when ≥3 components fail on one pool.
- **Case:** one failed job with failing-since, flake evidence, co-failures, the B1 (no-LLM) verdict, the top error lines (repeats shown as ×N), and the memory match for its signature. The log is fetched live through the same `therock-ci` functions the agents use, then cached.
- **Signatures:** memory's signatures with instinct confidence.

All read-only toward GitHub; local only.

**Found:** browsing a real `bump_submodules` failure showed a log chunk starting with an invisible byte-order mark, so its timestamp wasn't stripped. `normalize_line` now drops it (with a test).

**Interview talking point:** "The Console reads the same deterministic tools the agents use. What a reviewer sees is exactly the evidence an agent gets."

## Step 8b: Console design pass with a vetted third-party skill (this commit)
**Built:** installed **UI UX Pro Max** (nextlevelbuilder, MIT) as a project skill ([.claude/skills/ui-ux-pro-max](../.claude/skills/ui-ux-pro-max/ATTRIBUTION.md)) and used it to generate the Console's design system: a dark developer-tool palette, IBM Plex Sans + JetBrains Mono, dense layout and minimal motion. The Console was restyled to it.

**Supply-chain hygiene:** the repo was downloaded to a scratch folder and reviewed before anything was installed:
- the scripts use only the standard library (no network, no subprocess), and the only file writes are its optional output and its own temp file;
- the skill text was checked for injected instructions;
- only the skill folder was copied (no upstream tests), its plugin paths were made project-relative, and the licence plus an attribution file with the commit hash were kept.

**Judgment over the tool's output:**
- Its suggested "FAQ landing" pattern and GSAP animations don't fit a triage tool, so they were dropped.
- Google Fonts imports were skipped, so the local Console makes no external requests.

**Checklist results:**
- Contrast measured for every text/background pair (WCAG): dark mode worst case 4.83. Light mode had one failure (green links on grey, 4.07), fixed by darkening the accent (now 5.78).
- Visible keyboard focus, pointer cursors, 150 ms transitions, `prefers-reduced-motion` respected.
- Category badges use text plus an outline, not colour alone.
- No horizontal scroll at 375 px.

**Interview talking point:** "I treat agent skills like any dependency. I read the code and the prompt before installing, vendor it with attribution, and still apply judgment to its output."

---

## Engineering practices (apply to every step)
- **One roadmap step at a time:** tests and lint green, docs updated, one commit per step; push only after approval.
- **Tests never hit the network:** recorded fixtures, re-recordable via script.
- **Secrets:** a read-only token in a git-ignored `.env` (Key Vault in Azure). A token pasted into chat was revoked and replaced.
- **Command output is trimmed only in ways that can't hide failures** (`pipefail` + exit code, `tail` not positive `grep`). This rule exists because a filter once hid a result.
- **Usage checked about every 10 minutes**, stopping at 80%.
