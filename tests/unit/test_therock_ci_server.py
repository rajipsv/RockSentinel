"""Exercise the therock-ci MCP server through a real MCP client session (in memory)."""

import json
from pathlib import Path

import pytest
from mcp import Client

from rocksentinel.therock_ci.server import mcp

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
    assert tools == {"extract_errors", "map_job_to_family", "map_path_to_component"}


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
