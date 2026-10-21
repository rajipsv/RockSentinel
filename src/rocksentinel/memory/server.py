"""`memory` MCP server: the only path into long-term memory (design §2.5).

    python -m rocksentinel.memory.server

Read tools: lookup_signature, search_similar_failures, aggregate_by_family.
Write tools: upsert_case, record_outcome, update_instinct. The harness allowlists the
write tools for the Memory Curator (and the Report Builder's case write) only.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from rocksentinel.memory.signatures import make_signature
from rocksentinel.memory.store import MemoryStore

mcp = MCPServer("memory")

_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


def set_store(store: MemoryStore | None) -> None:
    global _store
    _store = store


@mcp.tool()
def lookup_signature(error_text: str, component: str | None = None) -> dict:
    """Exact lookup of a failure signature (normalized first error line + component).

    Returns occurrence counts, GPU families seen and the learned instinct (likely cause
    and confidence), or {"found": false}. A 'trusted' instinct can skip deep analysis.
    """
    sig = make_signature(error_text, component)
    hit = get_store().lookup_signature(sig)
    return {"found": False, "signature": sig} if hit is None else {"found": True, **hit}


@mcp.tool()
def search_similar_failures(error_text: str, k: int = 3,
                            component: str | None = None) -> list[dict]:
    """Past failures similar to this error (exact signature first, then token similarity)."""
    return get_store().search_similar(error_text, k=max(1, min(k, 10)), component=component)


@mcp.tool()
def upsert_case(case_key: str, first_error: str, category: str, component: str | None = None,
                gpu_family: str | None = None, platform: str | None = None,
                summary: str | None = None) -> dict:
    """Store a triaged case (idempotent by case_key = run_id:attempt:job_id)."""
    return get_store().upsert_case({
        "case_key": case_key, "first_error": first_error, "category": category,
        "component": component, "gpu_family": gpu_family, "platform": platform,
        "summary": summary,
    })


@mcp.tool()
def record_outcome(case_key: str, source: str, verdict: str, comment: str | None = None) -> dict:
    """Record what actually happened: source = console | rerun | fix_commit."""
    return get_store().record_outcome(case_key, source, verdict, comment)


@mcp.tool()
def update_instinct(error_text: str, outcome: str, component: str | None = None,
                    category: str | None = None, cause: str | None = None,
                    resolution: str | None = None, evidence: str | None = None) -> dict:
    """Confirm or contradict the learned cause for a signature; returns the new confidence."""
    return get_store().update_instinct(
        make_signature(error_text, component), outcome, category=category, cause=cause,
        resolution=resolution, evidence=evidence)


@mcp.tool()
def aggregate_by_family(since: str | None = None) -> list[dict]:
    """Case counts by GPU family and category (optionally since an ISO timestamp)."""
    return get_store().aggregate_by_family(since)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
