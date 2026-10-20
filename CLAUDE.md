# RockSentinel: working rules for Claude Code

Design: `docs/ADLC-RockSentinel.md`. Keep code consistent with it; if a decision changes, update the doc in the same step.

## Workflow rule: one step at a time, commit after every step
1. Work on **one roadmap step at a time** (list below). Don't start the next step until the current one is committed.
2. A step is done only when:
   - its code and tests are written,
   - `pytest` passes and `ruff check .` is clean (run with `.venv/Scripts/python -m ...`),
   - `README.md` / the design doc are updated if the step changed layout or decisions.
3. **Commit locally after every step**, one commit per step, with a clear message (what + why) ending with the attribution line used in this repo.
4. **Never push without asking the user first.** Ask once per step, after the commit.
5. After committing, tick the step in the roadmap below and say what the next step is.

## Roadmap
- [x] 0. Design doc, golden cases, log fixtures
- [x] 0a. `extract_errors` + tests
- [x] 0b. `therock-ci` MCP server + mappers
- [x] 0c. Read-only GitHub client, `list_failed_runs`, `get_job_log_windows`
- [x] 1. Poller + local SQLite job store (interface that ADX implements later)
- [ ] 2. `job_history` tool (failing-since, rerun/flake evidence, same-run co-failures)
- [ ] 3. B0 non-AI baseline + eval runner over the golden set
- [ ] 4. Grow the golden set to ~30–50 human-reviewed cases
- [ ] 5. `memory` MCP server (signatures, cases, instincts, feedback; SQLite locally)
- [ ] 6. Harness + Triage agent (`LLMProvider`; replay mode; GitHub Models for live dev)
- [ ] 7. RCA agent + `find_introducing_change`
- [ ] 8. Console UI (local via SWA CLI)
- [ ] 9. Azure deployment (Bicep + azd; switch `LLMProvider` to Azure OpenAI; rerun evals)
- [ ] 10. Eval gate in CI + runbook, threat model, interview guide

## Project rules
- **Read-only toward GitHub.** No code path may write to GitHub (no comments, issues, pushes from the app).
- **Secrets:** tokens live only in the git-ignored `.env` locally and in Key Vault in Azure. Never print, log, commit or paste them. Never log signed URLs.
- **Logs never go to an LLM whole:** always through `extract_errors` / `get_job_log_windows` evidence windows.
- **Tests never hit the network:** use recorded fixtures (`tests/fixtures/`, `scripts/record_github_fixtures.py`).
- **Agents vs components:** only LLM-driven decision makers are called agents; routing and formatting stay deterministic code.
- **Build from scratch:** do not reuse code, data or knowledge bases from the ARVIL project.
- **Usage:** check Claude plan usage about every 10 minutes during long work; stop and report at 80% of any limit.
- **Local first:** steps 1–8 need no Azure subscription.
