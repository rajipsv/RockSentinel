"""The bounded agent loop (design §7.1): model -> pre_tool -> tool -> post_tool -> ... -> output.

Everything safety-relevant is enforced here in code: the tool allowlist, the required
first tool, step/tool/token budgets, secret redaction, the untrusted-data envelope,
schema validation with one repair attempt, and grounding of cited evidence.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from rocksentinel.harness.policy import (
    UNTRUSTED_DATA_RULE,
    AgentSpec,
    envelope,
    estimate_tokens,
    redact,
)
from rocksentinel.llm.provider import LLMProvider, ToolCall

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


@dataclass
class AgentResult:
    status: str                          # ok | budget_exceeded | invalid_output | error
    output: BaseModel | None = None
    steps: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)   # policy denials, repairs, grounding
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


def _pre_tool(spec: AgentSpec, call: ToolCall, used: list[str]) -> str | None:
    """Return a denial reason, or None if the call may run."""
    if call.name not in spec.tools:
        return f"tool '{call.name}' is not allowed for agent '{spec.name}'"
    if spec.required_first_tool and not used and call.name != spec.required_first_tool:
        return f"the first tool call must be '{spec.required_first_tool}'"
    if len(used) >= spec.max_tool_calls:
        return f"tool-call budget ({spec.max_tool_calls}) exhausted; give your final answer"
    return None


def _validate_args(spec: AgentSpec, call: ToolCall) -> tuple[dict, str | None, list[str]]:
    """Coerce and check arguments against the tool's schema and Python signature.

    Small models send numbers as strings or invent argument names; fixing obvious types
    and dropping unknown names here keeps tool calls deterministic. Returns
    (clean_args, error, dropped_names).
    """
    tool = spec.tools[call.name]
    props = (tool.spec.parameters or {}).get("properties", {})
    params = inspect.signature(tool.fn).parameters
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    clean, dropped = {}, []
    for name, value in (call.arguments or {}).items():
        if name not in params and not accepts_any:
            dropped.append(name)
            continue
        if props.get(name, {}).get("type") == "integer" and isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                return {}, f"argument '{name}' must be an integer", dropped
        clean[name] = value
    missing = [n for n, p in params.items()
               if p.default is inspect.Parameter.empty
               and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) and n not in clean]
    if missing:
        expected = ", ".join(params)
        error = f"missing required argument(s) {missing}; expected arguments: {expected}"
        return {}, error, dropped
    return clean, None, dropped


def _post_tool(spec: AgentSpec, result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    text = redact(text)
    if len(text) > spec.tool_output_cap:
        text = text[: spec.tool_output_cap] + f"\n... [truncated to {spec.tool_output_cap} chars]"
    return text


def _parse(spec: AgentSpec, content: str | None) -> BaseModel:
    raw = _FENCE.sub("", (content or "").strip())
    return spec.output_model.model_validate(json.loads(raw))


def _ground(spec: AgentSpec, output: BaseModel, seen: str, events: list[dict]) -> BaseModel:
    """Drop evidence the agent did not actually see in tool output; lower its confidence."""
    evidence = getattr(output, spec.evidence_field, None)
    if not evidence:
        return output
    kept = [e for e in evidence if e.text and e.text.strip() in seen]
    dropped = len(evidence) - len(kept)
    if not dropped:
        return output
    events.append({"type": "ungrounded_evidence_removed", "count": dropped})
    update: dict = {spec.evidence_field: kept}
    if hasattr(output, "confidence"):
        update["confidence"] = round(max(0.0, output.confidence - 0.2 * dropped), 3)
    return output.model_copy(update=update)


def run_agent(spec: AgentSpec, provider: LLMProvider, task: str) -> AgentResult:
    messages: list[dict] = [
        {"role": "system", "content": f"{spec.system_prompt}\n\n{UNTRUSTED_DATA_RULE}"},
        {"role": "user", "content": task},
    ]
    res = AgentResult(status="error", model=getattr(provider, "model", ""))
    used: list[str] = []
    seen_outputs: list[str] = []
    repaired = False

    while res.steps < spec.max_steps:
        if estimate_tokens(messages) > spec.max_input_tokens:
            res.status = "budget_exceeded"
            res.events.append({"type": "token_budget", "limit": spec.max_input_tokens})
            return res
        res.steps += 1
        # After an invalid final answer, the repair turn asks for strict JSON and offers no tools.
        reply = (provider.complete(messages, [], json_mode=True) if repaired
                 else provider.complete(messages, spec.tool_specs()))
        res.input_tokens += reply.input_tokens
        res.output_tokens += reply.output_tokens

        if reply.tool_calls:
            messages.append({"role": "assistant", "content": reply.content,
                             "tool_calls": [c.__dict__ for c in reply.tool_calls]})
            for call in reply.tool_calls:
                denial = _pre_tool(spec, call, used)
                args, arg_error, dropped = ({}, None, [])
                if not denial:
                    args, arg_error, dropped = _validate_args(spec, call)
                    if dropped:
                        res.events.append({"type": "args_dropped", "tool": call.name,
                                           "names": dropped})
                record = {"name": call.name, "arguments": call.arguments, "denied": denial}
                if denial:
                    res.events.append({"type": "policy_denied", "tool": call.name,
                                       "reason": denial})
                    content = f"policy_denied: {denial}"
                elif arg_error:
                    res.events.append({"type": "invalid_arguments", "tool": call.name,
                                       "reason": arg_error})
                    content = f"invalid_arguments: {arg_error}"
                else:
                    used.append(call.name)
                    try:
                        text = _post_tool(spec, spec.tools[call.name].fn(**args))
                    except Exception as exc:  # tool errors go back to the model as data
                        text = f"tool_error: {type(exc).__name__}: {exc}"
                    seen_outputs.append(text)
                    content = envelope(call.name, text)
                res.tool_calls.append(record)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            continue

        try:
            output = _parse(spec, reply.content)
        except (ValueError, ValidationError) as exc:
            if repaired:
                res.status = "invalid_output"
                res.events.append({"type": "invalid_output", "error": str(exc)[:300]})
                return res
            repaired = True
            res.events.append({"type": "repair_requested", "error": str(exc)[:300]})
            messages.append({"role": "assistant", "content": reply.content})
            messages.append({"role": "user", "content":
                             f"Your answer was not valid: {str(exc)[:300]}. Reply with ONLY "
                             "a JSON object matching the required schema."})
            continue

        res.output = _ground(spec, output, "\n".join(seen_outputs), res.events)
        res.status = "ok"
        return res

    res.status = "budget_exceeded"
    res.events.append({"type": "step_budget", "limit": spec.max_steps})
    return res
