"""The `rocm` pack: AMD ROCm / TheRock-style GPU CI.

GPU memory faults, hardware exceptions and hang dumps, HIP runtime errors, gfx
families in job names, TheRock component paths and kernel namespaces, bump and
packaging workflows, and submodule-bump tracing (`find_introducing_change`).
"""

from __future__ import annotations

from pathlib import Path

from rocksentinel.packs.base import Pack, pattern
from rocksentinel.packs.rocm.mappers import map_job_to_family, map_symbol_to_component

_SKILLS = Path(__file__).resolve().parents[4] / "skills"

PATTERNS = (
    pattern("gpu_memory_fault", 100,
            r"Memory access fault by GPU|HSA_STATUS_ERROR_MEMORY_FAULT"),
    # The HSA runtime's hardware exception, which aborts the process on the spot:
    #   "HW Exception by GPU node-1 (Agent handle: 0x...) reason :GPU Hang"
    pattern("gpu_hw_exception", 96, r"HW Exception by GPU node-\d+.*reason\s*:"),
    # The hang-analysis dump. It also appears inside memory-fault dumps and when a hung test
    # is killed for running too long, so on its own it means "hung", not "what caused it".
    pattern("gpu_hang", 95, r"\*\*\* GPU HANG ANALYSIS"),
    # HIP runtime API failures, e.g. "hipModuleLoad failed: <path to .hsaco>"
    pattern("hip_runtime_error", 88, r"\bhip[A-Z][A-Za-z]+ failed\b|\bhipError[A-Z][A-Za-z]+"),
)

ROCM = Pack(
    name="rocm",
    description="AMD ROCm GPU CI (TheRock-style job names, GPU faults, HIP errors).",
    patterns=PATTERNS,
    root_cause_kinds=frozenset({"gpu_memory_fault", "gpu_hw_exception"}),
    code_kinds=frozenset({"gpu_memory_fault", "gpu_hw_exception", "hip_runtime_error"}),
    hang_kinds=frozenset({"gpu_hang"}),
    fault_kinds=frozenset({"gpu_memory_fault", "gpu_hw_exception", "gpu_hang"}),
    soft_kinds=frozenset({"hip_runtime_error"}),
    noise_prefixes=("kpack:",),  # the kpack code-object loader: high volume, never the cause
    category_hints=(
        ("bump_submodules.yml", "submodule-bump"),
        ("compiler_bump.yml", "submodule-bump"),
        ("*native_linux_packages*", "packaging"),
        ("*native_windows_packages*", "packaging"),
        ("*packages_install*", "packaging"),
        ("*python_packages*", "packaging"),
        ("*wheels*", "packaging"),
    ),
    job_mapper=map_job_to_family,
    symbol_mapper=lambda s: map_symbol_to_component(s).component,
    tool_names=("map_path_to_component", "find_introducing_change"),
    skills={"triage": _SKILLS / "triage-rocm" / "SKILL.md",
            "rca": _SKILLS / "rca-rocm" / "SKILL.md"},
)
