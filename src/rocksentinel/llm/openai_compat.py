"""OpenAI-compatible chat-completions provider (one class, configured by environment).

Works with any server that speaks the OpenAI chat-completions + tools API:
- Ollama locally:      LLM_BASE_URL=http://localhost:11434/v1   (no key)
- OpenAI:              LLM_BASE_URL=https://api.openai.com/v1   LLM_API_KEY=...
- Azure OpenAI (v1):   LLM_BASE_URL=https://<resource>.openai.azure.com/openai/v1
                       LLM_API_KEY=... (or an Entra token later via managed identity)
Plus LLM_MODEL (e.g. "qwen2.5:7b", "gpt-4.1-mini" or an Azure deployment name).
Settings come from the environment, else from the repo's git-ignored .env.

Also: RecordingProvider wraps any provider and saves each turn, so a live run can be
replayed later by ReplayProvider (deterministic evals, no repeated model cost).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from rocksentinel.github.client import _DEFAULT_ENV_FILE
from rocksentinel.llm.provider import LLMProvider, LLMResponse, ToolCall, ToolSpec


class LLMError(RuntimeError):
    pass


def _env(name: str, default: str | None = None, env_file: Path | None = None) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    path = env_file or _DEFAULT_ENV_FILE
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = raw.strip().partition("=")
            if sep and key.strip() == name and value.strip():
                return value.strip().strip('"').strip("'")
    return default


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"],
                                                     "arguments": json.dumps(c["arguments"])}}
                                       for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description,
        "parameters": t.parameters or {"type": "object", "properties": {}}}} for t in tools]


class OpenAICompatibleProvider:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, transport: httpx.BaseTransport | None = None,
                 temperature: float = 0.0, max_retries: int = 3, timeout_s: float = 120):
        self.base_url = (base_url or _env("LLM_BASE_URL") or "").rstrip("/")
        self.model = model or _env("LLM_MODEL") or ""
        if not self.base_url or not self.model:
            raise LLMError("set LLM_BASE_URL and LLM_MODEL (environment or .env)")
        key = api_key if api_key is not None else _env("LLM_API_KEY")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
            headers["api-key"] = key            # Azure OpenAI accepts this header
        self._http = httpx.Client(headers=headers, timeout=timeout_s, transport=transport)
        self.temperature = temperature
        self.max_retries = max_retries

    def complete(self, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        body: dict = {"model": self.model, "messages": _to_openai_messages(messages),
                      "temperature": self.temperature}
        if tools:
            body["tools"] = _to_openai_tools(tools)
        for attempt in range(self.max_retries + 1):
            resp = self._http.post(f"{self.base_url}/chat/completions", json=body)
            if resp.status_code in (429, 500, 502, 503) and attempt < self.max_retries:
                time.sleep(float(resp.headers.get("retry-after", 2 ** attempt)))
                continue
            break
        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            raw_args = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {"_unparsed_arguments": raw_args}
            calls.append(ToolCall(c.get("id") or f"call-{len(calls)}", c["function"]["name"], args))
        usage = data.get("usage") or {}
        return LLMResponse(content=msg.get("content"), tool_calls=tuple(calls),
                           input_tokens=usage.get("prompt_tokens", 0),
                           output_tokens=usage.get("completion_tokens", 0))

    def close(self) -> None:
        self._http.close()


class RecordingProvider:
    """Wraps a live provider and records every turn for later ReplayProvider runs."""

    def __init__(self, inner: LLMProvider):
        self.inner = inner
        self.model = getattr(inner, "model", "")
        self.turns: list[dict] = []

    def complete(self, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        r = self.inner.complete(messages, tools)
        self.turns.append({"content": r.content, "input_tokens": r.input_tokens,
                           "output_tokens": r.output_tokens,
                           "tool_calls": [asdict(c) for c in r.tool_calls]})
        return r

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"model": self.model, "turns": self.turns}, indent=2),
                              encoding="utf-8")
