"""Minimal read-only GitHub REST client for Actions runs, jobs and job logs.

- Token comes from the GITHUB_TOKEN environment variable, else from the repo's `.env`.
- Read-only by construction: only GET requests exist on this class.
- ETag caching for list endpoints (304 responses don't count against the rate limit).
- Retries 5xx with backoff; raises RateLimitedError when the quota is exhausted.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.github.com"

# httpx logs full request URLs at INFO, including the signed (SAS) blob URL that GitHub
# redirects log downloads to. Keep those out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
# Owner: letters, digits, hyphens (no dots). Repo: may contain dots but not start with one,
# which rules out "." and ".." path segments.
_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,99}\.ya?ml$")
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,99}$")
_DEFAULT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimitedError(GitHubError):
    def __init__(self, reset_at: datetime | None):
        super().__init__(f"GitHub rate limit exhausted; resets at {reset_at}")
        self.reset_at = reset_at


def load_token(env_file: Path | None = None) -> str:
    """Return GITHUB_TOKEN from the environment, else from a `KEY=value` .env file."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()
    path = env_file or _DEFAULT_ENV_FILE
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = raw.strip().partition("=")
            if sep and key.strip() == "GITHUB_TOKEN" and value.strip():
                return value.strip().strip('"').strip("'")
    raise GitHubError("GITHUB_TOKEN not set (environment or .env)")


def validate_workflow(workflow: str) -> str:
    if not _WORKFLOW_FILE.match(workflow):
        raise ValueError(f"invalid workflow file name: {workflow!r}")
    return workflow


def validate_repo(repo: str) -> str:
    if not _REPO_RE.match(repo):
        raise ValueError(f"invalid repo {repo!r}; expected 'owner/name'")
    return repo


@dataclass(frozen=True)
class RunRef:
    run_id: int
    run_number: int
    attempt: int
    workflow: str  # workflow file name, e.g. "test_artifacts.yml"
    title: str
    head_sha: str
    branch: str | None
    event: str
    conclusion: str | None
    created_at: str
    html_url: str


@dataclass(frozen=True)
class JobRef:
    job_id: int
    run_id: int
    attempt: int
    name: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    runner_label: str | None
    html_url: str


def _run_from_api(r: dict[str, Any]) -> RunRef:
    return RunRef(
        run_id=r["id"],
        run_number=r["run_number"],
        attempt=r.get("run_attempt") or 1,
        workflow=Path(r.get("path") or "").name or (r.get("name") or ""),
        title=r.get("display_title") or r.get("name") or "",
        head_sha=r["head_sha"],
        branch=r.get("head_branch"),
        event=r.get("event") or "",
        conclusion=r.get("conclusion"),
        created_at=r["created_at"],
        html_url=r["html_url"],
    )


def _job_from_api(j: dict[str, Any]) -> JobRef:
    labels = j.get("labels") or []
    return JobRef(
        job_id=j["id"],
        run_id=j["run_id"],
        attempt=j.get("run_attempt") or 1,
        name=j["name"],
        conclusion=j.get("conclusion"),
        started_at=j.get("started_at"),
        completed_at=j.get("completed_at"),
        runner_label=labels[0] if labels else None,
        html_url=j["html_url"],
    )


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        api_url: str = API_URL,
    ):
        # api_url: GitHub Enterprise Server uses https://<host>/api/v3
        self._http = httpx.Client(
            base_url=api_url,
            headers={
                "Authorization": f"Bearer {token or load_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "rocksentinel",
            },
            follow_redirects=True,
            timeout=60,
            transport=transport,
        )
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._etags: dict[str, tuple[str, Any]] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transport ------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                # Dropped connections / incomplete bodies / timeouts: retry like a 5xx, then
                # surface as GitHubError so callers can skip one run instead of crashing.
                if attempt < self._max_retries:
                    time.sleep(self._backoff_s * (2**attempt))
                    continue
                raise GitHubError(f"GET {path} failed: {type(exc).__name__}") from exc
            if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
                reset = resp.headers.get("x-ratelimit-reset")
                raise RateLimitedError(
                    datetime.fromtimestamp(int(reset), UTC) if reset else None
                )
            if resp.status_code >= 500 and attempt < self._max_retries:
                time.sleep(self._backoff_s * (2**attempt))
                continue
            return resp
        return resp  # pragma: no cover

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = f"{path}?{sorted((params or {}).items())}"
        cached = self._etags.get(key)
        if cached:
            self._http.headers["If-None-Match"] = cached[0]
        try:
            resp = self._get(path, params)
        finally:
            self._http.headers.pop("If-None-Match", None)
        if resp.status_code == 304 and cached:
            return cached[1]
        if resp.status_code != 200:
            raise GitHubError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}",
                              status=resp.status_code)
        data = resp.json()
        if etag := resp.headers.get("etag"):
            self._etags[key] = (etag, data)
        return data

    # --- API --------------------------------------------------------------------------------

    def list_runs(
        self,
        repo: str,
        *,
        since: datetime | None = None,
        since_hours: int = 24,
        status: str = "completed",
        workflow: str | None = None,
        branch: str | None = None,
        limit: int = 20,
    ) -> list[RunRef]:
        """Workflow runs created since `since` (or the last `since_hours`), newest first.

        `status` is any GitHub run status/conclusion filter: "completed", "failure", ...
        """
        validate_repo(repo)
        if workflow is not None:
            validate_workflow(workflow)
        start = since or (datetime.now(UTC) - timedelta(hours=since_hours))
        per_page = min(max(limit, 1), 100)
        params: dict[str, Any] = {
            "status": status,
            "created": f">={start.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "per_page": per_page,
        }
        if branch:
            params["branch"] = branch
        path = (
            f"/repos/{repo}/actions/workflows/{workflow}/runs"
            if workflow
            else f"/repos/{repo}/actions/runs"
        )
        runs: list[RunRef] = []
        page = 1
        while len(runs) < limit:
            data = self._get_json(path, {**params, "page": page})
            batch = [_run_from_api(r) for r in data.get("workflow_runs", [])]
            runs.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return runs[:limit]

    def list_failed_runs(
        self,
        repo: str,
        *,
        since_hours: int = 24,
        workflow: str | None = None,
        branch: str | None = None,
        limit: int = 20,
    ) -> list[RunRef]:
        return self.list_runs(
            repo,
            since_hours=since_hours,
            status="failure",
            workflow=workflow,
            branch=branch,
            limit=limit,
        )

    def list_jobs(
        self, repo: str, run_id: int, *, failed_only: bool = True, all_attempts: bool = False
    ) -> list[JobRef]:
        """Jobs of a run. A rerun gets new job ids; `all_attempts` keeps the earlier attempts
        (needed for rerun/flake evidence), otherwise only the latest attempt is returned."""
        validate_repo(repo)
        jobs: list[JobRef] = []
        # Very large runs (1000+ jobs) can make GitHub time out (502/504) on 100-job pages;
        # then fall back to smaller pages for this run.
        for per_page in (100, 30):
            jobs, page = [], 1
            try:
                while True:
                    data = self._get_json(
                        f"/repos/{repo}/actions/runs/{run_id}/jobs",
                        {"filter": "all" if all_attempts else "latest", "per_page": per_page,
                         "page": page},
                    )
                    batch = [_job_from_api(j) for j in data.get("jobs", [])]
                    jobs.extend(batch)
                    if len(batch) < per_page:
                        break
                    page += 1
                break
            except GitHubError as exc:
                if per_page == 30 or not (exc.status and exc.status >= 500):
                    raise
        if failed_only:
            jobs = [j for j in jobs if j.conclusion in ("failure", "timed_out")]
        return jobs

    def get_job_log(self, repo: str, job_id: int) -> str:
        """Download a job's full plain-text log (follows GitHub's redirect to blob storage)."""
        validate_repo(repo)
        resp = self._get(f"/repos/{repo}/actions/jobs/{job_id}/logs")
        if resp.status_code != 200:
            raise GitHubError(f"log for job {job_id} -> {resp.status_code}",
                              status=resp.status_code)
        return resp.text

    # --- history tracing (used by find_introducing_change) --------------------------------

    def search_commits(self, repo: str, terms: list[str], limit: int = 10) -> list[dict]:
        """GitHub commit search in one repo: [{sha, date, message, url}], newest first."""
        validate_repo(repo)
        query = " ".join([f"repo:{repo}", *terms])
        data = self._get_json("/search/commits", {"q": query, "sort": "committer-date",
                                                   "order": "desc", "per_page": min(limit, 30)})
        return [{"sha": i["sha"], "date": i["commit"]["committer"]["date"],
                 "message": i["commit"]["message"].splitlines()[0], "url": i["html_url"]}
                for i in data.get("items", [])][:limit]

    def list_commits(self, repo: str, *, path: str | None = None, since: str | None = None,
                     limit: int = 100) -> list[dict]:
        """Commits of a repo (optionally touching `path`), newest first."""
        validate_repo(repo)
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if path:
            params["path"] = path
        if since:
            params["since"] = since
        data = self._get_json(f"/repos/{repo}/commits", params)
        return [{"sha": c["sha"], "date": c["commit"]["committer"]["date"],
                 "message": c["commit"]["message"].splitlines()[0], "url": c["html_url"]}
                for c in data][:limit]

    def compare_status(self, repo: str, base: str, head: str) -> str:
        """'ahead' | 'behind' | 'identical' | 'diverged' for head relative to base."""
        validate_repo(repo)
        sha = re.compile(r"[0-9a-fA-F]{7,40}")
        if not (sha.fullmatch(base) and sha.fullmatch(head)):
            raise ValueError("compare_status expects commit SHAs")
        data = self._get_json(f"/repos/{repo}/compare/{base}...{head}", {"per_page": 1})
        return data["status"]
