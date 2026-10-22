"""Per-repo configuration: which repos RockSentinel watches and how to read their CI.

Central config (product decision): admins onboard repos here, which also works for repos
they don't own. Locally it is `config/repos.toml`; in Azure the same model is stored in
Cosmos DB. No secrets ever go in this file.

    [settings]
    github_api_url = "https://api.github.com"   # GitHub Enterprise Server: https://<host>/api/v3
    excerpt_retention_days = 30

    [[repos]]
    repo = "ROCm/TheRock"
    packs = ["rocm"]            # domain packs; "core" is always added last
    branches = ["main"]         # or "all"
    workflows = "all"           # or ["ci.yml", ...]

    [[repos.job_name_rules]]    # optional: read this repo's job names
    match = '^test \\((?P<component>[^,]+), (?P<platform>[^)]+)\\)'
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rocksentinel.github.client import API_URL, validate_repo, validate_workflow
from rocksentinel.packs import PACKS, JobInfo, Pack, Profile, profile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "repos.toml"
_RULE_GROUPS = frozenset({"component", "platform", "hardware", "shard", "stage"})


class JobNameRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match: str

    @field_validator("match")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        try:
            groups = set(re.compile(v).groupindex)
        except re.error as exc:
            raise ValueError(f"invalid regex {v!r}: {exc}") from exc
        if not groups or not groups <= _RULE_GROUPS:
            raise ValueError(f"named groups must be a non-empty subset of {sorted(_RULE_GROUPS)}")
        return v


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    tenant: str | None = None  # customer org; defaults to the repo owner
    enabled: bool = True
    packs: list[str] = Field(default_factory=list)
    branches: list[str] | Literal["all"] = Field(default_factory=lambda: ["main"])
    workflows: list[str] | Literal["all"] = "all"
    job_name_rules: list[JobNameRule] = Field(default_factory=list)

    @field_validator("repo")
    @classmethod
    def _valid_repo(cls, v: str) -> str:
        return validate_repo(v)

    @field_validator("packs")
    @classmethod
    def _known_packs(cls, v: list[str]) -> list[str]:
        unknown = [p for p in v if p not in PACKS]
        if unknown:
            raise ValueError(f"unknown packs {unknown}; known: {sorted(PACKS)}")
        return v

    @field_validator("workflows")
    @classmethod
    def _valid_workflows(cls, v: list[str] | str) -> list[str] | str:
        if isinstance(v, list):
            for w in v:
                validate_workflow(w)
        return v

    @model_validator(mode="after")
    def _default_tenant(self) -> RepoConfig:
        if self.tenant is None:
            self.tenant = self.repo.split("/")[0]
        return self

    def branch_filters(self) -> list[str | None]:
        """Branches to poll; None means all branches."""
        return [None] if self.branches == "all" else list(self.branches)

    def workflow_filters(self) -> list[str | None]:
        return [None] if self.workflows == "all" else list(self.workflows)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    github_api_url: str = API_URL
    excerpt_retention_days: int = Field(default=30, ge=1, le=365)

    @field_validator("github_api_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("github_api_url must be https")
        return v.rstrip("/")


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: Settings = Field(default_factory=Settings)
    repos: list[RepoConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Config:
        names = [r.repo.lower() for r in self.repos]
        if len(names) != len(set(names)):
            raise ValueError("each repo may appear only once")
        return self

    def repo(self, name: str) -> RepoConfig | None:
        return next((r for r in self.repos if r.repo.lower() == name.lower()), None)

    def enabled(self) -> list[RepoConfig]:
        return [r for r in self.repos if r.enabled]


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG
    if not path.is_file():
        return Config()
    with path.open("rb") as fh:
        return Config.model_validate(tomllib.load(fh))


@lru_cache(maxsize=1)
def _default_config() -> Config:
    return load_config()


def _rules_pack(cfg: RepoConfig, base: Profile) -> Pack:
    """A per-repo pack whose job mapper tries the repo's own rules, then the packs' mapper."""
    rules = [re.compile(r.match) for r in cfg.job_name_rules]

    def mapper(workflow: str, job_name: str, runner_label: str | None = None) -> JobInfo:
        info = base.map_job(workflow, job_name, runner_label)
        for rx in rules:
            if m := rx.search(job_name):
                g = {k: v for k, v in m.groupdict().items() if v}
                return replace(info, component=g.get("component", info.component),
                               platform=g.get("platform", info.platform),
                               gpu_family=g.get("hardware", info.gpu_family),
                               shard=g.get("shard", info.shard),
                               stage=g.get("stage", info.stage))
        return info

    return Pack(name=f"repo:{cfg.repo}", description="per-repo job-name rules", job_mapper=mapper)


def profile_for(repo: str, config: Config | None = None) -> Profile:
    """The packs for a repo. Repos that aren't configured get the default profile."""
    config = config or _default_config()
    cfg = config.repo(repo)
    if cfg is None:
        from rocksentinel.packs import default_profile

        return default_profile()
    base = profile(cfg.packs)
    if not cfg.job_name_rules:
        return base
    return Profile(packs=(_rules_pack(cfg, base), *base.packs))
