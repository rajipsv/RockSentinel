"""`ci-evidence` MCP server: CI evidence tools for the RockSentinel agents (MCP Python SDK 2.x).

Run locally over stdio:
    python -m rocksentinel.ci_evidence.server

Generic tools (logs, history, job mapping) use the repo's packs; pack tools
(`map_path_to_component`, `find_introducing_change`) come from the rocm pack.
All tools are read-only and deterministic (no LLM). `list_failed_runs` and
`get_job_log_windows` read from GitHub with the read-only GITHUB_TOKEN
(environment or .env); the other tools work on data passed in.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer  # mcp 2.x name for FastMCP

from rocksentinel.ci_evidence import extract as _extract
from rocksentinel.ci_evidence import history as _history
from rocksentinel.config import profile_for
from rocksentinel.github.client import GitHubClient
from rocksentinel.packs.rocm import mappers
from rocksentinel.store.base import JobStore

DEFAULT_REPO = "ROCm/TheRock"
# Cap on log size accepted by a single call, so one huge log can't blow the agent's budget.
MAX_LOG_CHARS = 5_000_000

mcp = MCPServer("ci-evidence")

_github: GitHubClient | None = None


def get_github() -> GitHubClient:
    global _github
    if _github is None:
        _github = GitHubClient()
    return _github


def set_github_client(client: GitHubClient | None) -> None:
    """Swap the GitHub client (tests use one backed by recorded responses)."""
    global _github
    _github = client


_store: JobStore | None = None


def get_store() -> JobStore:
    """The job store filled by the poller (local SQLite; ADX in Azure)."""
    global _store
    if _store is None:
        from rocksentinel.store.sqlite_store import SQLiteStore

        _store = SQLiteStore()
    return _store


def set_store(store: JobStore | None) -> None:
    global _store
    _store = store


@mcp.tool()
def job_history(job_id: int, limit: int = 50) -> dict:
    """History of one CI job across runs, from the job store filled by the poller.

    Returns: failing-since (start of the current failure streak), last success, recent
    outcomes, failure rate, flake evidence (passed on rerun in the same run; mixed
    results on the same commit), and same-run co-failures with a platform-event hint
    (several components failing on the same runner pool in one run attempt).
    """
    return _history.job_history(get_store(), job_id, limit=max(5, min(limit, 200)))


@mcp.tool()
def list_failed_runs(
    repo: str = DEFAULT_REPO,
    since_hours: int = 24,
    workflow: str | None = None,
    branch: str | None = "main",
    limit: int = 10,
    include_failed_jobs: bool = True,
) -> list[dict]:
    """List recent failed GitHub Actions runs of a repo (default ROCm/TheRock), newest first.

    With include_failed_jobs, each run lists its failed jobs, already mapped to
    component, GPU family, platform and shard.
    """
    gh = get_github()
    runs = gh.list_failed_runs(
        repo,
        since_hours=max(1, min(since_hours, 168)),
        workflow=workflow,
        branch=branch,
        limit=max(1, min(limit, 50)),
    )
    out: list[dict] = []
    for run in runs:
        item = {
            "run_id": run.run_id,
            "run_number": run.run_number,
            "attempt": run.attempt,
            "workflow": run.workflow,
            "title": run.title,
            "head_sha": run.head_sha,
            "branch": run.branch,
            "event": run.event,
            "created_at": run.created_at,
            "html_url": run.html_url,
        }
        if include_failed_jobs:
            item["failed_jobs"] = [
                {
                    "job_id": job.job_id,
                    "name": job.name,
                    "conclusion": job.conclusion,
                    "runner_label": job.runner_label,
                    "html_url": job.html_url,
                    **profile_for(repo).map_job(run.workflow, job.name,
                                                job.runner_label).as_dict(),
                }
                for job in gh.list_jobs(repo, run.run_id, failed_only=True)
            ]
        out.append(item)
    return out


@mcp.tool()
def get_job_log_windows(
    job_id: int,
    repo: str = DEFAULT_REPO,
    max_candidates: int = 5,
    max_lines_per_window: int = 40,
) -> dict:
    """Fetch a failed job's log and return only the windows around its ranked error lines.

    Never returns the whole log. Lines are normalized (timestamps and ctest prefixes
    removed) and keep their original 1-based line numbers so evidence can be cited.
    """
    log = get_github().get_job_log(repo, job_id)
    result = _extract.extract_errors(
        log[:MAX_LOG_CHARS], max_candidates=max(1, min(max_candidates, 20)),
        profile=profile_for(repo),
    )
    cap = max(5, min(max_lines_per_window, 100))
    windows = []
    covered: set[int] = set()
    for c in result.candidates:
        if c.line_no in covered:
            continue  # already shown inside an earlier, higher-ranked window
        start, end = c.window
        if end - start + 1 > cap:
            start = max(start, c.line_no - cap // 3)
            end = start + cap - 1
        lines = [{"line_no": n, "text": result.lines[n - 1]} for n in range(start, end + 1)]
        covered.update(range(start, end + 1))
        windows.append({
            "anchor": {
                "line_no": c.line_no, "kind": c.kind, "text": c.text, "count": c.count,
                # all repeats of this error shape, so none are lost; ask for a window at any
                "occurrence_lines": list(c.occurrences),
                "variants": list(c.variants),
            },
            "lines": lines,
        })
    return {
        "job_id": job_id,
        "log_line_count": len(result.lines),
        "windows": windows,
        "failing_test": result.failing_test,
        "faulting_kernel": result.faulting_kernel,
        "ctest_failed": result.ctest_failed,
        "suite_failed": result.suite_failed,
        "exit_code": result.exit_code,
        "step_timeout_minutes": result.step_timeout_minutes,
        "ignored_in_passing_tests": result.ignored_in_passing_tests,
    }


@mcp.tool()
def extract_errors(log_text: str, max_candidates: int = 20, repo: str = DEFAULT_REPO) -> dict:
    """Rank the lines of a CI job log that most likely explain its failure.

    Returns ranked error candidates (root cause first, cascade after) with line
    numbers and quote windows, plus parsed facts: failing test, faulting GPU
    kernel, ctest failures, exit code and step timeout.
    """
    if len(log_text) > MAX_LOG_CHARS:
        raise ValueError(f"log_text exceeds {MAX_LOG_CHARS} characters")
    result = _extract.extract_errors(log_text, max_candidates=max(1, min(max_candidates, 100)),
                                     profile=profile_for(repo))
    return {
        "candidates": [
            {
                "line_no": c.line_no,
                "kind": c.kind,
                "severity": c.severity,
                "text": c.text,
                "window": list(c.window),
                "count": c.count,
                "occurrence_lines": list(c.occurrences),
                "variants": list(c.variants),
            }
            for c in result.candidates
        ],
        "failing_test": result.failing_test,
        "faulting_kernel": result.faulting_kernel,
        "ctest_failed": result.ctest_failed,
        "suite_failed": result.suite_failed,
        "exit_code": result.exit_code,
        "step_timeout_minutes": result.step_timeout_minutes,
        "ignored_in_passing_tests": result.ignored_in_passing_tests,
    }


@mcp.tool()
def map_job_to_family(workflow: str, job_name: str, runner_label: str | None = None,
                      repo: str = DEFAULT_REPO) -> dict:
    """Parse a CI job name into component, hardware family, platform, stage, shard and variant.

    Example: workflow="test_artifacts.yml",
    job_name="Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)",
    runner_label="linux-gfx1151-gpu-rocm".
    """
    return profile_for(repo).map_job(workflow, job_name, runner_label).as_dict()


@mcp.tool()
def map_path_to_component(path_or_symbol: str) -> dict:
    """Map a TheRock build path (e.g. 'math-libs/BLAS/rocSPARSE/stage/...') or a C++ symbol
    (e.g. 'rocsparse::csrgemm_bitmap_emit_kernel<...>') to its component and group."""
    info = mappers.map_path_to_component(path_or_symbol)
    if info.component is None:
        info = mappers.map_symbol_to_component(path_or_symbol)
    return info.as_dict()


@mcp.tool()
def find_introducing_change(symbol: str, submodule_repo: str = "ROCm/rocm-libraries",
                            submodule_path: str = "rocm-libraries",
                            since_days: int = 30) -> dict:
    """Trace a failing symbol (kernel, function or test name) to candidate commits in a
    submodule repo, and to the TheRock submodule bump (PR) that brought each one in."""
    from rocksentinel.packs.rocm.changes import find_introducing_change as _find  # rocm pack

    return _find(get_github(), symbol, submodule_repo=submodule_repo,
                 submodule_path=submodule_path, therock_repo=DEFAULT_REPO,
                 since_days=max(1, min(since_days, 90)))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
