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
- Tests: 187 unit/integration tests, all offline (recorded GitHub responses).

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

---

## Engineering practices (apply to every step)
- **One roadmap step at a time:** tests and lint green, docs updated, one commit per step; push only after approval.
- **Tests never hit the network:** recorded fixtures, re-recordable via script.
- **Secrets:** a read-only token in a git-ignored `.env` (Key Vault in Azure). A token pasted into chat was revoked and replaced.
- **Command output is trimmed only in ways that can't hide failures** (`pipefail` + exit code, `tail` not positive `grep`). This rule exists because a filter once hid a result.
- **Usage checked about every 10 minutes**, stopping at 80%.
