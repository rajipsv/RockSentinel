# RockSentinel

AI agents that triage [ROCm/TheRock](https://github.com/ROCm/TheRock) CI failures on Azure. The agents read the logs, find the real error, attribute it to a component, GPU family and platform, trace it to the change that likely caused it, and learn from past cases. Results are shown in a reviewer console.

RockSentinel is **read-only toward GitHub**: it never posts, comments or pushes. Reviewers decide what to share.

## Status
Design phase. See the full design: [docs/ADLC-RockSentinel.md](docs/ADLC-RockSentinel.md).

## At a glance
- **AI agents:** Triage, Root-Cause Advisor, Memory Curator (PR Reviewer planned). They are run by a deterministic Supervisor, with a Report Builder at the end.
- **Tools via MCP:** GitHub's official MCP server (read-only), plus custom `therock-ci` and `memory` servers.
- **Azure:** Container Apps jobs, Service Bus, Cosmos DB, Azure Data Explorer, Azure OpenAI, Static Web Apps, Key Vault, managed identity, App Insights. Infrastructure as code in Bicep, deployed with `azd`.
- **Evaluation:** a golden set of real TheRock failures, a non-AI baseline, grounding checks and prompt-injection safety tests. See [evals/golden/](evals/golden/).

## Repository layout (so far)
```
src/rocksentinel/therock_ci/extract.py   deterministic error extraction (extract_errors)
src/rocksentinel/therock_ci/mappers.py   job -> component/family/platform, path/symbol -> component
src/rocksentinel/therock_ci/history.py   job_history: failing-since, flake evidence, co-failures
src/rocksentinel/baseline/               non-AI baselines: B0 (TheRock-style excerpt), B1 (extract_errors)
src/rocksentinel/evals/runner.py         scores systems on the golden set
src/rocksentinel/evals/labeler.py        proposes golden cases from real failures for human review
src/rocksentinel/memory/                 memory MCP server: signatures, cases, instincts, feedback
src/rocksentinel/llm/provider.py         LLMProvider interface + ReplayProvider (offline)
src/rocksentinel/llm/openai_compat.py    live provider for Ollama / OpenAI / Azure OpenAI + recorder
src/rocksentinel/harness/                agent loop, policy hooks, budgets, grounding
src/rocksentinel/agents/triage.py        Triage agent (skill: skills/triage-therock/SKILL.md)
src/rocksentinel/console/                Console API (framework-free) + local dev server
web/                                     Console frontend (static HTML/JS, served by SWA in Azure)
src/rocksentinel/therock_ci/server.py    therock-ci MCP server (MCP Python SDK 2.x)
src/rocksentinel/github/client.py        read-only GitHub REST client (runs, jobs, logs)
src/rocksentinel/ingest/poller.py        polls completed runs, stores every job, queues failures
src/rocksentinel/store/                  storage interfaces + local SQLite adapter (.data/, git-ignored)
scripts/record_github_fixtures.py        records trimmed real API responses for tests
tests/unit/                              unit + golden grounding tests
tests/fixtures/                          recorded CI log excerpts
evals/golden/                            labelled real-failure cases
docs/                                    design document (ADLC)
```

## GitHub access
Live tools need a **read-only** fine-grained token ("Public repositories (read-only)") in a
git-ignored `.env` file at the repo root:
```
GITHUB_TOKEN=<your token>
```
Tests never use the network: they replay recorded responses from `tests/fixtures/github/`.

## Poll TheRock CI locally
```bash
.venv/Scripts/python -m rocksentinel.ingest.poller --since-hours 24 --max-runs 20
```
This stores every job result (pass and fail) in `.data/rocksentinel.db` and queues failures
for triage. Re-running is safe: writes are idempotent, and each poll re-reads a 6-hour
lookback before its saved cursor to catch runs that finished late.

## Run the evals
```bash
.venv/Scripts/python -m rocksentinel.evals.runner --out evals/reports/latest.json
```
It prints category accuracy and macro-F1, component/family/platform match, failing-test and
kernel match, and the grounding rate for each system, separately for the **dev** split (may be
tuned on) and the held-out **test** split (headline numbers). The LLM agents must beat B0 and B1.

Pattern coverage on failures *outside* the golden set (which unknown log formats still need a pattern):
```bash
.venv/Scripts/python -m rocksentinel.evals.coverage --sample 25
```
The build story for interviews, updated after each step: [docs/BUILD-LOG.md](docs/BUILD-LOG.md).

## Console (local)
```bash
.venv/Scripts/python -m rocksentinel.console.server     # http://127.0.0.1:8765
```
- **Review:** each pending proposal with its highlighted evidence lines, failing-since and
  co-failure context, a category picker and Accept/Reject (keys A / R, J / K to move).
- **Golden set:** all labelled cases and who confirmed them.
- **Evals:** B0 vs B1 metrics and per-case results.

Local only (binds to 127.0.0.1, same-origin writes); it edits only the golden-set files and
never writes to GitHub. In Azure, `web/` is served by Static Web Apps and
`src/rocksentinel/console/api.py` runs as its managed Functions.

## Grow the golden set (human review)
```bash
.venv/Scripts/python -m rocksentinel.evals.labeler propose --max 10   # from the job store
.venv/Scripts/python -m rocksentinel.evals.labeler list
.venv/Scripts/python -m rocksentinel.evals.labeler accept cand-<job_id> [--category infra] [--note "..."]
.venv/Scripts/python -m rocksentinel.evals.labeler reject cand-<job_id>
```
Proposed labels come from B1's rules, so open each job's log and verify before accepting;
accepting unchecked would grade B1 against itself. Only accepted cases are `confirmed`.

## Live LLM configuration (agents)
Agents use any OpenAI-compatible endpoint, configured in the git-ignored `.env`:
```
LLM_BASE_URL=http://localhost:11434/v1      # Ollama locally (no key needed)
LLM_MODEL=qwen2.5:7b                         # a model that supports tool calling
# LLM_API_KEY=...                            # for OpenAI or Azure OpenAI
```
Tests never call a model: they replay recorded turns.

## Run the tests
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # on Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest
```

## Acknowledgements
- [ROCm/TheRock](https://github.com/ROCm/TheRock): the CI system analysed, plus its `skills/`, `detect_failed_logs.py` and `reproduce_test_failure.py`.
- [affaan-m/ECC](https://github.com/affaan-m/ECC) (MIT): skill and harness patterns adapted for the agents.
