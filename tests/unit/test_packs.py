"""Packs: composition, and what a repo with only the core pack sees (no ROCm knowledge)."""

import pytest

from rocksentinel.baseline.rules import classify
from rocksentinel.ci_evidence.extract import extract_errors
from rocksentinel.packs import PACKS, default_profile, get_pack, profile

CORE_ONLY = profile([])
ROCM = profile(["rocm"])


def test_core_is_always_last_and_names_are_deduplicated():
    assert profile(["rocm", "core", "rocm"]).names == ("rocm", "core")
    assert CORE_ONLY.names == ("core",)
    assert default_profile().names == ("rocm", "core")


def test_unknown_pack_is_a_clear_error():
    with pytest.raises(KeyError, match="unknown pack 'nope'"):
        get_pack("nope")


def test_every_pack_kind_role_refers_to_a_pattern_kind():
    kinds = {pt.kind for p in PACKS.values() for pt in p.patterns}
    for p in PACKS.values():
        for role in ("root_cause_kinds", "code_kinds", "hang_kinds", "infra_kinds",
                     "fault_kinds", "soft_kinds"):
            assert getattr(p, role) <= kinds, (p.name, role)


def test_gpu_lines_are_only_specific_with_the_rocm_pack():
    log = ("[ RUN      ] Matmul.f64\n"
           "HW Exception by GPU node-1 (Agent handle: 0x5e) reason :GPU Hang\n"
           "\t  3 - hipblaslt-test (Subprocess aborted)\n")
    assert extract_errors(log, profile=ROCM).first_error.kind == "gpu_hw_exception"
    # core alone: the process abort is still the root signal, the hang is just "an exception"
    core = extract_errors(log, profile=CORE_ONLY)
    assert core.first_error.kind == "crash"
    assert "gpu_hw_exception" not in {c.kind for c in core.candidates}
    assert core.failing_test == "Matmul.f64"


def test_workflow_hints_come_from_packs():
    kinds = ["build_error"]
    assert classify(kinds, workflow="bump_submodules.yml", profile=ROCM)[0] == "submodule-bump"
    assert classify(kinds, workflow="release_python_packages.yml",
                    profile=ROCM)[0] == "packaging"
    # another company's repo has no such convention: it's a plain regression
    assert classify(kinds, workflow="bump_submodules.yml", profile=CORE_ONLY)[0] == "regression"


def test_hip_errors_are_not_a_code_signal_without_the_rocm_pack():
    assert classify(["hip_runtime_error"], workflow="ci.yml", profile=ROCM)[0] == "regression"
    assert classify(["hip_runtime_error"], workflow="ci.yml", profile=CORE_ONLY)[0] == "unknown"


@pytest.mark.parametrize("job,runner,expected", [
    ("build (ubuntu-latest, 3.12) / test", None, ("test", "linux", None)),
    ("Tests / unit (shard 2/4)", "windows-2022", ("unit", "windows", "2/4")),
    ("lint", "macos-14", ("lint", "macos", None)),
])
def test_core_job_mapper_reads_generic_job_names(job, runner, expected):
    info = CORE_ONLY.map_job("ci.yml", job, runner)
    assert (info.component, info.platform, info.shard) == expected
    assert info.gpu_family is None


def test_rocm_profile_maps_therock_job_names_and_kernels():
    info = ROCM.map_job("test_artifacts.yml",
                        "Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)", None)
    assert (info.component, info.gpu_family, info.shard) == ("rocsparse", "gfx1151", "2/3")
    assert ROCM.map_symbol("rocsparse::csrgemm_bitmap_emit_kernel<256u>") == "rocSPARSE"
    assert CORE_ONLY.map_symbol("rocsparse::csrgemm_bitmap_emit_kernel<256u>") is None


def test_skills_and_tools_come_from_the_rocm_pack():
    assert ROCM.skill("triage").name == "SKILL.md" and ROCM.skill("triage").is_file()
    assert CORE_ONLY.skill("triage") is None
    assert "find_introducing_change" in ROCM.tool_names
    assert CORE_ONLY.tool_names == ()
