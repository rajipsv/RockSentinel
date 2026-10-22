"""The two non-AI systems compared in evals.

B0 - TheRock-style excerpt: re-implements the documented behaviour of TheRock's
     build_tools/github_actions/detect_failed_logs.py (search backwards for the last
     keyword line; keep 12 lines before and 20 after; else the last 32 lines).
B1 - RockSentinel extraction: extract_errors (normalized lines, root cause ranked
     first, structured facts).

Both use the same rule classifier and the same job-name mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rocksentinel.baseline.rules import classify
from rocksentinel.ci_evidence.extract import extract_errors, normalize_line
from rocksentinel.packs import default_profile

B0_KEYWORDS = (
    "FAILED:",
    "error:",
    "CMake Error",
    "Traceback",
    "FileNotFoundError",
    "ninja: build stopped",
    "subcommand failed",
)
B0_BEFORE, B0_AFTER, B0_FALLBACK = 12, 20, 32


@dataclass
class Prediction:
    system: str
    category: str
    reason: str
    component: str | None
    gpu_family: str | None
    platform: str | None
    failing_test: str | None = None
    faulting_kernel: str | None = None
    evidence: list[str] = field(default_factory=list)


def b0_excerpt(log_text: str) -> list[str]:
    """TheRock detect_failed_logs.py-style excerpt (raw lines, no normalization)."""
    lines = log_text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if any(k in lines[idx] for k in B0_KEYWORDS):
            return lines[max(0, idx - B0_BEFORE) : idx + B0_AFTER + 1]
    return lines[-B0_FALLBACK:]


def _kinds_of(lines: list[str]) -> list[str]:
    """Classify each excerpt line with the same patterns extract_errors uses."""
    joined = "\n".join(lines)
    return [c.kind for c in extract_errors(joined, max_candidates=1000).candidates]


def run_b0(log_text: str, workflow: str, job_name: str, runner_label: str | None) -> Prediction:
    info = default_profile().map_job(workflow, job_name, runner_label)
    excerpt = b0_excerpt(log_text)
    category, reason = classify(_kinds_of(excerpt), workflow=workflow, variant=info.variant)
    return Prediction(
        system="B0", category=category, reason=reason, component=info.component,
        gpu_family=info.gpu_family, platform=info.platform,
        evidence=[normalize_line(line) for line in excerpt],
    )


def run_b1(log_text: str, workflow: str, job_name: str, runner_label: str | None,
           likely_flaky: bool = False) -> Prediction:
    profile = default_profile()
    info = profile.map_job(workflow, job_name, runner_label)
    result = extract_errors(log_text)
    category, reason = classify(
        [c.kind for c in result.candidates], workflow=workflow, variant=info.variant,
        likely_flaky=likely_flaky,
    )
    component = info.component
    if component is None and result.faulting_kernel:
        component = profile.map_symbol(result.faulting_kernel)
    return Prediction(
        system="B1", category=category, reason=reason, component=component,
        gpu_family=info.gpu_family, platform=info.platform,
        failing_test=result.failing_test, faulting_kernel=result.faulting_kernel,
        evidence=result.evidence_lines(),
    )


SYSTEMS = {"B0": run_b0, "B1": run_b1}
