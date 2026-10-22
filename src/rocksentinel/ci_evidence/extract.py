"""Deterministic error extraction from CI job logs (no LLM).

Finds the lines that explain a failure in a huge log, ranks them so the root
error comes before its cascade, and pulls out structured facts (failing test,
faulting kernel, ctest failures, exit code, step timeout).

This module is the engine; WHAT counts as an error comes from the repo's packs
(`rocksentinel.packs`): their patterns, kind roles and noise prefixes. The keyword
idea and the 12-before/20-after window follow TheRock's
`build_tools/github_actions/detect_failed_logs.py`; ranking, grouping and noise
filtering are RockSentinel's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from rocksentinel.packs import Pattern, Profile, default_profile

WINDOW_BEFORE = 12
WINDOW_AFTER = 20
DEFAULT_MAX_CANDIDATES = 20
MAX_VARIANTS = 20  # distinct texts kept per grouped error (all line numbers are always kept)

# Lines that contain failure words but are not failures (flags, counts, retries, setup).
_GENERIC_NOISE = re.compile(
    r"(?i)-W(?:no-)?error|\b0 (?:\w+ )?(?:errors?|failures?|failed)\b|\bno errors?\b|xfail|kfail|"
    r"passed (?:after|on) retry|flaky|ignored failures?|expected failures?|"
    r"if-no-files-found|continue-on-error|fail-fast|failfast|on-error|--fail\b|"
    r"set -e|pipefail|errexit|ErrorAction|error_reporting|cleaning up|"
    r"^\s*(?:shell:|##\[group\]|\+ |echo )"
)

_GTEST_RUN = re.compile(r"^\[ RUN\s+\] (\S+)")
_GTEST_OK = re.compile(r"^\[\s+OK \] (\S+)")
_CTEST_TEST_NAME = re.compile(r"Test\s+#\d+:\s+(\S+)")
# Skips the summary line "[  FAILED  ] 1 test, listed below:"
_GTEST_FAILED_NAME = re.compile(r"^\[\s+FAILED\s+\] (?!\d+ tests?, listed below)(\S+)")
_DEJAGNU_NAME = re.compile(r"^(?:FAIL|UNRESOLVED|ERROR): (\S+\.exp)")
_KERNEL = re.compile(r"kernel: (?:void )?([^(\]]+)\(")
_CTEST_FAILED_ENTRY = re.compile(r"^\s+(\d+ - .+?)\s*$")
_SUITE_FAILED_HEADER = re.compile(r"\[X\] Unexpected Failures \(\d+\):")
# Entries under the header, possibly behind a logging prefix: "... - INFO:        gdb.rocm/x.exp"
_SUITE_FAILED_ENTRY = re.compile(r"^(?:.*? - [A-Z]+:)?\s{2,}(\S+)\s*$")
_EXIT_CODE = re.compile(r"Process completed with exit code (\d+)")
_STEP_TIMEOUT = re.compile(r"timed out after (\d+) minutes")

# An indented "error: ..." line continues the message on the line above, e.g.
#   hipModuleLoad failed: /tmp/.../Kernels.so-000-gfx950.hsaco
#    error: file not found
# The line above holds the call and the file, so evidence anchors there.
_CONTINUATION = re.compile(r"^\s+error: ")

# Repeated errors usually differ only in paths, addresses or numbers (e.g. 66 x
# "hipModuleLoad failed: /tmp/.../caches/<hash>/...hsaco"); mask those to group them.
_DEDUPE_MASKS = (
    (re.compile(r"(?:[A-Za-z]:)?[/\\][^\s:'\"]+"), "<path>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\d+"), "#"),
)


def dedupe_key(text: str) -> str:
    for pattern, repl in _DEDUPE_MASKS:
        text = pattern.sub(repl, text)
    return text.strip()

# Raw GitHub Actions logs: "2026-10-19T13:40:51.0080074Z 16: Memory access fault ..."
#   timestamp prefix, optional "##[error]"-style command, optional ctest -V "<test#>: " prefix.
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
_GHA_COMMAND = re.compile(r"^##\[(?:error|warning|notice|debug|group|endgroup)\]")
_CTEST_PREFIX = re.compile(r"^\d{1,4}: ")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def normalize_line(line: str) -> str:
    """Strip GitHub log timestamps, workflow-command markers, ctest -V prefixes and ANSI codes."""
    line = _ANSI.sub("", line).lstrip("﻿")   # some GitHub log chunks start with a BOM
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
    # Repeats of the same error shape are grouped, never dropped: every occurrence's line
    # number is kept, plus the distinct texts (e.g. different files or tests) up to a cap.
    occurrences: tuple[int, ...] = ()
    variants: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return max(1, len(self.occurrences))


@dataclass
class Extraction:
    candidates: list[ErrorCandidate]
    failing_test: str | None = None
    faulting_kernel: str | None = None
    ctest_failed: list[str] = field(default_factory=list)
    # Tests a suite runner reported as unexpected failures (all of them, not just the first)
    suite_failed: list[str] = field(default_factory=list)
    exit_code: int | None = None
    step_timeout_minutes: int | None = None
    # Error-looking lines skipped because they sit inside a gtest that passed (counted, so
    # nothing disappears silently).
    ignored_in_passing_tests: int = 0
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


def _is_noise(line: str, prefixes: tuple[str, ...]) -> bool:
    return bool(prefixes) and line.lstrip().startswith(prefixes)


def _classify(line: str, patterns: tuple[Pattern, ...]) -> Pattern | None:
    """Best specific pattern for the line; the generic safety net only if none matches."""
    best: Pattern | None = None
    generic: Pattern | None = None
    for pattern in patterns:
        if not pattern.regex.search(line):
            continue
        if pattern.kind == "generic_error":
            generic = None if _GENERIC_NOISE.search(line) else pattern
        elif best is None or pattern.severity > best.severity:
            best = pattern
    return best or generic


def _inside_passing_tests(lines: list[str]) -> list[bool]:
    """True for lines between "[ RUN ] X" and the matching "[ OK ] X" (a test that passed)."""
    inside = [False] * len(lines)
    open_at: dict[str, int] = {}
    for idx, line in enumerate(lines):
        if m := _GTEST_RUN.match(line):
            open_at[m.group(1)] = idx
        elif (m := _GTEST_OK.match(line)) and m.group(1) in open_at:
            for i in range(open_at.pop(m.group(1)) + 1, idx):
                inside[i] = True
    return inside


def extract_errors(log_text: str, max_candidates: int = DEFAULT_MAX_CANDIDATES,
                   profile: Profile | None = None) -> Extraction:
    """Rank the lines of a CI job log that most likely explain its failure.

    Candidates are ordered by severity, then by position, so a GPU fault ranks
    above the ctest summary and exit code it caused. `profile` is the repo's packs
    (default: the configured default profile).
    """
    profile = profile or default_profile()
    patterns, noise, soft = profile.patterns, profile.noise_prefixes, profile.soft_kinds
    lines = [normalize_line(raw) for raw in log_text.splitlines()]
    total = len(lines)
    in_passing = _inside_passing_tests(lines)
    ignored: set[int] = set()
    by_line: dict[int, ErrorCandidate] = {}
    first_by_key: dict[str, int] = {}
    merged: set[int] = set()  # lines folded into an earlier identical error

    for idx, line in enumerate(lines):
        if _is_noise(line, noise):
            continue
        pattern = _classify(line, patterns)
        if pattern is None:
            continue
        anchor = idx
        if _CONTINUATION.match(line) and idx > 0 and lines[idx - 1].strip() \
                and not _is_noise(lines[idx - 1], noise):
            anchor = idx - 1
            above = _classify(lines[anchor], patterns)
            if above is not None and above.severity > pattern.severity:
                pattern = above
        if in_passing[anchor] and pattern.kind in soft:
            ignored.add(anchor)
            continue
        n = anchor + 1
        text = lines[anchor].rstrip()
        if n in merged:  # continuation of a line already counted as a repeat
            continue
        existing = by_line.get(n)
        if existing is not None:  # same line reached again (continuation): not a repeat
            if pattern.severity > existing.severity:
                by_line[n] = replace(existing, kind=pattern.kind, severity=pattern.severity)
            continue
        key = dedupe_key(text)
        if key in first_by_key:  # repeated error shape: record it on the first occurrence
            first = by_line[first_by_key[key]]
            variants = first.variants
            if text not in variants and len(variants) < MAX_VARIANTS:
                variants = (*variants, text)
            by_line[first.line_no] = replace(
                first, occurrences=(*first.occurrences, n), variants=variants
            )
            merged.add(n)
            continue
        window = (max(1, n - WINDOW_BEFORE), min(total, n + WINDOW_AFTER))
        by_line[n] = ErrorCandidate(
            n, text, pattern.kind, pattern.severity, window, occurrences=(n,), variants=(text,)
        )
        first_by_key[key] = n

    found = sorted(by_line.values(), key=lambda c: (-c.severity, c.line_no))
    result = Extraction(candidates=found[:max_candidates], lines=lines,
                        ignored_in_passing_tests=len(ignored))
    _fill_facts(result, lines, found, profile.fault_kinds)
    return result


def _fill_facts(result: Extraction, lines: list[str], found: list[ErrorCandidate],
                fault_kinds: frozenset[str]) -> None:
    first = found[0] if found else None

    if first is not None and first.kind == "crash" and _CTEST_TEST_NAME.search(first.text):
        # e.g. "1349/2256 Test #1691: tests.integration...-k2000-i200 ...***Exception: SegFault"
        result.failing_test = _CTEST_TEST_NAME.search(first.text).group(1)
    elif first is not None and first.kind in fault_kinds:
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
        names = (_GTEST_FAILED_NAME.match(c.text) for c in found if c.kind == "gtest_failed")
        result.failing_test = next((m.group(1) for m in names if m), None)
    elif first is not None and first.kind == "dejagnu_fail":
        m = _DEJAGNU_NAME.match(first.text)
        result.failing_test = m.group(1) if m else None

    for idx, line in enumerate(lines):
        if "The following tests FAILED:" in line:
            for follow in lines[idx + 1 :]:
                m = _CTEST_FAILED_ENTRY.match(follow)
                if not m:
                    break
                result.ctest_failed.append(m.group(1))
        elif _SUITE_FAILED_HEADER.search(line):
            for follow in lines[idx + 1 :]:
                m = _SUITE_FAILED_ENTRY.match(follow)
                if not m or m.group(1).startswith("["):  # next "[!] ..." section
                    break
                result.suite_failed.append(m.group(1))
    if result.failing_test is None and result.suite_failed:
        result.failing_test = result.suite_failed[0]

    for line in reversed(lines):
        if result.exit_code is None and (m := _EXIT_CODE.search(line)):
            result.exit_code = int(m.group(1))
        if result.step_timeout_minutes is None and (m := _STEP_TIMEOUT.search(line)):
            result.step_timeout_minutes = int(m.group(1))
