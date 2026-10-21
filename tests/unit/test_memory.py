"""Step 5: memory (signatures, cases, instincts, feedback) and its MCP server."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from mcp import Client

from rocksentinel.memory import server
from rocksentinel.memory.signatures import make_signature, normalize, similarity
from rocksentinel.memory.store import MemoryStore, effective_confidence

FAULT_A = ("Memory access fault by GPU node-1 (Agent handle: 0x64e203123630) on address "
           "0x72f104dc4000. Reason: Page not present")
FAULT_B = FAULT_A.replace("0x64e203123630", "0x5555aaaa0000").replace("node-1", "node-3")
HIP_A = "hipModuleLoad failed: /tmp/pytest-0/popen-gw1/caches/084e6e5c/Kernels-gfx950.hsaco"
HIP_B = "hipModuleLoad failed: /tmp/pytest-7/popen-gw3/caches/aa11bb22/Kernels-gfx1151.hsaco"


# --- signatures ----------------------------------------------------------------------------

def test_same_error_on_other_run_or_family_has_same_signature():
    assert make_signature(FAULT_A, "rocsparse") == make_signature(FAULT_B, "rocsparse")
    assert make_signature(HIP_A, "tensilelite") == make_signature(HIP_B, "tensilelite")
    assert "<gfx>" in normalize(HIP_A) or "<path>" in normalize(HIP_A)


def test_component_and_different_errors_change_signature():
    assert make_signature(FAULT_A, "rocsparse") != make_signature(FAULT_A, "rocblas")
    assert make_signature(FAULT_A, "x") != make_signature(HIP_A, "x")


def test_similarity():
    assert similarity(FAULT_A, FAULT_B) == 1.0
    assert similarity(FAULT_A, HIP_A) < 0.2


# --- store ---------------------------------------------------------------------------------

@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def _case(key, text=FAULT_A, family="gfx1151", category="regression", component="rocsparse"):
    return {"case_key": key, "first_error": text, "component": component, "gpu_family": family,
            "platform": "linux", "category": category}


def test_upsert_is_idempotent_and_counts_occurrences(store):
    a = store.upsert_case(_case("1:1:10"))
    assert a["new"] is True
    assert store.upsert_case(_case("1:1:10"))["new"] is False  # same case again: no recount
    store.upsert_case(_case("2:1:20", text=FAULT_B, family="gfx110X-all"))
    sig = store.lookup_signature(a["signature"])
    assert sig["count"] == 2
    assert sig["families"] == ["gfx110X-all", "gfx1151"]  # sorted
    assert sig["instinct"] is None
    assert store.lookup_signature("0" * 16) is None


def test_search_similar_ranks_exact_then_similar(store):
    store.upsert_case(_case("1:1:10"))
    store.upsert_case(_case("3:1:30", text=HIP_A, component="tensilelite", category="flaky"))
    hits = store.search_similar(FAULT_B, component="rocsparse")
    assert hits[0]["score"] == 1.0 and hits[0]["component"] == "rocsparse"
    assert all(h["component"] == "rocsparse" for h in hits)
    assert store.search_similar("totally unrelated words here") == []


def test_instinct_confidence_rises_falls_and_archives(store):
    sig = store.upsert_case(_case("1:1:10"))["signature"]
    i = store.update_instinct(sig, "confirm", category="regression",
                              cause="OOB access in csrgemm_bitmap_emit_kernel", evidence="#12830")
    assert i["confidence"] == 0.5 and not i["trusted"]
    for _ in range(5):
        i = store.update_instinct(sig, "confirm")
    assert i["confidence"] >= 0.8 and i["trusted"]  # 0.5 -> .6 -> .68 -> .744 -> .795 -> .836
    assert i["cause"].startswith("OOB access")       # kept from the first confirmation
    for _ in range(4):
        i = store.update_instinct(sig, "contradict")
    assert i["confidence"] < 0.3 and i["archived"]
    assert len(i["history"]) == 10
    with pytest.raises(ValueError):
        store.update_instinct(sig, "maybe")


def test_confidence_decays_without_new_evidence():
    old = (datetime.now(UTC) - timedelta(weeks=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert effective_confidence(0.9, old) == round(0.9 * 0.98 ** 10, 3)
    assert effective_confidence(0.9, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")) == 0.9


def test_feedback_and_aggregate(store):
    store.upsert_case(_case("1:1:10"))
    store.upsert_case(_case("2:1:20", text=HIP_A, category="flaky"))
    store.upsert_case(_case("3:1:30", family="gfx90a", category="timeout", text="x timed out"))
    fb = store.record_outcome("1:1:10", "console", "correct", "checked the log")
    assert fb["id"] == 1
    agg = {(r["gpu_family"], r["category"]): r["n"] for r in store.aggregate_by_family()}
    assert agg == {("gfx1151", "regression"): 1, ("gfx1151", "flaky"): 1,
                   ("gfx90a", "timeout"): 1}


# --- MCP server ----------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(client, name, args):
    res = await client.call_tool(name, args)
    assert not res.is_error, res.content
    data = res.structured_content
    if data is None:
        return json.loads(res.content[0].text)
    return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data


@pytest.mark.anyio
async def test_memory_server_round_trip():
    server.set_store(MemoryStore(":memory:"))
    try:
        async with Client(server.mcp) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"lookup_signature", "search_similar_failures", "upsert_case",
                             "record_outcome", "update_instinct", "aggregate_by_family"}
            miss = await _call(client, "lookup_signature",
                               {"error_text": FAULT_A, "component": "rocsparse"})
            assert miss["found"] is False
            await _call(client, "upsert_case", {"case_key": "1:1:10", "first_error": FAULT_A,
                                                "category": "regression", "component": "rocsparse",
                                                "gpu_family": "gfx1151"})
            inst = await _call(client, "update_instinct", {
                "error_text": FAULT_B, "component": "rocsparse", "outcome": "confirm",
                "cause": "OOB in csrgemm_bitmap_emit_kernel"})
            assert inst["confidence"] == 0.5
            hit = await _call(client, "lookup_signature",
                              {"error_text": FAULT_B, "component": "rocsparse"})
            assert hit["found"] and hit["count"] == 1 and hit["instinct"]["cause"].startswith("OOB")
            similar = await _call(client, "search_similar_failures", {"error_text": FAULT_B})
            assert similar[0]["score"] == 1.0
    finally:
        server.set_store(None)
