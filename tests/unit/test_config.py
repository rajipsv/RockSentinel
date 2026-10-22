"""P2: per-repo config, per-repo profiles, multi-repo polling, repo-keyed store queries."""

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from rocksentinel.config import DEFAULT_CONFIG, Config, load_config, profile_for
from rocksentinel.github.client import GitHubClient, GitHubError
from rocksentinel.ingest.poller import cursor_key, poll_repo
from rocksentinel.store.base import JobResult
from rocksentinel.store.sqlite_store import SQLiteStore

TOML = """
[settings]
github_api_url = "https://ghe.example.com/api/v3/"

[[repos]]
repo = "ROCm/TheRock"
packs = ["rocm"]

[[repos]]
repo = "acme/web"
branches = "all"
workflows = ["ci.yml", "e2e.yml"]

[[repos.job_name_rules]]
match = '^e2e \\((?P<platform>[a-z]+), (?P<component>[^)]+)\\)'
"""


@pytest.fixture
def config(tmp_path: Path) -> Config:
    path = tmp_path / "repos.toml"
    path.write_text(TOML, encoding="utf-8")
    return load_config(path)


def test_config_loads_with_defaults(config):
    assert config.settings.github_api_url == "https://ghe.example.com/api/v3"
    rock, web = config.repos
    assert (rock.tenant, rock.branch_filters(), rock.workflow_filters()) == (
        "ROCm", ["main"], [None])
    assert (web.tenant, web.branch_filters(), web.workflow_filters()) == (
        "acme", [None], ["ci.yml", "e2e.yml"])
    assert config.repo("rocm/therock") is rock  # GitHub names are case-insensitive


def test_shipped_config_is_valid():
    cfg = load_config(DEFAULT_CONFIG)
    assert {r.repo for r in cfg.enabled()} >= {"ROCm/TheRock", "ROCm/rockrel"}


def test_missing_config_file_means_no_repos(tmp_path):
    assert load_config(tmp_path / "nope.toml").repos == []


@pytest.mark.parametrize("bad", [
    {"repos": [{"repo": "../etc"}]},
    {"repos": [{"repo": "a/b", "packs": ["nope"]}]},
    {"repos": [{"repo": "a/b", "workflows": ["../x.yml"]}]},
    {"repos": [{"repo": "a/b", "job_name_rules": [{"match": "(unclosed"}]}]},
    {"repos": [{"repo": "a/b", "job_name_rules": [{"match": "(?P<secret>.*)"}]}]},
    {"repos": [{"repo": "a/b"}, {"repo": "A/B"}]},
    {"repos": [{"repo": "a/b", "token": "x"}]},          # unknown keys rejected (no secrets)
    {"settings": {"github_api_url": "http://plain.example.com"}},
])
def test_invalid_config_is_rejected(bad):
    with pytest.raises(ValidationError):
        Config.model_validate(bad)


def test_profiles_per_repo(config):
    assert profile_for("ROCm/TheRock", config).names == ("rocm", "core")
    web = profile_for("acme/web", config)
    assert web.names == ("repo:acme/web", "core")
    info = web.map_job("e2e.yml", "e2e (firefox, checkout-flow)", "ubuntu-latest")
    assert (info.component, info.platform) == ("checkout-flow", "firefox")
    # no rule matches: the packs' generic mapper still answers
    info = web.map_job("ci.yml", "build / unit (shard 1/2)", "ubuntu-latest")
    assert (info.component, info.platform, info.shard) == ("unit", "linux", "1/2")
    # repos that aren't configured get the default profile
    assert profile_for("other/repo", config).names == ("rocm", "core")


def test_cursor_keys_keep_legacy_main_key_and_separate_filters():
    assert cursor_key("a/b") == "poller:a/b:last_created_at"
    assert cursor_key("a/b", "ci.yml") == "poller:a/b:last_created_at:ci.yml"
    assert cursor_key("a/b", None, None) == "poller:a/b:last_created_at:branch=*"
    assert cursor_key("a/b", "ci.yml", "dev") == "poller:a/b:last_created_at:ci.yml:branch=dev"


def _run(run_id, workflow):
    return {"id": run_id, "run_number": 1, "run_attempt": 1, "name": workflow,
            "path": f".github/workflows/{workflow}", "head_sha": "s", "head_branch": "feature",
            "event": "pull_request", "conclusion": "failure",
            "created_at": "2026-10-22T01:00:00Z", "html_url": "u"}


def test_poll_repo_follows_config_and_uses_the_repo_profile(config):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/workflows/ci.yml/runs"):
            return httpx.Response(200, json={"workflow_runs": [_run(1, "ci.yml")]})
        if path.endswith("/workflows/e2e.yml/runs"):
            return httpx.Response(200, json={"workflow_runs": [_run(2, "e2e.yml")]})
        run_id = int(path.split("/runs/")[1].split("/")[0])
        name = "build / unit (shard 1/2)" if run_id == 1 else "e2e (firefox, checkout-flow)"
        return httpx.Response(200, json={"jobs": [{
            "id": run_id * 10, "run_id": run_id, "run_attempt": 1, "name": name,
            "conclusion": "failure", "labels": ["ubuntu-latest"], "html_url": "u"}]})

    store = SQLiteStore(":memory:")
    with GitHubClient("t", transport=httpx.MockTransport(handler)) as gh:
        results = poll_repo(gh, store, config.repo("acme/web"), config=config)
    assert [label for label, _ in results] == [
        "acme/web [ci.yml @ all branches]", "acme/web [e2e.yml @ all branches]"]
    run_calls = [c for c in calls if c.url.path.endswith("/runs")]
    assert all("branch" not in c.url.params for c in run_calls)  # branches = "all"
    assert store.get_job(10).component == "unit"
    assert (store.get_job(20).component, store.get_job(20).platform) == ("checkout-flow",
                                                                         "firefox")
    assert store.get_state("poller:acme/web:last_created_at:e2e.yml:branch=*")
    assert len(store.pending()) == 2
    store.close()


def test_huge_runs_fall_back_to_smaller_job_pages():
    sizes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sizes.append(request.url.params["per_page"])
        if request.url.params["per_page"] == "100":
            return httpx.Response(504, json={"message": "We couldn't respond in time"})
        return httpx.Response(200, json={"jobs": [{"id": 1, "run_id": 9, "run_attempt": 1,
                                                   "name": "x", "conclusion": "failure",
                                                   "labels": [], "html_url": "u"}]})

    with GitHubClient("t", transport=httpx.MockTransport(handler), max_retries=0) as gh:
        jobs = gh.list_jobs("a/b", 9, failed_only=False)
    assert [j.job_id for j in jobs] == [1] and sizes == ["100", "30"]


def test_client_errors_that_are_not_server_errors_do_not_fall_back():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with GitHubClient("t", transport=httpx.MockTransport(handler), max_retries=0) as gh, \
            pytest.raises(GitHubError) as err:
        gh.list_jobs("a/b", 9)
    assert err.value.status == 404


def test_github_enterprise_server_base_url_is_used():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(200, json={"workflow_runs": []})

    with GitHubClient("t", transport=httpx.MockTransport(handler),
                      api_url="https://ghe.example.com/api/v3") as gh:
        gh.list_runs("a/b")
    assert hosts == ["ghe.example.com/api/v3/repos/a/b/actions/runs"]


def _job(job_id, repo, workflow, conclusion="failure", run_id=None):
    return JobResult(run_id=run_id or job_id, attempt=1, job_id=job_id, repo=repo,
                     workflow=workflow, job_name="j", component=None, gpu_family=None,
                     platform=None, shard=None, runner_label=None, conclusion=conclusion,
                     started_at="2026-10-22T00:00:00Z", finished_at="2026-10-22T00:10:00Z",
                     duration_s=600, head_sha="s", branch="main", event="push")


def test_same_workflow_name_in_two_repos_stays_separate():
    store = SQLiteStore(":memory:")
    store.upsert_jobs([_job(1, "ROCm/TheRock", "test_artifacts.yml"),
                       _job(2, "ROCm/rockrel", "test_artifacts.yml"),
                       _job(3, "ROCm/rockrel", "test_artifacts.yml", "success")])
    summaries = store.workflow_summaries()
    assert sorted((w["repo"], w["runs"]) for w in summaries) == [
        ("ROCm/TheRock", 1), ("ROCm/rockrel", 2)]
    assert [r["run_id"] for r in store.recent_runs(repo="ROCm/TheRock")] == [1]
    assert [j.job_id for j in store.failed_jobs(repo="ROCm/rockrel")] == [2]
    assert [w["repo"] for w in store.workflow_summaries(repo="ROCm/rockrel")] == ["ROCm/rockrel"]
    store.close()


def test_dropped_connections_are_retried_then_reported_as_github_errors():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("peer closed connection", request=request)
        return httpx.Response(200, json={"workflow_runs": []})

    with GitHubClient("t", transport=httpx.MockTransport(handler), backoff_s=0) as gh:
        assert gh.list_runs("a/b") == [] and len(attempts) == 2

    def always_drop(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    with GitHubClient("t", transport=httpx.MockTransport(always_drop), backoff_s=0,
                      max_retries=1) as gh, pytest.raises(GitHubError, match="ReadError"):
        gh.list_runs("a/b")
