"""Pack building blocks: log patterns, job-name info, and how packs combine into a profile.

A pack bundles everything that is specific to a kind of CI (a language, a test runner,
a hardware stack): log patterns, the role of each error kind for the classifier,
workflow-name hints, a job-name mapper, and agent skills. A repo's profile is the
combination of its packs (configured per repo), always ending with `core`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Pattern:
    kind: str
    severity: int  # higher = more likely the root cause, not a consequence
    regex: re.Pattern[str]


def pattern(kind: str, severity: int, expr: str) -> Pattern:
    return Pattern(kind, severity, re.compile(expr))


@dataclass(frozen=True)
class JobInfo:
    """What a job name says about the job."""

    component: str | None
    gpu_family: str | None  # hardware target (GPU family for ROCm); None when not relevant
    platform: str | None
    stage: str | None
    shard: str | None
    variant: str | None

    def as_dict(self) -> dict:
        return asdict(self)


JobMapper = Callable[[str, str, "str | None"], JobInfo]  # (workflow, job_name, runner_label)
SymbolMapper = Callable[[str], "str | None"]  # faulting symbol -> component


@dataclass(frozen=True)
class Pack:
    name: str
    description: str
    patterns: tuple[Pattern, ...] = ()
    # Roles of error kinds, used by the rule classifier and the extractor.
    root_cause_kinds: frozenset[str] = frozenset()  # a hang/timeout next to these is a consequence
    code_kinds: frozenset[str] = frozenset()        # code or test failure -> "regression"
    hang_kinds: frozenset[str] = frozenset()        # time limits -> "timeout"
    infra_kinds: frozenset[str] = frozenset()       # runner/infrastructure -> "infra"
    fault_kinds: frozenset[str] = frozenset()       # process died: name the running test
    soft_kinds: frozenset[str] = frozenset()        # ignorable inside a test that passed
    noise_prefixes: tuple[str, ...] = ()            # high-volume lines that are never the cause
    # (fnmatch glob on the workflow file name, category), checked in order.
    category_hints: tuple[tuple[str, str], ...] = ()
    job_mapper: JobMapper | None = None
    symbol_mapper: SymbolMapper | None = None
    tool_names: tuple[str, ...] = ()  # extra agent tools this pack provides
    skills: dict[str, Path] = field(default_factory=dict)  # agent name -> SKILL.md


@dataclass(frozen=True)
class Profile:
    """The packs of one repo, combined. Domain packs come first, `core` last."""

    packs: tuple[Pack, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.packs)

    @property
    def patterns(self) -> tuple[Pattern, ...]:
        return tuple(pt for p in self.packs for pt in p.patterns)

    def _union(self, attr: str) -> frozenset[str]:
        return frozenset().union(*(getattr(p, attr) for p in self.packs))

    @property
    def root_cause_kinds(self) -> frozenset[str]:
        return self._union("root_cause_kinds")

    @property
    def code_kinds(self) -> frozenset[str]:
        return self._union("code_kinds")

    @property
    def hang_kinds(self) -> frozenset[str]:
        return self._union("hang_kinds")

    @property
    def infra_kinds(self) -> frozenset[str]:
        return self._union("infra_kinds")

    @property
    def fault_kinds(self) -> frozenset[str]:
        return self._union("fault_kinds")

    @property
    def soft_kinds(self) -> frozenset[str]:
        return self._union("soft_kinds")

    @property
    def noise_prefixes(self) -> tuple[str, ...]:
        return tuple(n for p in self.packs for n in p.noise_prefixes)

    @property
    def category_hints(self) -> tuple[tuple[str, str], ...]:
        return tuple(h for p in self.packs for h in p.category_hints)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t for p in self.packs for t in p.tool_names)

    def skill(self, agent: str) -> Path | None:
        """The most specific pack's skill for an agent (domain packs before core)."""
        return next((p.skills[agent] for p in self.packs if agent in p.skills), None)

    def map_job(self, workflow: str, job_name: str, runner_label: str | None = None) -> JobInfo:
        mapper = next(p.job_mapper for p in self.packs if p.job_mapper is not None)
        return mapper(workflow, job_name, runner_label)

    def map_symbol(self, symbol: str) -> str | None:
        for p in self.packs:
            if p.symbol_mapper is not None and (component := p.symbol_mapper(symbol)):
                return component
        return None
