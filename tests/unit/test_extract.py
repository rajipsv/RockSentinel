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


def test_rocsparse_exit_code_ranks_last(rocsparse):
    assert rocsparse.candidates[-1].kind == "exit_code"


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


def test_window_is_clamped_to_log_bounds():
    result = extract_errors("Segmentation fault\n")
    assert result.first_error.window == (1, 1)
