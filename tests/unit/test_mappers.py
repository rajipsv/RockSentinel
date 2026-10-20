import pytest

from rocksentinel.therock_ci.mappers import (
    map_job_to_family,
    map_path_to_component,
    map_symbol_to_component,
)

# Real job names seen in TheRock CI / HUD on 2026-10-19.
JOB_CASES = [
    (
        "test_artifacts.yml",
        "Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)",
        "linux-gfx1151-gpu-rocm",
        dict(component="rocsparse", gpu_family="gfx1151", platform="linux", stage="test",
             shard="2/3", variant=None),
    ),
    (
        "multi_arch_ci.yml",
        "Linux::release / Test gfx90a / Test rocfft / Test rocfft (gfx90a)",
        None,
        dict(component="rocfft", gpu_family="gfx90a", platform="linux", stage="test",
             shard=None, variant=None),
    ),
    (
        "multi_arch_ci.yml",
        "Windows::release / Test gfx110X-all / Test hip-tests (ROCR) / Test hip-tests (ROCR) "
        "(gfx110X-all)",
        None,
        dict(component="hip-tests (ROCR)", gpu_family="gfx110X-all", platform="windows",
             stage="test", shard=None, variant=None),
    ),
    (
        "therock-multi-arch-ci-asan-nightly.yml",
        "Linux::asan / Test gfx94X-dcgpu / Test miopen-dbsync / Test miopen-dbsync (gfx94X-dcgpu)",
        None,
        dict(component="miopen-dbsync", gpu_family="gfx94X-dcgpu", platform="linux",
             stage="test", shard=None, variant="asan"),
    ),
    (
        "compiler_bump.yml",
        "Build Windows Release / math-libs (gfx110X-all, gfx1100,gfx1101,gfx1102,gfx1103)",
        None,
        dict(component="math-libs", gpu_family="gfx110X-all", platform="windows",
             stage="build", shard=None, variant=None),
    ),
    (
        "multi_arch_ci.yml",
        "Linux::release / Build Multi-Arch Stages / wsl-rocdxg / Stage - WSL ROCDXG",
        None,
        dict(component="wsl-rocdxg", gpu_family=None, platform="linux", stage="build",
             shard=None, variant=None),
    ),
    (
        "multi_arch_ci.yml",
        "CI Summary",
        None,
        dict(component=None, gpu_family=None, platform=None, stage="summary",
             shard=None, variant=None),
    ),
]


@pytest.mark.parametrize(("workflow", "job", "runner", "expected"), JOB_CASES,
                         ids=[c[1][:40] for c in JOB_CASES])
def test_map_job_to_family(workflow, job, runner, expected):
    assert map_job_to_family(workflow, job, runner).as_dict() == expected


def test_platform_unknown_without_hints():
    info = map_job_to_family("test_artifacts.yml", "Test rocblas / Test rocblas (gfx1151)")
    assert info.platform is None
    assert info.gpu_family == "gfx1151"


@pytest.mark.parametrize(
    ("path", "component", "group"),
    [
        ("math-libs/BLAS/rocSPARSE/stage/lib/librocsparse.so.1.0", "rocSPARSE", "math-libs"),
        ("/__w/TheRock/TheRock/build/math-libs/BLAS/rocSPARSE/stage/bin/x", "rocSPARSE",
         "math-libs"),
        ("C:\\w\\TheRock\\build\\ml-libs\\MIOpen\\build\\src\\conv.cpp", "MIOpen", "ml-libs"),
        ("core/clr/build/hipamd/src/hip_module.cpp", "clr", "core"),
        ("compiler/amd-llvm/stamp/build.stamp", "amd-llvm", "compiler"),
    ],
)
def test_map_path_to_component(path, component, group):
    info = map_path_to_component(path)
    assert (info.component, info.group, info.source) == (component, group, "path")


def test_map_path_unknown():
    assert map_path_to_component("/usr/lib/libc.so").source == "none"


@pytest.mark.parametrize(
    ("symbol", "component"),
    [
        ("rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>", "rocSPARSE"),
        ("void rocblas::gemm_kernel<float>", "rocBLAS"),
        ("miopen::ConvSolver::Run", "MIOpen"),
        ("rocfft_execute", "rocFFT"),
        ("some_unknown_fn", None),
    ],
)
def test_map_symbol_to_component(symbol, component):
    assert map_symbol_to_component(symbol).component == component
