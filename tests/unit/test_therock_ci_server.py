"""Exercise the therock-ci MCP server through a real MCP client session (in memory)."""

import json
from pathlib import Path

import pytest
from _recorded import JOB_ID, RUN_ID, recorded_transport
from mcp import Client

from rocksentinel.github.client import GitHubClient
from rocksentinel.therock_ci.server import mcp, set_github_client

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_lists_expected_tools():
    async with Client(mcp) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {
        "extract_errors",
        "map_job_to_family",
        "map_path_to_component",
        "list_failed_runs",
        "get_job_log_windows",
        "job_history",
        "find_introducing_change",
    }


@pytest.fixture
def recorded_github():
    client = GitHubClient("test-token", transport=recorded_transport())
    set_github_client(client)
    yield client
    set_github_client(None)
    client.close()


async def test_list_failed_runs_tool(recorded_github):
    out = await _call("list_failed_runs", {"since_hours": 24})
    runs = out["result"] if isinstance(out, dict) and "result" in out else out
    assert len(runs) == 1
    run = runs[0]
    assert (run["run_id"], run["workflow"]) == (RUN_ID, "test_artifacts.yml")
    rocsparse = next(j for j in run["failed_jobs"] if j["job_id"] == JOB_ID)
    assert (rocsparse["component"], rocsparse["gpu_family"], rocsparse["platform"]) == (
        "rocsparse", "gfx1151", "linux"
    )
    assert rocsparse["shard"] == "2/3"


async def test_get_job_log_windows_tool_on_raw_recorded_log(recorded_github):
    out = await _call("get_job_log_windows", {"job_id": JOB_ID, "max_lines_per_window": 30})
    assert out["faulting_kernel"] == "rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>"
    assert out["failing_test"] == "internal_extra_csrgemm_bitmap.calc_tiled_f64_i64_i64"
    assert out["exit_code"] == 8
    first = out["windows"][0]
    assert first["anchor"]["kind"] == "gpu_memory_fault"
    assert len(first["lines"]) <= 30
    texts = [line["text"] for w in out["windows"] for line in w["lines"]]
    # normalized: no GitHub timestamps or ctest "16: " prefixes in returned evidence
    assert not any(t.startswith("2026-") for t in texts)
    assert any(t.startswith("[ RUN      ] internal_extra_csrgemm_bitmap.calc_tiled") for t in texts)
    # windows never overlap
    numbers = [line["line_no"] for w in out["windows"] for line in w["lines"]]
    assert len(numbers) == len(set(numbers))


async def test_extract_errors_tool_on_rocsparse_log():
    log = (FIXTURES / "run-39249671429_rocsparse_shard2_gfx1151.txt").read_text(encoding="utf-8")
    out = await _call("extract_errors", {"log_text": log, "max_candidates": 5})
    assert out["candidates"][0]["kind"] == "gpu_memory_fault"
    assert len(out["candidates"]) <= 5
    assert out["faulting_kernel"] == "rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>"
    assert out["failing_test"] == "internal_extra_csrgemm_bitmap.calc_tiled_f64_i64_i64"
    assert out["exit_code"] == 8


async def test_map_job_to_family_tool():
    out = await _call(
        "map_job_to_family",
        {
            "workflow": "test_artifacts.yml",
            "job_name": "Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)",
            "runner_label": "linux-gfx1151-gpu-rocm",
        },
    )
    assert (out["component"], out["gpu_family"], out["platform"]) == (
        "rocsparse", "gfx1151", "linux"
    )


async def test_map_path_to_component_tool_falls_back_to_symbol():
    by_path = await _call(
        "map_path_to_component",
        {"path_or_symbol": "math-libs/BLAS/rocSPARSE/stage/lib/librocsparse.so.1.0"},
    )
    by_symbol = await _call(
        "map_path_to_component",
        {"path_or_symbol": "rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>"},
    )
    assert by_path == {"component": "rocSPARSE", "group": "math-libs", "source": "path"}
    assert by_symbol == {"component": "rocSPARSE", "group": None, "source": "symbol"}
