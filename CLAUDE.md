# RockSentinel: working rules for Claude Code

Design: `docs/ADLC-RockSentinel.md`. Keep code consistent with it; if a decision changes, update the doc in the same step.

## Workflow rule: one step at a time, commit after every step
1. Work on **one roadmap step at a time** (list below). Don't start the next step until the current one is committed.
2. A step is done only when:
   - its code and tests are written,
   - `pytest` passes and `ruff check .` is clean (run with `.venv/Scripts/python -m ...`),
   - `README.md` / the design doc are updated if the step changed layout or decisions.
3. **Update `docs/BUILD-LOG.md` with the `update-build-log` skill** (`.claude/skills/update-build-log/`) before committing, so the log ships with the step.
4. **Commit locally after every step**, one commit per step, with a clear message (what + why) ending with the attribution line used in this repo.
5. **Never push without asking the user first.** Ask once per step, after the commit.
6. After committing, tick the step in the roadmap below and say what the next step is.

## Roadmap
- [x] 0. Design doc, golden cases, log fixtures
- [x] 0a. `extract_errors` + tests
- [x] 0b. `therock-ci` MCP server + mappers
- [x] 0c. Read-only GitHub client, `list_failed_runs`, `get_job_log_windows`
- [x] 1. Poller + local SQLite job store (interface that ADX implements later)
- [x] 2. `job_history` tool (failing-since, rerun/flake evidence, same-run co-failures)
- [x] 3. B0 non-AI baseline + eval runner over the golden set (plus B1 deterministic baseline)
- [x] 4. Grow the golden set to ~30–50 human-reviewed cases (35 cases, 33 confirmed; 24 dev / 11 test)
- [x] 4a. Console v0 (pulled forward, user request): local review UI for proposals (accept/reject), golden cases and eval results; step 8 extends it into the full Console
- [x] 5. `memory` MCP server (signatures, cases, instincts, feedback; SQLite locally)
- [ ] 6. Harness + Triage agent (`LLMProvider`; replay mode; OpenAI-compatible live provider: Ollama locally or Azure OpenAI)
  - [x] 6a. Harness + Triage agent in replay mode (no live LLM)
  - [ ] 6b. Live OpenAI-compatible provider (built) + first live Triage run + Triage as an eval system row vs B0/B1
- [x] 7. RCA agent + `find_introducing_change` (replay mode; live evals once Azure is set up)
- [x] 8. Console UI: Review, Golden, Evals, Runs, Case, Signatures (local; SWA in step 9)
- [x] 8b. Console design pass with the vetted ui-ux-pro-max skill (.claude/skills/ui-ux-pro-max)
- [ ] Product track (generic CI/CD product; plan: org → repo → workflow, packs, GitHub App)
  - [x] P0. Extractor fix: GPU hardware exceptions, passing-test noise, mid-log excerpts
  - [x] P1. Pack architecture: `core` + `rocm` packs, `ci_evidence` engine (pure refactor, evals byte-identical)
  - [ ] P2. Per-repo config (`config/repos.yaml`), tenants, multi-repo ingestion (TheRock, rockrel, one non-ROCm repo)
  - [ ] P3. GitHub App auth (installation tokens) + webhook handler (offline-tested; live in step 9)
  - [ ] P4. Console: org → repo → workflow → run → case, root cause first, triage worker, Quality tab, no Review tab
  - [ ] P5. Docs: product architecture, private-repo/data handling, onboarding
- [ ] 8a. Adapt real ECC skills (read ECC SKILL.md files, adapt with MIT attribution into skills/, wire into agents)
- [ ] 9. Azure deployment (Bicep + azd; switch `LLMProvider` to Azure OpenAI; rerun evals)
- [ ] 10. Eval gate in CI + runbook, threat model, interview guide

## Project rules
- **Read-only toward GitHub.** No code path may write to GitHub (no comments, issues, pushes from the app).
- **Secrets:** tokens live only in the git-ignored `.env` locally and in Key Vault in Azure. Never print, log, commit or paste them. Never log signed URLs.
- **Eval hygiene:** when writing or changing patterns/rules, look only at `dev` golden cases (and non-golden logs). Never inspect `test` split cases. If a case's log is used for tuning, add its job id to `TUNED_ON_JOB_IDS` in `src/rocksentinel/evals/splits.py` so it is pinned to dev. Report headline numbers from the test split. Memory (`memory` MCP server) must never be seeded with `test` split cases before an eval run; otherwise agents could look up the answers.
- **Logs never go to an LLM whole:** always through `extract_errors` / `get_job_log_windows` evidence windows.
- **Tests never hit the network:** use recorded fixtures (`tests/fixtures/`, `scripts/record_github_fixtures.py`).
- **Agents vs components:** only LLM-driven decision makers are called agents; routing and formatting stay deterministic code.
- **Build from scratch:** do not reuse code, data or knowledge bases from the ARVIL project.
- **Usage:** check Claude plan usage about every 10 minutes during long work; stop and report at 80% of any limit.
- **Trimming command output (to save usage) must never hide failures:**
  1. use `set -o pipefail` and print `EXIT=$?` on any piped/trimmed command;
  2. trim with `tail` (errors and tracebacks come last) or `grep -v` for known noise, never a positive `grep` that keeps only expected lines;
  3. for long output, also save the full text to a scratchpad file so it can be read in full;
  4. when a command fails or a result is surprising, rerun it unfiltered before concluding anything.
- **Local first:** steps 1–8 need no Azure subscription.
