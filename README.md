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
src/rocksentinel/therock_ci/server.py    therock-ci MCP server (MCP Python SDK 2.x)
src/rocksentinel/github/client.py        read-only GitHub REST client (runs, jobs, logs)
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

## Run the tests
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # on Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest
```

## Acknowledgements
- [ROCm/TheRock](https://github.com/ROCm/TheRock): the CI system analysed, plus its `skills/`, `detect_failed_logs.py` and `reproduce_test_failure.py`.
- [affaan-m/ECC](https://github.com/affaan-m/ECC) (MIT): skill and harness patterns adapted for the agents.
