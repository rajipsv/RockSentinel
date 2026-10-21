"""Step 6b: OpenAI-compatible provider + recorder, against a mocked HTTP server."""

import json

import httpx
import pytest

from rocksentinel.llm.openai_compat import LLMError, OpenAICompatibleProvider, RecordingProvider
from rocksentinel.llm.provider import ReplayProvider, ToolSpec

TOOLS = [ToolSpec("get_job_log_windows", "fetch windows",
                  {"type": "object", "properties": {"job_id": {"type": "integer"}}})]


def _server(responses, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body, headers={"retry-after": "0"})
    return httpx.MockTransport(handler)


def _provider(responses, seen, **kw):
    return OpenAICompatibleProvider(base_url="http://llm.test/v1", api_key="k", model="m",
                                    transport=_server(responses, seen), **kw)


def test_tool_call_round_trip_and_request_shape():
    seen = []
    p = _provider([(200, {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "get_job_log_windows", "arguments": "{\"job_id\": 5}"}}]}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 9}})], seen)
    messages = [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c0", "name": "map_job_to_family", "arguments": {"w": 1}}]},
        {"role": "tool", "tool_call_id": "c0", "content": "<untrusted_data>x</untrusted_data>"},
    ]
    r = p.complete(messages, TOOLS)
    assert r.tool_calls[0].name == "get_job_log_windows" and r.tool_calls[0].arguments == {
        "job_id": 5}
    assert (r.input_tokens, r.output_tokens) == (120, 9)
    req = seen[0]
    assert req.url == "http://llm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body["model"] == "m" and body["temperature"] == 0.0
    assert body["tools"][0]["function"]["name"] == "get_job_log_windows"
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"w": 1}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c0",
                                   "content": "<untrusted_data>x</untrusted_data>"}


def test_final_answer_and_retry_on_429():
    seen = []
    p = _provider([(429, {"error": "slow down"}),
                   (200, {"choices": [{"message": {"content": "{\"ok\": 1}"}}]})], seen)
    r = p.complete([{"role": "user", "content": "hi"}], [])
    assert r.content == '{"ok": 1}' and len(seen) == 2
    assert "tools" not in json.loads(seen[-1].content)


def test_http_error_raises_and_bad_arguments_are_kept_not_crashed():
    with pytest.raises(LLMError):
        _provider([(400, {"error": "bad"})], []).complete([], [])
    p = _provider([(200, {"choices": [{"message": {"tool_calls": [
        {"id": "c", "function": {"name": "t", "arguments": "{not json"}}]}}]})], [])
    assert p.complete([], []).tool_calls[0].arguments == {"_unparsed_arguments": "{not json"}


def test_no_key_means_no_auth_header_and_config_is_required(tmp_path, monkeypatch):
    seen = []
    p = OpenAICompatibleProvider(base_url="http://localhost:11434/v1", api_key="", model="qwen",
                                 transport=_server([(200, {"choices": [{"message": {
                                     "content": "x"}}]})], seen))
    p.complete([], [])
    assert "authorization" not in seen[0].headers
    for var in ("LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("rocksentinel.llm.openai_compat._DEFAULT_ENV_FILE", tmp_path / "none")
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        OpenAICompatibleProvider()


def test_recording_can_be_replayed(tmp_path):
    seen = []
    live = _provider([(200, {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "get_job_log_windows", "arguments": "{}"}}]}}]}),
        (200, {"choices": [{"message": {"content": "{\"done\": true}"}}]})], seen)
    rec = RecordingProvider(live)
    rec.complete([], TOOLS)
    rec.complete([], TOOLS)
    path = tmp_path / "rec.json"
    rec.save(path)
    replay = ReplayProvider.from_file(path)
    assert replay.model == "replay:m"
    assert replay.complete([], []).tool_calls[0].name == "get_job_log_windows"
    assert replay.complete([], []).content == '{"done": true}'
