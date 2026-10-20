"""Record small, trimmed real GitHub API responses for offline tests.

Usage (needs GITHUB_TOKEN in the environment or .env):
    python scripts/record_github_fixtures.py

Writes to tests/fixtures/github/. Responses are trimmed to the fields the
client uses so fixtures stay small and contain no personal data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from rocksentinel.github.client import load_token

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "github"
API = "https://api.github.com"

RUN_FIELDS = (
    "id", "run_number", "run_attempt", "name", "display_title", "path", "head_branch",
    "head_sha", "event", "status", "conclusion", "created_at", "updated_at", "html_url",
)
JOB_FIELDS = (
    "id", "run_id", "run_attempt", "name", "status", "conclusion", "started_at",
    "completed_at", "labels", "runner_name", "html_url",
)


def _trim(obj: dict, fields: tuple[str, ...]) -> dict:
    return {k: obj.get(k) for k in fields}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="ROCm/TheRock")
    parser.add_argument("--run-id", type=int, default=39249671429)
    parser.add_argument("--job-id", type=int, default=118078343556)
    parser.add_argument("--max-jobs", type=int, default=6)
    parser.add_argument("--log-context", type=int, default=60)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    headers = {
        "Authorization": f"Bearer {load_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(base_url=API, headers=headers, follow_redirects=True, timeout=60) as gh:
        run = gh.get(f"/repos/{args.repo}/actions/runs/{args.run_id}").json()
        runs_page = {"total_count": 1, "workflow_runs": [_trim(run, RUN_FIELDS)]}
        (OUT / "runs_failure.json").write_text(json.dumps(runs_page, indent=2), encoding="utf-8")

        jobs = gh.get(
            f"/repos/{args.repo}/actions/runs/{args.run_id}/jobs",
            params={"filter": "latest", "per_page": 100},
        ).json()
        failed = [j for j in jobs["jobs"] if j.get("conclusion") == "failure"][: args.max_jobs]
        passed = [j for j in jobs["jobs"] if j.get("conclusion") == "success"][:1]
        trimmed = [_trim(j, JOB_FIELDS) for j in failed + passed]
        jobs_page = {"total_count": len(trimmed), "jobs": trimmed}
        (OUT / f"jobs_run_{args.run_id}.json").write_text(
            json.dumps(jobs_page, indent=2), encoding="utf-8"
        )

        log = gh.get(f"/repos/{args.repo}/actions/jobs/{args.job_id}/logs").text
        lines = log.splitlines()
        hits = [i for i, line in enumerate(lines) if "Memory access fault by GPU" in line]
        tail_hits = [
            i for i, line in enumerate(lines) if "Process completed with exit code" in line
        ]
        anchor = hits[0] if hits else len(lines) - 1
        start = max(0, anchor - args.log_context)
        end = min(len(lines), anchor + args.log_context)
        excerpt = lines[start:end]
        if tail_hits and tail_hits[-1] >= end:
            excerpt += ["..."] + lines[tail_hits[-1] - 2 : tail_hits[-1] + 1]
        (OUT / f"job_{args.job_id}_log_excerpt.txt").write_text(
            "\n".join(excerpt) + "\n", encoding="utf-8"
        )

    print(f"Recorded fixtures in {OUT}")


if __name__ == "__main__":
    main()
