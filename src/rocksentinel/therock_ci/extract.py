"""Deterministic error extraction from TheRock CI job logs (no LLM).

Finds the lines that explain a failure in a huge log, ranks them so the root
error comes before its cascade, and pulls out structured facts (failing test,
faulting GPU kernel, ctest failures, exit code, step timeout).

The keyword idea and the 12-before/20-after window follow TheRock's
`build_tools/github_actions/detect_failed_logs.py`. Ranking, GPU/test/infra
patterns and noise filtering are RockSentinel's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WINDOW_BEFORE = 12
WINDOW_AFTER = 20
DEFAULT_MAX_CANDIDATES = 20

# Lines that are high-volume and never the cause (e.g. the kpack code-object loader).
NOISE_PREFIXES = ("kpack:",)


@dataclass(frozen=True)
class Pattern:
    kind: str
    severity: int
    regex: re.Pattern[str]


def _p(kind: str, severity: int, expr: str) -> Pattern:
    return Pattern(kind, severity, re.compile(expr))


# Higher severity = more likely to be the root cause, not a consequence.
PATTERNS: tuple[Pattern, ...] = (
    _p("gpu_memory_fault", 100, r"Memory access fault by GPU|HSA_STATUS_ERROR_MEMORY_FAULT"),
    _p("gpu_hang", 95, r"\*\*\* GPU HANG ANALYSIS"),
    _p("asan", 95, r"ERROR: AddressSanitizer"),
    _p("crash", 90, r"Segmentation fault|\(SEGFAULT\)|Exception: SegFault|\(Subprocess aborted\)"),
    _p("test_timeout", 85, r"\*\*\*Timeout\s+[\d.]+\s*sec"),
    _p("killed", 85, r"exit code 137\b"),
    _p(
        "infra",
        80,
        r"No space left on device"
        r"|The runner has received a shutdown signal"
        r"|lost communication with the server"
        r"|Executing the custom container implementation failed",
    ),
    _p("build_error", 80, r"^FAILED: |CMake Error|\berror: |undefined reference to|fatal error:"),
    _p("python_error", 75, r"Traceback \(most recent call last\)|FileNotFoundError"),
    _p("gtest_failed", 75, r"^\[\s+FAILED\s+\]"),
    _p("ctest_failed_list", 70, r"The following tests FAILED:"),
    _p("ctest_summary", 60, r"\d+% tests passed, [1-9]\d* tests? failed out of \d+"),
    _p("step_timeout", 60, r"The action '.+' has timed out after \d+ minutes"),
    _p("build_stopped", 50, r"ninja: build stopped|subcommand failed"),
    _p("exit_code", 10, r"Process completed with exit code \d+"),
)

_GTEST_RUN = re.compile(r"^\[ RUN\s+\] (\S+)")
_CTEST_TEST_NAME = re.compile(r"Test\s+#\d+:\s+(\S+)")
_GTEST_FAILED_NAME = re.compile(r"^\[\s+FAILED\s+\] (\S+)")
_KERNEL = re.compile(r"kernel: (?:void )?([^(\]]+)\(")
_CTEST_FAILED_ENTRY = re.compile(r"^\s+(\d+ - .+?)\s*$")
_EXIT_CODE = re.compile(r"Process completed with exit code (\d+)")
_STEP_TIMEOUT = re.compile(r"timed out after (\d+) minutes")

_FAULT_KINDS = {"gpu_memory_fault", "gpu_hang", "asan", "crash"}

# Raw GitHub Actions logs: "2026-10-19T13:40:51.0080074Z 16: Memory access fault ..."
#   timestamp prefix, optional "##[error]"-style command, optional ctest -V "<test#>: " prefix.
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
_GHA_COMMAND = re.compile(r"^##\[(?:error|warning|notice|debug|group|endgroup)\]")
_CTEST_PREFIX = re.compile(r"^\d{1,4}: ")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def normalize_line(line: str) -> str:
    """Strip GitHub log timestamps, workflow-command markers, ctest -V prefixes and ANSI codes."""
    line = _ANSI.sub("", line)
    line = _TIMESTAMP.sub("", line, count=1)
    line = _GHA_COMMAND.sub("", line, count=1)
    return _CTEST_PREFIX.sub("", line, count=1)


@dataclass(frozen=True)
class ErrorCandidate:
    """One log line that likely explains the failure. Line numbers are 1-based."""

    line_no: int
    text: str
    kind: str
    severity: int
    window: tuple[int, int]


@dataclass
class Extraction:
    candidates: list[ErrorCandidate]
    failing_test: str | None = None
    faulting_kernel: str | None = None
    ctest_failed: list[str] = field(default_factory=list)
    exit_code: int | None = None
    step_timeout_minutes: int | None = None
    lines: list[str] = field(default_factory=list, repr=False)

    @property
    def first_error(self) -> ErrorCandidate | None:
        return self.candidates[0] if self.candidates else None

    def evidence_lines(self) -> list[str]:
        """Every line inside any candidate's window: what an agent may quote."""
        seen: set[int] = set()
        out: list[str] = []
        for c in self.candidates:
            for n in range(c.window[0], c.window[1] + 1):
                if n not in seen:
                    seen.add(n)
                    out.append(self.lines[n - 1])
        return out


def _is_noise(line: str) -> bool:
    return line.lstrip().startswith(NOISE_PREFIXES)


def _classify(line: str) -> Pattern | None:
    best: Pattern | None = None
    for pattern in PATTERNS:
        if pattern.regex.search(line) and (best is None or pattern.severity > best.severity):
            best = pattern
    return best


def extract_errors(log_text: str, max_candidates: int = DEFAULT_MAX_CANDIDATES) -> Extraction:
    """Rank the lines of a CI job log that most likely explain its failure.

    Candidates are ordered by severity, then by position, so a GPU fault ranks
    above the ctest summary and exit code it caused.
    """
    lines = [normalize_line(raw) for raw in log_text.splitlines()]
    total = len(lines)
    found: list[ErrorCandidate] = []

    for idx, line in enumerate(lines):
        if _is_noise(line):
            continue
        pattern = _classify(line)
        if pattern is None:
            continue
        n = idx + 1
        window = (max(1, n - WINDOW_BEFORE), min(total, n + WINDOW_AFTER))
        found.append(ErrorCandidate(n, line.rstrip(), pattern.kind, pattern.severity, window))

    found.sort(key=lambda c: (-c.severity, c.line_no))
    result = Extraction(candidates=found[:max_candidates], lines=lines)
    _fill_facts(result, lines, found)
    return result


def _fill_facts(result: Extraction, lines: list[str], found: list[ErrorCandidate]) -> None:
    first = found[0] if found else None

    if first is not None and first.kind in _FAULT_KINDS:
        # The test that was running when the process faulted.
        for idx in range(first.line_no - 1, -1, -1):
            m = _GTEST_RUN.match(lines[idx])
            if m:
                result.failing_test = m.group(1)
                break
        for line in lines[first.line_no - 1 : first.line_no - 1 + WINDOW_AFTER]:
            m = _KERNEL.search(line)
            if m:
                result.faulting_kernel = m.group(1).strip()
                break
    elif first is not None and first.kind == "test_timeout":
        m = _CTEST_TEST_NAME.search(first.text)
        result.failing_test = m.group(1) if m else None
    elif first is not None and first.kind == "gtest_failed":
        m = _GTEST_FAILED_NAME.match(first.text)
        result.failing_test = m.group(1) if m else None

    for idx, line in enumerate(lines):
        if "The following tests FAILED:" in line:
            for follow in lines[idx + 1 :]:
                m = _CTEST_FAILED_ENTRY.match(follow)
                if not m:
                    break
                result.ctest_failed.append(m.group(1))

    for line in reversed(lines):
        if result.exit_code is None and (m := _EXIT_CODE.search(line)):
            result.exit_code = int(m.group(1))
        if result.step_timeout_minutes is None and (m := _STEP_TIMEOUT.search(line)):
            result.step_timeout_minutes = int(m.group(1))
