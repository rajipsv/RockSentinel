"""Step 3: B0/B1 non-AI baselines and the eval runner."""

import json

import pytest

from rocksentinel.baseline.rules import classify
from rocksentinel.baseline.systems import b0_excerpt, run_b0, run_b1
from rocksentinel.evals.runner import (
    evaluate,
    format_table,
    grounding,
    load_cases,
    macro_f1,
    norm_component,
)

# --- rules ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("kinds", "workflow", "variant", "flaky", "expected"),
    [
        (["asan"], "multi_arch_ci_asan.yml", "asan", False, "asan"),
        (["infra", "crash"], "test_artifacts.yml", None, False, "infra"),
        (["killed"], "test_artifacts.yml", None, False, "infra"),
        (["test_timeout"], "test_artifacts.yml", None, False, "timeout"),
        (["step_timeout"], "test_artifacts.yml", None, False, "timeout"),
        (["infra", "step_timeout"], "test_artifacts.yml", None, False, "infra"),  # runner died
        (["test_timeout"], "test_artifacts.yml", None, True, "flaky"),  # passed on rerun
        (["crash", "test_timeout"], "test_artifacts.yml", None, False, "regression"),
        (["test_timeout"], "compiler_bump.yml", None, False, "timeout"),
        (["crash"], "test_artifacts.yml", None, True, "flaky"),
        (["infra"], "test_artifacts.yml", None, True, "infra"),  # infra beats flaky
        (["build_error"], "compiler_bump.yml", None, False, "submodule-bump"),
        (["build_error"], "multi_arch_build_native_linux_packages.yml", None, False, "packaging"),
        (["gpu_memory_fault", "gpu_hang"], "test_artifacts.yml", None, False, "regression"),
        (["gtest_failed"], "test_artifacts.yml", None, False, "regression"),
        (["exit_code"], "test_artifacts.yml", None, False, "unknown"),
        ([], "test_artifacts.yml", None, False, "unknown"),
    ],
)
def test_classify_rules(kinds, workflow, variant, flaky, expected):
    category, reason = classify(kinds, workflow=workflow, variant=variant, likely_flaky=flaky)
    assert category == expected and reason


def test_gpu_hang_alone_is_timeout():
    assert classify(["gpu_hang"], workflow="test_artifacts.yml")[0] == "timeout"


# --- B0 excerpt (TheRock detect_failed_logs.py behaviour) -----------------------------------

def test_b0_takes_last_keyword_match_with_12_before_20_after():
    lines = [f"line {i}" for i in range(100)]
    lines[30] = "x.cpp:1:1: error: first"
    lines[60] = "FAILED: last"
    excerpt = b0_excerpt("\n".join(lines))
    assert excerpt[0] == "line 48" and excerpt[12] == "FAILED: last" and excerpt[-1] == "line 80"


def test_b0_falls_back_to_last_32_lines():
    lines = [f"line {i}" for i in range(100)]
    assert b0_excerpt("\n".join(lines)) == lines[-32:]


# --- systems on the real golden logs ---------------------------------------------------------

@pytest.fixture(scope="module")
def cases():
    return {c["case_id"]: c for c in load_cases()}


def _log(case):
    from rocksentinel.evals.runner import ROOT
    return (ROOT / case["log_fixture"]).read_text(encoding="utf-8")


def test_b1_on_rocsparse(cases):
    c = cases["golden-001"]
    p = run_b1(_log(c), c["workflow"], c["job_name"], c["runner_label"])
    assert p.category == "regression"
    assert p.faulting_kernel == c["expected"]["failing_kernel"]
    assert grounding(c["expected"], p) == 1.0


def test_b0_misses_part_of_rocsparse_evidence(cases):
    c = cases["golden-001"]
    p = run_b0(_log(c), c["workflow"], c["job_name"], c["runner_label"])
    assert p.failing_test is None and p.faulting_kernel is None
    assert grounding(c["expected"], p) < 1.0


# --- metrics + runner -----------------------------------------------------------------------

def test_macro_f1():
    assert macro_f1(["a", "a", "b"], ["a", "a", "b"]) == 1.0
    # a: P=1,R=0.5,F1=.667 ; b: P=.5,R=1,F1=.667 -> .667
    assert macro_f1(["a", "a", "b"], ["a", "b", "b"]) == 0.667
    assert macro_f1([], []) == 0.0


@pytest.mark.parametrize(
    ("name", "norm"),
    [("rocSPARSE", "rocsparse"), ("hip-tests (HIP runtime / hiprtc)", "hip-tests"), (None, None)],
)
def test_norm_component(name, norm):
    assert norm_component(name) == norm


def test_evaluate_golden_set(cases):
    report = evaluate([cases["golden-001"], cases["golden-002"]])
    b0, b1 = report["systems"]["B0"], report["systems"]["B1"]
    assert report["n_cases"] == 2  # this test pins the two hand-built cases only
    assert b1["category_accuracy"] == 1.0
    assert b1["failing_test_match"] == 1.0 and b1["faulting_kernel_match"] == 1.0
    assert b1["grounding_rate"] == 1.0
    assert b0["grounding_rate"] < b1["grounding_rate"]
    assert {r["case_id"] for r in report["cases"]} == {"golden-001", "golden-002"}
    table = format_table(report)
    assert "grounding_rate" in table and "B0" in table and "B1" in table


def test_whole_golden_set_runs(cases):
    report = evaluate(list(cases.values()))
    assert report["n_cases"] == len(cases) >= 2
    assert all(0.0 <= (v or 0) <= 1.0 for s in report["systems"].values() for v in s.values())


def test_confirmed_only_filters(cases, tmp_path):
    data = tmp_path / "d.jsonl"
    rows = [dict(cases["golden-001"], label_status="confirmed"), cases["golden-002"]]
    data.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert [c["case_id"] for c in load_cases(data, confirmed_only=True)] == ["golden-001"]
