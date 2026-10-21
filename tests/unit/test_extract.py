import json
from pathlib import Path

import pytest

from rocksentinel.therock_ci.extract import extract_errors

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "logs"
GOLDEN = ROOT / "evals" / "golden" / "dataset.jsonl"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _golden_cases() -> list[dict]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line]


# --- golden-001: rocsparse GPU page fault -------------------------------------------------

@pytest.fixture(scope="module")
def rocsparse():
    return extract_errors(_load("run-39249671429_rocsparse_shard2_gfx1151.txt"))


def test_rocsparse_first_error_is_gpu_fault_not_cascade(rocsparse):
    assert rocsparse.first_error.kind == "gpu_memory_fault"
    assert "HSA_STATUS_ERROR_MEMORY_FAULT" in rocsparse.first_error.text


def test_rocsparse_identifies_test_and_kernel(rocsparse):
    assert rocsparse.failing_test == "internal_extra_csrgemm_bitmap.calc_tiled_f64_i64_i64"
    assert rocsparse.faulting_kernel == "rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>"


def test_rocsparse_ctest_facts(rocsparse):
    expected = "16 - rocsparse-unit-test-device_gfx1151 (Subprocess aborted)"
    assert rocsparse.ctest_failed == [expected]
    assert rocsparse.exit_code == 8
    assert rocsparse.step_timeout_minutes is None


def test_rocsparse_ignores_kpack_noise(rocsparse):
    assert not any(c.text.lstrip().startswith("kpack:") for c in rocsparse.candidates)


def test_rocsparse_exit_code_ranks_last():
    log = _load("run-39249671429_rocsparse_shard2_gfx1151.txt")
    full = extract_errors(log, max_candidates=1000).candidates
    assert full[-1].kind == "exit_code"
    # the generic safety net never outranks a specific pattern
    kinds = [c.kind for c in full]
    assert kinds.index("gpu_memory_fault") < min(
        (i for i, k in enumerate(kinds) if k == "generic_error"), default=len(kinds))


def test_generic_safety_net_catches_unknown_formats_and_skips_noise():
    log = (
        "shell: bash --noprofile --norc -e -o pipefail {0}\n"
        "cmake -DCMAKE_CXX_FLAGS=-Werror ..\n"
        "100% tests passed, 0 tests failed out of 10\n"
        "XFAIL: gdb.base/x.exp\n"
        "Some new tool: Fatal: device lost while running kernel foo\n"
        "Cleaning up orphan processes\n"
        "Process completed with exit code 1.\n"
    )
    r = extract_errors(log)
    assert [c.kind for c in r.candidates] == ["generic_error", "exit_code"]
    assert r.first_error.text.startswith("Some new tool: Fatal")


# --- golden-002: hip-tests timeout --------------------------------------------------------

@pytest.fixture(scope="module")
def hip_tests():
    return extract_errors(_load("run-39249671429_hip-tests_shard4_gfx1151.txt"))


def test_hip_tests_first_error_is_test_timeout(hip_tests):
    assert hip_tests.first_error.kind == "test_timeout"
    assert hip_tests.failing_test == "Unit_hiprtcGpuMaxThreadPerBlockComplrOptnTst"


def test_hip_tests_step_timeout(hip_tests):
    assert hip_tests.step_timeout_minutes == 120
    assert any(c.kind == "step_timeout" for c in hip_tests.candidates)


# --- grounding: every line the golden set says must be cited is reachable as evidence ----

@pytest.mark.parametrize("case", _golden_cases(), ids=lambda c: c["case_id"])
def test_golden_must_cite_lines_are_in_evidence(case):
    log = (ROOT / case["log_fixture"]).read_text(encoding="utf-8")
    evidence = "\n".join(extract_errors(log).evidence_lines())
    for snippet in case["expected"]["must_cite"]:
        assert snippet in evidence, f"{case['case_id']}: missing evidence {snippet!r}"


# --- pattern coverage on small synthetic logs ---------------------------------------------

@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("FAILED: math-libs/rocBLAS/build/foo.o", "build_error"),
        ("CMake Error at CMakeLists.txt:12 (message):", "build_error"),
        ("/src/a.cpp:3:5: error: use of undeclared identifier 'x'", "build_error"),
        ("ld.lld: error: undefined reference to `hipFoo'", "build_error"),
        ("OSError: [Errno 28] No space left on device", "infra"),
        ("Executing the custom container implementation failed.", "infra"),
        ("Error: Process completed with exit code 137.", "killed"),
        ("ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1", "asan"),
        ("[  FAILED  ] Suite.Case (12 ms)", "gtest_failed"),
        ("Traceback (most recent call last):", "python_error"),
    ],
)
def test_pattern_kinds(line, kind):
    result = extract_errors(f"some setup\n{line}\nmore output\n")
    assert result.first_error.kind == kind
    assert result.first_error.line_no == 2


def test_clean_log_has_no_candidates():
    result = extract_errors("all good\n100% tests passed, 0 tests failed out of 10\n")
    assert result.candidates == []
    assert result.first_error is None


def test_root_cause_beats_earlier_lower_severity_line():
    log = "ninja: build stopped: subcommand failed.\nMemory access fault by GPU node-1\n"
    assert extract_errors(log).first_error.kind == "gpu_memory_fault"


def test_normalizes_raw_github_log_lines():
    raw = (
        "2026-10-19T13:40:50.1Z 16: [ RUN      ] Suite.Case\n"
        "2026-10-19T13:40:51.0080074Z 16: Memory access fault by GPU node-1 on address 0x1.\n"
        "2026-10-19T13:40:52.3247183Z ##[error]Process completed with exit code 8.\n"
    )
    result = extract_errors(raw)
    assert result.first_error.kind == "gpu_memory_fault"
    assert result.first_error.text.startswith("Memory access fault")
    assert result.failing_test == "Suite.Case"
    assert result.exit_code == 8


def test_hip_runtime_error_anchors_on_line_above_continuation():
    log = (
        "+ tensilelite-client --config-file /tmp/a/ClientParameters.ini\n"
        "hipModuleLoad failed: /tmp/a/Kernels.so-000-gfx950.hsaco\n"
        " error: file not found\n"
        "Process completed with exit code 1.\n"
    )
    first = extract_errors(log).first_error
    assert first.kind == "hip_runtime_error" and first.line_no == 2
    assert first.text.startswith("hipModuleLoad failed:")
    assert first.count == 1


def test_repeats_are_grouped_but_every_occurrence_is_kept():
    blocks = []
    for i in range(5):
        blocks += [
            f"hipModuleLoad failed: /tmp/cache/{i:04x}/Kernels.hsaco",
            " error: file not found",
            "TIMING:flush 0.001",
        ]
    r = extract_errors("\n".join(blocks))
    hip = [c for c in r.candidates if c.kind == "hip_runtime_error"]
    assert len(hip) == 1  # one candidate, not five windows of the same thing
    assert hip[0].count == 5
    assert hip[0].occurrences == (1, 4, 7, 10, 13)  # no occurrence lost
    assert len(hip[0].variants) == 5 and hip[0].variants[3].endswith("/0003/Kernels.hsaco")


def test_different_errors_are_not_grouped():
    log = "hipModuleLoad failed: /tmp/a.hsaco\nhipMalloc failed: out of memory\n"
    assert len(extract_errors(log).candidates) == 2


def test_real_tensilelite_log_groups_66_module_load_failures():
    log = (FIXTURES / "run-39385542565_job-118506197594.txt").read_text(encoding="utf-8")
    first = extract_errors(log).first_error
    assert first.kind == "hip_runtime_error" and first.count == 66
    assert first.text.startswith("hipModuleLoad failed:")


def test_ctest_segfault_line_gives_failing_test():
    log = ("1349/2256 Test #1691: tests.integration.execute.rocprofv3-test-hip-graph "
           "....***Exception: SegFault  0.69 sec\n")
    r = extract_errors(log)
    assert r.first_error.kind == "crash"
    assert r.failing_test == "tests.integration.execute.rocprofv3-test-hip-graph"


def test_suite_summary_failures_are_extracted_in_full():
    log = (
        "2026-10-19 20:29:42,057 - INFO: [X] Total Failed Tests: 2\n"
        "2026-10-19 20:29:42,057 - INFO: [~] Flaky Tests (passed after retry) (1):\n"
        "2026-10-19 20:29:42,057 - INFO:        gdb.rocm/register-watchpoint.exp\n"
        "2026-10-19 20:29:42,057 - INFO: [X] Unexpected Failures (2):\n"
        "2026-10-19 20:29:42,057 - INFO:        gdb.rocm/multi-inferior-fork.exp\n"
        "2026-10-19 20:29:42,057 - INFO:        gdb.rocm/multi-inferior-gpu.exp\n"
        "2026-10-19 20:29:42,057 - INFO: [!] Unused Ignored Failures (1):\n"
        "2026-10-19 20:29:42,057 - INFO:        gdb.dwarf2/pr13961.exp\n"
        "Cleaning up orphan processes\n"
    )
    r = extract_errors(log)
    assert r.first_error.kind == "test_suite_failed"
    assert r.suite_failed == ["gdb.rocm/multi-inferior-fork.exp", "gdb.rocm/multi-inferior-gpu.exp"]
    assert r.failing_test == "gdb.rocm/multi-inferior-fork.exp"
    assert not any("Cleaning up" in c.text for c in r.candidates)


def test_dejagnu_fail_lines_give_test_and_reason():
    log = (
        "Running .../gdb.rocm/multi-inferior-gpu.exp ...\n"
        "FAIL: gdb.rocm/multi-inferior-gpu.exp: wait for gpu stops (timeout)\n"
        "FAIL: gdb.rocm/multi-inferior-fork.exp: stop_at=kernel: run\n"
        "FAIL: gdb.rocm/multi-inferior-gpu.exp: wait for gpu stops (timeout)\n"  # retry
        "2026-10-19 20:29:42,057 - INFO: [X] Unexpected Failures (2):\n"
        "2026-10-19 20:29:42,057 - INFO:        gdb.rocm/multi-inferior-fork.exp\n"
        "2026-10-19 20:29:42,057 - INFO:        gdb.rocm/multi-inferior-gpu.exp\n"
    )
    r = extract_errors(log)
    fails = [c for c in r.candidates if c.kind == "dejagnu_fail"]
    assert [c.text for c in fails] == [
        "FAIL: gdb.rocm/multi-inferior-gpu.exp: wait for gpu stops (timeout)",
        "FAIL: gdb.rocm/multi-inferior-fork.exp: stop_at=kernel: run",
    ]
    assert fails[0].count == 2  # failed again on retry; both occurrences kept
    assert r.first_error.kind == "dejagnu_fail"  # per-test reason outranks the summary
    assert r.failing_test == "gdb.rocm/multi-inferior-gpu.exp"
    assert r.suite_failed == ["gdb.rocm/multi-inferior-fork.exp", "gdb.rocm/multi-inferior-gpu.exp"]


def test_real_rocgdb_log():
    log = (FIXTURES / "rocgdb_suite_summary_run-39314226932.txt").read_text(encoding="utf-8")
    r = extract_errors(log)
    assert r.first_error.kind == "test_suite_failed"
    assert r.suite_failed == ["gdb.rocm/multi-inferior-fork.exp", "gdb.rocm/multi-inferior-gpu.exp"]


def test_window_is_clamped_to_log_bounds():
    result = extract_errors("Segmentation fault\n")
    assert result.first_error.window == (1, 1)


def test_byte_order_mark_before_timestamp_is_stripped():
    from rocksentinel.therock_ci.extract import normalize_line
    assert normalize_line("﻿2026-10-20T23:24:28.9432368Z error: unable to write file x") == (
        "error: unable to write file x")
