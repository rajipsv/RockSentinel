"""The LLMProvider interface and a replay provider.

Agents depend on this interface, not on a vendor. Implementations:
- ReplayProvider: returns recorded responses in order (deterministic, free, offline);
  used by tests and offline evals.
- (later) an OpenAI-compatible provider for GitHub Models during development and
  Azure OpenAI with managed identity in Azure: same interface, config-only switch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class LLMResponse:
    content: str | None = None                       # final answer text (JSON for agents)
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)   # JSON schema of the arguments


class LLMProvider(Protocol):
    model: str

    def complete(self, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse: ...


class ReplayExhausted(RuntimeError):
    pass


class ReplayProvider:
    """Plays back a recorded conversation: one LLMResponse per complete() call."""

    def __init__(self, responses: list[LLMResponse], model: str = "replay"):
        self.model = model
        self._responses = list(responses)
        self.calls: list[list[dict]] = []   # messages seen at each call, for assertions

    def complete(self, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            raise ReplayExhausted("no more recorded responses")
        return self._responses.pop(0)

    @classmethod
    def from_file(cls, path: str | Path) -> ReplayProvider:
        """Load a recording: a JSON list of {content?, tool_calls?: [{id,name,arguments}]}."""
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([
            LLMResponse(
                content=r.get("content"),
                tool_calls=tuple(ToolCall(t["id"], t["name"], t.get("arguments", {}))
                                 for t in r.get("tool_calls", [])),
                input_tokens=r.get("input_tokens", 0),
                output_tokens=r.get("output_tokens", 0),
            )
            for r in rows
        ])
