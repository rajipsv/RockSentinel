"""Deterministic triage of one failed job: root cause, verdict, signature, evidence.

Shared by the triage worker, the Console's on-demand "Fetch log & triage" action and (via
the same functions) the agents' tools. No LLM: this is the B1 baseline the agents must beat.
Results go to the job store's `triage_results` table, never to the memory store, so
held-out (test split) jobs can't leak into what agents look up.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from rocksentinel.baseline.rules import classify
from rocksentinel.ci_evidence.extract import Extraction, evidence_windows, extract_errors
from rocksentinel.ci_evidence.history import job_history
from rocksentinel.config import profile_for
from rocksentinel.memory.signatures import make_signature
from rocksentinel.store.base import JobResult

NON_ROOT = frozenset({"exit_code", "generic_error"})
_HW_REASON = re.compile(r"reason\s*:\s*(.+)$")
_MAX_QUOTE = 160


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_QUOTE else text[: _MAX_QUOTE - 1] + "…"


def headline(kind: str | None, text: str | None, *, failing_test: str | None = None,
             kernel: str | None = None, where: str | None = None,
             step_timeout: int | None = None, failed_tests: list[str] | None = None) -> str:
    """One plain sentence: what broke, in which test/kernel, where."""
    during = f" while running {failing_test}" if failing_test else ""
    on = f" on {where}" if where else ""
    q = _quote(text or "")
    if kind == "gpu_memory_fault":
        k = f" in kernel {kernel}" if kernel else ""
        return f"GPU memory access fault{k}{during}{on}; the test process died."
    if kind == "gpu_hw_exception":
        m = _HW_REASON.search(text or "")
        why = f" ({m.group(1).strip()})" if m else ""
        return f"GPU hardware exception{why}{during}{on}; the process was aborted."
    if kind == "gpu_hang":
        return f"The GPU hung{during}{on}."
    if kind == "asan":
        return f"AddressSanitizer found a memory error{during}{on}."
    if kind == "crash":
        return f"The test process crashed{during}{on}: {q}"
    if kind == "hip_runtime_error":
        return f"A HIP runtime call failed{on}: {q}"
    if kind == "test_timeout":
        return f"A test ran past its time limit{during}{on}: {q}"
    if kind == "step_timeout":
        mins = f" after {step_timeout} minutes" if step_timeout else ""
        return f"A CI step timed out{mins}{on}."
    if kind == "killed":
        return f"The job was killed (exit code 137: out of memory or cancelled){on}."
    if kind == "infra":
        return f"Runner or infrastructure failure{on}: {q}"
    if kind == "build_error":
        return f"The build failed{on}: {q}"
    if kind == "python_error":
        return f"A Python error stopped the job{on}: {q}"
    if kind in ("gtest_failed", "dejagnu_fail", "test_suite_failed", "ctest_failed_list",
                "ctest_summary"):
        what = failing_test or (failed_tests[0] if failed_tests else q)
        more = f" (+{len(failed_tests) - 1} more)" if failed_tests and len(failed_tests) > 1 else ""
        return f"Test failure{on}: {what}{more}"
    if kind == "build_stopped":
        return f"The build stopped{on}: {q}"
    if kind == "generic_error":
        return f"Unrecognised failure{on} (no known pattern matched): {q}"
    return f"No error lines recognised{on}; open the full log."


def root_candidate(result: Extraction, prefer: frozenset[str] = frozenset()):
    """The line behind the verdict: the first candidate of a preferred kind (e.g. the time-limit
    kinds when the verdict is "timeout"), else the highest-ranked specific error, else the
    safety net."""
    return (next((c for c in result.candidates if c.kind in prefer), None)
            or next((c for c in result.candidates if c.kind not in NON_ROOT), None)
            or result.first_error)


def triage_log(job: JobResult, log_text: str, store, *, source: str) -> dict:
    """Triage one failed job from its log text. Returns the triage_results row."""
    profile = profile_for(job.repo)
    result = extract_errors(log_text, profile=profile)
    history = job_history(store, job.job_id)
    category, reason = classify(
        [c.kind for c in result.candidates], workflow=job.workflow,
        likely_flaky=history["flake_evidence"]["likely_flaky"], profile=profile)
    prefer = {"timeout": profile.hang_kinds, "infra": profile.infra_kinds,
              "asan": frozenset({"asan"})}.get(category, frozenset())
    root = root_candidate(result, prefer)
    component = job.component or (profile.map_symbol(result.faulting_kernel)
                                  if result.faulting_kernel else None)
    where = " · ".join(x for x in (job.gpu_family, job.platform) if x) or None
    return {
        "job_id": job.job_id,
        "repo": job.repo,
        "signature": make_signature(root.text, component) if root else None,
        "root_kind": root.kind if root else None,
        "root_line": root.text if root else None,
        "headline": headline(root.kind if root else None, root.text if root else None,
                             failing_test=result.failing_test, kernel=result.faulting_kernel,
                             where=where, step_timeout=result.step_timeout_minutes,
                             failed_tests=result.ctest_failed or result.suite_failed),
        "category": category,
        "reason": reason,
        "failing_test": result.failing_test,
        "faulting_kernel": result.faulting_kernel,
        "ignored_noise": result.ignored_in_passing_tests,
        "evidence": {
            "windows": evidence_windows(result),
            "log_line_count": len(result.lines),
            "exit_code": result.exit_code,
            "ctest_failed": result.ctest_failed,
            "suite_failed": result.suite_failed,
        },
        "source": source,
        "triaged_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
