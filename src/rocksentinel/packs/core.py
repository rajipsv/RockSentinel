"""The `core` pack: failure signals common to any GitHub Actions CI.

Crashes, sanitizers, test runners (gtest, ctest, DejaGnu, pytest/Python), build errors,
time limits, runner/infrastructure failures, a generic safety net and the exit code.
Every repo's profile ends with this pack.
"""

from __future__ import annotations

import re

from rocksentinel.packs.base import JobInfo, Pack, pattern

PATTERNS = (
    pattern("asan", 95, r"ERROR: AddressSanitizer"),
    pattern("crash", 90,
            r"Segmentation fault|\(SEGFAULT\)|Exception: SegFault|\(Subprocess aborted\)"),
    pattern("test_timeout", 85, r"\*\*\*Timeout\s+[\d.]+\s*sec"),
    pattern("killed", 85, r"exit code 137\b"),
    pattern(
        "infra",
        80,
        r"No space left on device"
        r"|The runner has received a shutdown signal"
        r"|lost communication with the server"
        r"|Executing the custom container implementation failed",
    ),
    pattern("build_error", 80,
            r"^FAILED: |CMake Error|\berror: |undefined reference to|fatal error:"),
    pattern("python_error", 75, r"Traceback \(most recent call last\)|FileNotFoundError"),
    pattern("gtest_failed", 75, r"^\[\s+FAILED\s+\]"),
    # DejaGnu: one line per failure, with the reason after the test name:
    #   "FAIL: gdb.rocm/multi-inferior-gpu.exp: wait for gpu stops (timeout)"
    pattern("dejagnu_fail", 74, r"^(?:FAIL|UNRESOLVED|ERROR): \S+\.exp\b"),
    pattern("ctest_failed_list", 70, r"The following tests FAILED:"),
    # Summary printed by some suites' runners:
    #   "[X] Unexpected Failures (2):" / "[X] Total Failed Tests: 2" / "[X] OVERALL STATUS: FAIL"
    pattern("test_suite_failed", 72,
            r"\[X\] Unexpected Failures \(\d+\)|\[X\] Total Failed Tests: [1-9]"
            r"|OVERALL STATUS: FAIL"),
    pattern("ctest_summary", 60, r"\d+% tests passed, [1-9]\d* tests? failed out of \d+"),
    pattern("step_timeout", 60, r"The action '.+' has timed out after \d+ minutes"),
    pattern("build_stopped", 50, r"ninja: build stopped|subcommand failed"),
    # Safety net for formats no specific pattern knows: "something failed here". Ranked just
    # above the exit code, so specific patterns always win; noisy lines are filtered out.
    pattern("generic_error", 20,
            r"(?i)\b(?:error|errors|failed|failure|fatal|exception|aborted|panic|"
            r"assertion failed|assert(?:ion)? error)\b"),
    pattern("exit_code", 10, r"Process completed with exit code \d+"),
)

_SHARD = re.compile(r"\(shard (\d+)/(\d+)\)")
_PARENS = re.compile(r"\s*\([^)]*\)")


def platform_of(job_name: str, workflow: str, runner_label: str | None) -> str | None:
    text = f"{job_name} {workflow}".lower()
    for name, words in (("windows", ("windows",)), ("macos", ("macos", "darwin")),
                        ("linux", ("linux", "ubuntu"))):
        if any(w in text for w in words):
            return name
    label = (runner_label or "").lower()
    if label.startswith(("windows", "win-")) or "windows" in label:
        return "windows"
    if "macos" in label:
        return "macos"
    if label.startswith("linux") or "linux" in label or "ubuntu" in label:
        return "linux"
    return None


def map_job(workflow: str, job_name: str, runner_label: str | None = None) -> JobInfo:
    """Generic job-name reading: the last " / " segment names the job, minus "(...)" parts."""
    last = job_name.split(" / ")[-1].strip()
    component = _PARENS.sub("", last).strip() or None
    lowered = f"{job_name} {workflow}".lower()
    stage = "test" if "test" in lowered else "build" if "build" in lowered else None
    shard = f"{m.group(1)}/{m.group(2)}" if (m := _SHARD.search(job_name)) else None
    return JobInfo(component=component, gpu_family=None,
                   platform=platform_of(job_name, workflow, runner_label), stage=stage,
                   shard=shard, variant="asan" if "asan" in lowered else None)


CORE = Pack(
    name="core",
    description="Crashes, sanitizers, test runners, build errors, time limits, infra.",
    patterns=PATTERNS,
    root_cause_kinds=frozenset({"crash", "asan"}),
    code_kinds=frozenset({"crash", "build_error", "gtest_failed", "python_error",
                          "ctest_failed_list", "ctest_summary", "test_suite_failed",
                          "dejagnu_fail"}),
    hang_kinds=frozenset({"test_timeout", "step_timeout"}),
    infra_kinds=frozenset({"infra", "killed"}),
    fault_kinds=frozenset({"asan", "crash"}),
    soft_kinds=frozenset({"generic_error", "python_error", "build_error"}),
    job_mapper=map_job,
)
