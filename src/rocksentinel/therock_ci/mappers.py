"""Map TheRock CI job names, file paths and symbols to component, GPU family and platform.

Job names follow the conventions of TheRock's workflows, for example:
  test_artifacts.yml   "Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)"
  multi_arch_ci.yml    "Linux::release / Test gfx90a / Test rocfft / Test rocfft (gfx90a)"
  compiler_bump.yml    "Build Windows Release / math-libs (gfx110X-all, gfx1100,gfx1101,...)"
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Top-level component groups of the TheRock super-project (see TheRock AGENTS.md).
COMPONENT_GROUPS = frozenset(
    {
        "base",
        "comm-libs",
        "compiler",
        "core",
        "cv-libs",
        "dctools",
        "debug-tools",
        "emulation",
        "math-libs",
        "media-libs",
        "ml-libs",
        "profiler",
        "storage-libs",
        "third-party",
    }
)

# Per-component build-tree directories; the segment before one of these is the component.
_BUILD_TREE_DIRS = frozenset({"build", "stage", "dist", "stamp"})

# C++ namespace / library prefix -> component name as TheRock spells it.
SYMBOL_PREFIXES: dict[str, str] = {
    "rocsparse": "rocSPARSE",
    "hipsparse": "hipSPARSE",
    "rocblas": "rocBLAS",
    "hipblaslt": "hipBLASLt",
    "hipblas": "hipBLAS",
    "rocsolver": "rocSOLVER",
    "rocfft": "rocFFT",
    "hipfft": "hipFFT",
    "rocrand": "rocRAND",
    "rocprim": "rocPRIM",
    "rocthrust": "rocThrust",
    "rocwmma": "rocWMMA",
    "miopen": "MIOpen",
    "rccl": "RCCL",
    "ck": "composable_kernel",
}

_FAMILY = re.compile(r"\bgfx[0-9][0-9A-Za-z]*(?:-(?:all|dcgpu|generic|igpu|dgpu))?")
_FAMILY_IN_PARENS = re.compile(r"\((gfx[0-9][0-9A-Za-z]*(?:-[a-z]+)?)")
_SHARD = re.compile(r"\(shard (\d+)/(\d+)\)")
_TEST_SEGMENT = re.compile(r"^Test (?!gfx)([A-Za-z0-9_.\-]+)(?: \(([A-Za-z]+)\))?")
_BUILD_GROUP = re.compile(r"^([a-z][a-z0-9\-]+) \(gfx")
# A bare lowercase slug segment after a "Build ..." segment names the build stage,
# e.g. "Linux::release / Build Multi-Arch Stages / wsl-rocdxg / Stage - WSL ROCDXG".
_STAGE_SLUG = re.compile(r"^[a-z][a-z0-9\-]+$")
_SYMBOL_HEAD = re.compile(r"^(?:void\s+)?([A-Za-z_][A-Za-z0-9_]*)::")


@dataclass(frozen=True)
class JobInfo:
    component: str | None
    gpu_family: str | None
    platform: str | None
    stage: str | None
    shard: str | None
    variant: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def _platform(job_name: str, workflow: str, runner_label: str | None) -> str | None:
    text = f"{job_name} {workflow}".lower()
    if "windows" in text:
        return "windows"
    if "linux" in text:
        return "linux"
    if runner_label:
        label = runner_label.lower()
        if label.startswith(("windows", "win-")) or "windows" in label:
            return "windows"
        if label.startswith("linux") or "linux" in label:
            return "linux"
    return None


def map_job_to_family(workflow: str, job_name: str, runner_label: str | None = None) -> JobInfo:
    """Parse a TheRock CI job name into component, GPU family, platform, stage and shard."""
    segments = [s.strip() for s in job_name.split(" / ")]

    component = None
    stage = None
    for seg in segments:
        m = _TEST_SEGMENT.match(seg)
        if m:
            component = m.group(1) if not m.group(2) else f"{m.group(1)} ({m.group(2)})"
            stage = "test"
        elif seg.startswith("Build"):
            stage = stage or "build"
        elif (g := _BUILD_GROUP.match(seg)) and g.group(1) in COMPONENT_GROUPS:
            component = component or g.group(1)
        elif stage == "build" and _STAGE_SLUG.match(seg):
            component = component or seg
    if "Summary" in job_name:
        stage = stage or "summary"

    # Prefer the family in parentheses at the end of the job name, then any gfx token.
    if m := _FAMILY_IN_PARENS.search(job_name):
        gpu_family = m.group(1)
    elif m := (_FAMILY.search(job_name) or _FAMILY.search(runner_label or "")):
        gpu_family = m.group(0)
    else:
        gpu_family = None

    shard_m = _SHARD.search(job_name)
    shard = f"{shard_m.group(1)}/{shard_m.group(2)}" if shard_m else None

    lowered = f"{job_name} {workflow}".lower()
    variant = "asan" if "asan" in lowered else None

    return JobInfo(
        component=component,
        gpu_family=gpu_family,
        platform=_platform(job_name, workflow, runner_label),
        stage=stage,
        shard=shard,
        variant=variant,
    )


@dataclass(frozen=True)
class ComponentInfo:
    component: str | None
    group: str | None
    source: str  # "path" | "symbol" | "none"

    def as_dict(self) -> dict:
        return asdict(self)


def map_path_to_component(path: str) -> ComponentInfo:
    """Map a TheRock build/source path to its component group and component.

    e.g. ".../build/math-libs/BLAS/rocSPARSE/stage/lib/librocsparse.so" -> rocSPARSE, math-libs
    """
    parts = [p for p in re.split(r"[\\/]", path) if p]
    start = next((i for i, p in enumerate(parts) if p in COMPONENT_GROUPS), None)
    if start is None:
        return ComponentInfo(component=None, group=None, source="none")

    group = parts[start]
    rest = parts[start + 1 :]
    stop = next((i for i, p in enumerate(rest) if p in _BUILD_TREE_DIRS), len(rest))
    component_dirs = rest[:stop]
    component = component_dirs[-1] if component_dirs else None
    return ComponentInfo(component=component, group=group, source="path")


def map_symbol_to_component(symbol: str) -> ComponentInfo:
    """Map a C++ symbol (e.g. a faulting kernel) to a component by its namespace prefix.

    e.g. "rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>" -> rocSPARSE
    """
    m = _SYMBOL_HEAD.match(symbol.strip())
    head = (m.group(1) if m else symbol.strip().split("_", 1)[0]).lower()
    component = SYMBOL_PREFIXES.get(head)
    return ComponentInfo(
        component=component, group=None, source="symbol" if component else "none"
    )
