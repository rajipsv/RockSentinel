"""`therock-ci` MCP server: TheRock domain tools for the RockSentinel agents (MCP Python SDK 2.x).

Run locally over stdio:
    python -m rocksentinel.therock_ci.server

All tools are read-only and deterministic (no LLM). `list_failed_runs` and
`get_job_log_windows` read from GitHub with the read-only GITHUB_TOKEN
(environment or .env); the other tools work on data passed in.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer  # mcp 2.x name for FastMCP

from rocksentinel.github.client import GitHubClient
from rocksentinel.therock_ci import extract as _extract
from rocksentinel.therock_ci import mappers

DEFAULT_REPO = "ROCm/TheRock"
# Cap on log size accepted by a single call, so one huge log can't blow the agent's budget.
MAX_LOG_CHARS = 5_000_000

mcp = MCPServer("therock-ci")

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
                    **mappers.map_job_to_family(run.workflow, job.name, job.runner_label).as_dict(),
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
        log[:MAX_LOG_CHARS], max_candidates=max(1, min(max_candidates, 20))
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
        windows.append({"anchor": {"line_no": c.line_no, "kind": c.kind, "text": c.text},
                        "lines": lines})
    return {
        "job_id": job_id,
        "log_line_count": len(result.lines),
        "windows": windows,
        "failing_test": result.failing_test,
        "faulting_kernel": result.faulting_kernel,
        "ctest_failed": result.ctest_failed,
        "exit_code": result.exit_code,
        "step_timeout_minutes": result.step_timeout_minutes,
    }


@mcp.tool()
def extract_errors(log_text: str, max_candidates: int = 20) -> dict:
    """Rank the lines of a TheRock CI job log that most likely explain its failure.

    Returns ranked error candidates (root cause first, cascade after) with line
    numbers and quote windows, plus parsed facts: failing test, faulting GPU
    kernel, ctest failures, exit code and step timeout.
    """
    if len(log_text) > MAX_LOG_CHARS:
        raise ValueError(f"log_text exceeds {MAX_LOG_CHARS} characters")
    result = _extract.extract_errors(log_text, max_candidates=max(1, min(max_candidates, 100)))
    return {
        "candidates": [
            {
                "line_no": c.line_no,
                "kind": c.kind,
                "severity": c.severity,
                "text": c.text,
                "window": list(c.window),
            }
            for c in result.candidates
        ],
        "failing_test": result.failing_test,
        "faulting_kernel": result.faulting_kernel,
        "ctest_failed": result.ctest_failed,
        "exit_code": result.exit_code,
        "step_timeout_minutes": result.step_timeout_minutes,
    }


@mcp.tool()
def map_job_to_family(workflow: str, job_name: str, runner_label: str | None = None) -> dict:
    """Parse a TheRock CI job name into component, GPU family, platform, stage, shard and variant.

    Example: workflow="test_artifacts.yml",
    job_name="Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)",
    runner_label="linux-gfx1151-gpu-rocm".
    """
    return mappers.map_job_to_family(workflow, job_name, runner_label).as_dict()


@mcp.tool()
def map_path_to_component(path_or_symbol: str) -> dict:
    """Map a TheRock build path (e.g. 'math-libs/BLAS/rocSPARSE/stage/...') or a C++ symbol
    (e.g. 'rocsparse::csrgemm_bitmap_emit_kernel<...>') to its component and group."""
    info = mappers.map_path_to_component(path_or_symbol)
    if info.component is None:
        info = mappers.map_symbol_to_component(path_or_symbol)
    return info.as_dict()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
