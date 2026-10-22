"""Packs: pluggable CI knowledge (patterns, kind roles, job-name mapping, skills).

    from rocksentinel.packs import profile
    p = profile(["rocm"])          # rocm + core; core is always included, last
    p.map_job(workflow, job_name, runner_label)
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache

from rocksentinel.packs.base import JobInfo, Pack, Pattern, Profile, pattern
from rocksentinel.packs.core import CORE
from rocksentinel.packs.rocm import ROCM

PACKS: dict[str, Pack] = {p.name: p for p in (CORE, ROCM)}

# Until per-repo config exists (roadmap P2), everything runs with the ROCm profile.
DEFAULT_PACKS: tuple[str, ...] = ("rocm",)

__all__ = ["DEFAULT_PACKS", "PACKS", "JobInfo", "Pack", "Pattern", "Profile", "default_profile",
           "get_pack", "pattern", "profile"]


def get_pack(name: str) -> Pack:
    try:
        return PACKS[name]
    except KeyError:
        raise KeyError(f"unknown pack {name!r}; known: {sorted(PACKS)}") from None


@lru_cache(maxsize=64)
def _profile(names: tuple[str, ...]) -> Profile:
    domain = [get_pack(n) for n in dict.fromkeys(names) if n != "core"]
    return Profile(packs=(*domain, CORE))


def profile(names: Iterable[str] = ()) -> Profile:
    """Combine packs: the named domain packs in order, then `core`."""
    return _profile(tuple(names))


def default_profile() -> Profile:
    return profile(DEFAULT_PACKS)
