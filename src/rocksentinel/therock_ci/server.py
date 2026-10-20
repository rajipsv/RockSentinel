"""`therock-ci` MCP server: TheRock domain tools for the RockSentinel agents (MCP Python SDK 2.x).

Run locally over stdio:
    python -m rocksentinel.therock_ci.server

All tools are read-only and deterministic (no LLM, no network). Tools that fetch
logs and run history from GitHub / ADX are added in later phases; for now
`extract_errors` takes the log text directly.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer  # mcp 2.x name for FastMCP

from rocksentinel.therock_ci import extract as _extract
from rocksentinel.therock_ci import mappers

# Cap on log size accepted by a single call, so one huge log can't blow the agent's budget.
MAX_LOG_CHARS = 5_000_000

mcp = MCPServer("therock-ci")


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
