"""Root-Cause & Fix Advisor (RCA) agent: why it failed, which change, what next (strong model tier).

Runs only for cases Triage escalated (regression/unknown or low confidence). Read-only
tools: therock-ci get_job_log_windows / job_history / find_introducing_change /
map_path_to_component and memory search_similar_failures. Skill: skills/rca-therock.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from rocksentinel.agents.triage import Evidence, TriageResult
from rocksentinel.harness.loop import AgentResult, run_agent
from rocksentinel.harness.policy import AgentSpec, Tool
from rocksentinel.llm.provider import LLMProvider, ToolSpec

SKILL = Path(__file__).resolve().parents[3] / "skills" / "rca-therock" / "SKILL.md"


class SuspectedChange(BaseModel):
    repo: str
    commit: str
    pr: str | None = None
    therock_bump_pr: str | None = None


class RcaResult(BaseModel):
    root_cause_hypothesis: str = Field(min_length=1, max_length=1200)
    suspected_change: SuspectedChange | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    suggested_next_step: str = Field(min_length=1, max_length=800)
    confidence: float = Field(ge=0.0, le=1.0)


TOOL_DOCS = {
    "get_job_log_windows": ("Fetch a failed job's log and return ranked error windows.",
                            {"job_id": "integer", "max_candidates": "integer"}),
    "job_history": ("Failing-since, flake evidence and same-run co-failures for a job.",
                    {"job_id": "integer"}),
    "find_introducing_change": ("Trace a kernel/function/test name to submodule commits and "
                                "the TheRock bump PR that brought each in.",
                                {"symbol": "string", "submodule_repo": "string",
                                 "submodule_path": "string", "since_days": "integer"}),
    "map_path_to_component": ("Map a build path or C++ symbol to its component.",
                              {"path_or_symbol": "string"}),
    "search_similar_failures": ("Past failures similar to an error line, with learned causes.",
                                {"error_text": "string", "k": "integer"}),
}


def default_tools() -> dict[str, Callable]:
    from rocksentinel.memory import server as memory
    from rocksentinel.therock_ci import server as therock

    return {
        "get_job_log_windows": therock.get_job_log_windows,
        "job_history": therock.job_history,
        "find_introducing_change": therock.find_introducing_change,
        "map_path_to_component": therock.map_path_to_component,
        "search_similar_failures": memory.search_similar_failures,
    }


def build_spec(tool_fns: dict[str, Callable] | None = None) -> AgentSpec:
    fns = tool_fns or default_tools()
    tools = {
        name: Tool(ToolSpec(name, TOOL_DOCS[name][0], {
            "type": "object",
            "properties": {k: {"type": v} for k, v in TOOL_DOCS[name][1].items()},
        }), fn)
        for name, fn in fns.items() if name in TOOL_DOCS
    }
    return AgentSpec(name="rca", system_prompt=SKILL.read_text(encoding="utf-8"), tools=tools,
                     output_model=RcaResult, max_steps=14, max_tool_calls=12,
                     max_input_tokens=40_000)


def task_text(job: dict, triage: TriageResult) -> str:
    return (
        "Find the root cause of this triaged CI failure.\n"
        f"job: {job.get('job_name')} ({job.get('workflow')})\n"
        f"repo: {job.get('repo', 'ROCm/TheRock')}  run_id: {job.get('run_id')}  "
        f"job_id: {job.get('job_id')}\n"
        f"triage: {triage.model_dump_json()}"
    )


def rca(job: dict, triage: TriageResult, provider: LLMProvider,
        tool_fns: dict[str, Callable] | None = None) -> AgentResult:
    return run_agent(build_spec(tool_fns), provider, task_text(job, triage))
