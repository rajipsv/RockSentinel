"""find_introducing_change: trace a failing symbol to its commit and the TheRock bump.

Automates the manual trace done for golden-001:
  1. search the submodule repo's commits for the symbol's distinctive words
     (rocsparse::csrgemm_bitmap_emit_kernel<...> -> "csrgemm bitmap");
  2. list TheRock commits that touched the submodule path ("Bump rocm-libraries from A to B");
  3. for each candidate commit, find the first bump whose new SHA contains it and whose
     old SHA does not (GitHub compare API: head 'ahead'/'identical' = contains).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from rocksentinel.github.client import GitHubClient, GitHubError

_GENERIC_WORDS = frozenset({
    "kernel", "kernels", "emit", "impl", "device", "host", "template", "launch", "test",
    "tests", "void", "const", "long", "int", "float", "double", "unsigned", "internal",
    "extra", "detail", "helper", "run", "main",
})
_BUMP = re.compile(r"Bump (?P<sub>[\w.\-]+) from (?P<old>[0-9a-f]{7,40}) to "
                   r"(?P<new>[0-9a-f]{7,40})(?: \(#(?P<pr>\d+)\))?")


def search_terms(symbol: str, max_terms: int = 3) -> list[str]:
    """Distinctive words of a C++ symbol or test name, for commit search."""
    name = re.sub(r"<.*", "", symbol.strip())           # drop template arguments
    name = name.split("::")[-1].split(".")[-1]          # last namespace / test segment
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+|_", name) if w]
    picked = [w for w in words if len(w) >= 4 and w not in _GENERIC_WORDS and not w.isdigit()]
    return list(dict.fromkeys(picked))[:max_terms]


def _contains(gh: GitHubClient, repo: str, head: str, commit: str) -> bool:
    return gh.compare_status(repo, commit, head) in ("ahead", "identical")


def find_introducing_change(gh: GitHubClient, symbol: str, *,
                            submodule_repo: str = "ROCm/rocm-libraries",
                            submodule_path: str = "rocm-libraries",
                            therock_repo: str = "ROCm/TheRock",
                            since_days: int = 30, max_candidates: int = 3) -> dict:
    terms = search_terms(symbol)
    result: dict = {"symbol": symbol, "search_terms": terms, "submodule_repo": submodule_repo,
                    "candidates": [], "notes": []}
    if not terms:
        result["notes"].append("no distinctive words in symbol; nothing to search")
        return result

    commits = gh.search_commits(submodule_repo, terms, limit=max_candidates)
    since = (datetime.now(UTC) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bumps = []
    for c in reversed(gh.list_commits(therock_repo, path=submodule_path, since=since)):
        m = _BUMP.search(c["message"])
        if m:                                            # oldest first
            bumps.append({"therock_commit": c["sha"][:10], "date": c["date"], "pr": m["pr"],
                          "from": m["old"], "to": m["new"], "message": c["message"]})
    if not bumps:
        result["notes"].append(f"no '{submodule_path}' bumps in TheRock in the last "
                               f"{since_days} days")

    for c in commits:
        via = None
        for b in bumps:
            try:
                if _contains(gh, submodule_repo, b["to"], c["sha"]) and not _contains(
                        gh, submodule_repo, b["from"], c["sha"]):
                    via = b
                    break
            except GitHubError as exc:
                result["notes"].append(f"compare failed for {b['to']}: {exc}")
        result["candidates"].append({**c, "entered_therock_via": via})
    return result
