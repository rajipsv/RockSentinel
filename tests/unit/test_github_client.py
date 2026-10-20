"""GitHub client tests against recorded TheRock API responses (no network)."""

import json

import httpx
import pytest
from _recorded import GH, JOB_ID, RUN_ID, recorded_transport

from rocksentinel.github.client import (
    GitHubClient,
    GitHubError,
    RateLimitedError,
    load_token,
    validate_repo,
)


@pytest.fixture
def client():
    with GitHubClient("test-token", transport=recorded_transport(), backoff_s=0) as c:
        yield c


def test_list_failed_runs_parses_recorded_run(client):
    runs = client.list_failed_runs("ROCm/TheRock", since_hours=24)
    assert len(runs) == 1
    run = runs[0]
    assert (run.run_id, run.run_number, run.workflow) == (RUN_ID, 3351, "test_artifacts.yml")
    assert run.head_sha.startswith("f79fc42")


def test_list_failed_runs_sends_failure_filter():
    calls: list[httpx.Request] = []
    with GitHubClient("t", transport=recorded_transport(calls)) as c:
        c.list_failed_runs("ROCm/TheRock", since_hours=6, branch="main", limit=5)
    params = calls[0].url.params
    assert params["status"] == "failure"
    assert params["created"].startswith(">=")
    assert params["branch"] == "main"
    assert params["per_page"] == "5"
    assert calls[0].headers["authorization"] == "Bearer t"


def test_list_jobs_failed_only(client):
    jobs = client.list_jobs("ROCm/TheRock", RUN_ID, failed_only=True)
    assert jobs and all(j.conclusion in ("failure", "timed_out") for j in jobs)
    assert any(j.job_id == JOB_ID for j in jobs)
    assert jobs[0].runner_label == "linux-gfx1151-gpu-rocm"
    all_jobs = client.list_jobs("ROCm/TheRock", RUN_ID, failed_only=False)
    assert len(all_jobs) > len(jobs)  # the recording includes one passing job


def test_get_job_log_returns_raw_text(client):
    log = client.get_job_log("ROCm/TheRock", JOB_ID)
    assert "Memory access fault by GPU" in log
    assert log.startswith("2026-")  # raw GitHub logs carry timestamps


def test_etag_cache_serves_304():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"workflow_runs": []}, headers={"etag": '"v1"'})

    with GitHubClient("t", transport=httpx.MockTransport(handler)) as c:
        c.list_failed_runs("ROCm/TheRock")
        c.list_failed_runs("ROCm/TheRock")
    assert state["n"] == 2  # second call was a conditional request answered by 304


def test_rate_limit_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "rate limited"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1760900000"},
        )

    with GitHubClient("t", transport=httpx.MockTransport(handler)) as c, pytest.raises(
        RateLimitedError
    ):
        c.list_failed_runs("ROCm/TheRock")


def test_retries_5xx_then_succeeds():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(502)
        return httpx.Response(200, json={"workflow_runs": []})

    with GitHubClient("t", transport=httpx.MockTransport(handler), backoff_s=0) as c:
        assert c.list_failed_runs("ROCm/TheRock") == []
    assert state["n"] == 3


def test_http_error_raises(client):
    with pytest.raises(GitHubError):
        client.get_job_log("ROCm/TheRock", 1)


@pytest.mark.parametrize("bad", ["ROCm", "ROCm/TheRock/x", "../etc", "a b/c"])
def test_validate_repo_rejects_bad_names(bad):
    with pytest.raises(ValueError):
        validate_repo(bad)


def test_load_token_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nOTHER=1\nGITHUB_TOKEN="abc123"\n', encoding="utf-8")
    assert load_token(env) == "abc123"


def test_load_token_prefers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert load_token(tmp_path / "missing.env") == "from-env"


def test_load_token_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(GitHubError):
        load_token(tmp_path / "missing.env")


def test_recorded_fixtures_contain_no_token():
    for f in GH.iterdir():
        text = f.read_text(encoding="utf-8")
        assert "github_pat_" not in text and "ghp_" not in text, f.name
    json.loads((GH / "runs_failure.json").read_text(encoding="utf-8"))
