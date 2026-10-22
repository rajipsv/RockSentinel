"""Rule-based failure classifier over the taxonomy in the design doc (Appendix A).

Shared by the B0 and B1 baselines, so differences between them come only from how
evidence is extracted, not from the rules. Which error kinds mean "code failure",
"time limit", "infra" or "root cause", and which workflow names hint at a category,
come from the repo's packs (`rocksentinel.packs`).
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

from rocksentinel.packs import Profile, default_profile

CATEGORIES = (
    "infra",
    "flaky",
    "timeout",
    "regression",
    "submodule-bump",
    "packaging",
    "windows-specific",
    "asan",
    "unknown",
)

_HINT_REASONS = {
    "submodule-bump": "failure in bump workflow {workflow}",
    "packaging": "failure in packaging workflow {workflow}",
}


def classify(
    kinds: Iterable[str],
    *,
    workflow: str,
    variant: str | None = None,
    likely_flaky: bool = False,
    profile: Profile | None = None,
) -> tuple[str, str]:
    """Return (category, reason). Tie-breaks follow Appendix A:
    asan > infra > flaky > timeout > workflow hints (bump, packaging) > regression > unknown."""
    p = profile or default_profile()
    found = set(kinds)
    if found & p.root_cause_kinds:
        # e.g. the ROCm runtime prints "*** GPU HANG ANALYSIS" inside a memory-fault dump
        # (found by golden-001); a hang/timeout next to a fault or crash is its consequence.
        found -= p.hang_kinds
    if "asan" in found or (variant == "asan" and found & p.code_kinds):
        return "asan", "AddressSanitizer report or ASan workflow failure"
    if found & p.infra_kinds:
        return "infra", f"infrastructure signal: {sorted(found & p.infra_kinds)}"
    if likely_flaky:
        return "flaky", "passed on rerun or same commit passed elsewhere"
    if found & p.hang_kinds:
        return "timeout", f"time limit exceeded: {sorted(found & p.hang_kinds)}"
    if found:
        for glob, category in p.category_hints:
            if fnmatchcase(workflow, glob):
                reason = _HINT_REASONS.get(category, "workflow hint for {workflow}")
                return category, reason.format(workflow=workflow)
    if found & p.code_kinds:
        return "regression", f"code/test failure signal: {sorted(found & p.code_kinds)}"
    return "unknown", "no recognised failure signal"
