"""Triage agent: classify and attribute one failed job (mini model tier).

Tools (read-only): therock-ci get_job_log_windows / map_job_to_family / job_history and
memory lookup_signature / search_similar_failures. Instructions come from the versioned
skill skills/triage-therock/SKILL.md. The escalation rule to the RCA agent is code.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from rocksentinel.baseline.rules import CATEGORIES
from rocksentinel.harness.loop import AgentResult, run_agent
from rocksentinel.harness.policy import AgentSpec, Tool
from rocksentinel.llm.provider import LLMProvider, ToolSpec

SKILL = Path(__file__).resolve().parents[3] / "skills" / "triage-therock" / "SKILL.md"
RCA_CONFIDENCE_BELOW = 0.7
RCA_CATEGORIES = frozenset({"regression", "unknown"})

Category = Literal[CATEGORIES]  # type: ignore[valid-type]


class Evidence(BaseModel):
    line_no: int | None = None
    text: str = Field(min_length=1, max_length=2000)


class TriageResult(BaseModel):
    category: Category
    component: str | None = None
    gpu_family: str | None = None
    platform: str | None = None
    failing_test: str | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_rca: bool = False
    rationale: str = Field(default="", max_length=600)


TOOL_DOCS = {
    "get_job_log_windows": ("Fetch a failed job's log and return ranked error windows.",
                            {"job_id": "integer", "max_candidates": "integer"}),
    "map_job_to_family": ("Parse a job name into component, GPU family, platform, shard.",
                          {"workflow": "string", "job_name": "string", "runner_label": "string"}),
    "job_history": ("Failing-since, flake evidence and same-run co-failures for a job.",
                    {"job_id": "integer"}),
    "lookup_signature": ("Known failure signature + learned instinct for an error line.",
                         {"error_text": "string", "component": "string"}),
    "search_similar_failures": ("Past failures similar to an error line.",
                                {"error_text": "string", "k": "integer"}),
}


def default_tools() -> dict[str, Callable]:
    from rocksentinel.memory import server as memory
    from rocksentinel.therock_ci import server as therock

    return {
        "get_job_log_windows": therock.get_job_log_windows,
        "map_job_to_family": therock.map_job_to_family,
        "job_history": therock.job_history,
        "lookup_signature": memory.lookup_signature,
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
    return AgentSpec(
        name="triage",
        system_prompt=SKILL.read_text(encoding="utf-8"),
        tools=tools,
        output_model=TriageResult,
        required_first_tool="get_job_log_windows",
        max_steps=8,
        max_tool_calls=6,
        max_input_tokens=12_000,
    )


def task_text(job: dict) -> str:
    return (
        "Triage this failed CI job.\n"
        f"repo: {job.get('repo', 'ROCm/TheRock')}\nrun_id: {job.get('run_id')}\n"
        f"job_id: {job.get('job_id')}\nworkflow: {job.get('workflow')}\n"
        f"job_name: {job.get('job_name')}\nrunner_label: {job.get('runner_label')}"
    )


def apply_escalation(result: TriageResult) -> TriageResult:
    """Code-enforced rule: low confidence or regression/unknown always goes to RCA."""
    if result.confidence < RCA_CONFIDENCE_BELOW or result.category in RCA_CATEGORIES:
        return result.model_copy(update={"needs_rca": True})
    return result


def triage(job: dict, provider: LLMProvider,
           tool_fns: dict[str, Callable] | None = None) -> AgentResult:
    res = run_agent(build_spec(tool_fns), provider, task_text(job))
    if res.output is not None:
        res.output = apply_escalation(res.output)
    return res
