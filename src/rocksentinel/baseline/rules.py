"""Rule-based failure classifier over the taxonomy in the design doc (Appendix A).

Shared by the B0 and B1 baselines, so differences between them come only from how
evidence is extracted, not from the rules.
"""

from __future__ import annotations

from collections.abc import Iterable

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

BUMP_WORKFLOWS = frozenset({"bump_submodules.yml", "compiler_bump.yml"})
_PACKAGING_HINTS = ("native_linux_packages", "native_windows_packages", "packages_install",
                    "python_packages", "wheels")

_INFRA_KINDS = frozenset({"infra", "killed"})
_HANG_KINDS = frozenset({"test_timeout", "step_timeout", "gpu_hang"})
_CODE_KINDS = frozenset({
    "gpu_memory_fault", "crash", "hip_runtime_error", "build_error", "gtest_failed",
    "python_error", "ctest_failed_list", "ctest_summary",
})
# A fault or crash is the root cause; any timeout/hang next to it is a consequence.
_ROOT_CAUSE_KINDS = frozenset({"gpu_memory_fault", "crash", "asan"})


def classify(
    kinds: Iterable[str],
    *,
    workflow: str,
    variant: str | None = None,
    likely_flaky: bool = False,
) -> tuple[str, str]:
    """Return (category, reason). Tie-breaks follow Appendix A:
    asan > infra > flaky > timeout > submodule-bump > packaging > regression > unknown."""
    found = set(kinds)
    if found & _ROOT_CAUSE_KINDS:
        # e.g. the ROCm runtime prints "*** GPU HANG ANALYSIS" inside a memory-fault dump
        # (found by golden-001); a hang/timeout next to a fault or crash is its consequence.
        found -= _HANG_KINDS
    if "asan" in found or (variant == "asan" and found & _CODE_KINDS):
        return "asan", "AddressSanitizer report or ASan workflow failure"
    if found & _INFRA_KINDS:
        return "infra", f"infrastructure signal: {sorted(found & _INFRA_KINDS)}"
    if likely_flaky:
        return "flaky", "passed on rerun or same commit passed elsewhere"
    if found & _HANG_KINDS:
        return "timeout", f"time limit exceeded: {sorted(found & _HANG_KINDS)}"
    if workflow in BUMP_WORKFLOWS and found:
        return "submodule-bump", f"failure in bump workflow {workflow}"
    if any(h in workflow for h in _PACKAGING_HINTS) and found:
        return "packaging", f"failure in packaging workflow {workflow}"
    if found & _CODE_KINDS:
        return "regression", f"code/test failure signal: {sorted(found & _CODE_KINDS)}"
    return "unknown", "no recognised failure signal"
