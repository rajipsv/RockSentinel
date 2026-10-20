"""Shared test helper: an httpx transport that serves recorded GitHub API responses."""

from pathlib import Path

import httpx

GH = Path(__file__).resolve().parents[1] / "fixtures" / "github"
RUN_ID = 39249671429
JOB_ID = 118078343556


def recorded_transport(calls: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == "/repos/ROCm/TheRock/actions/runs":
            body = (GH / "runs_failure.json").read_text(encoding="utf-8")
            return httpx.Response(200, text=body, headers={"etag": '"runs-v1"'})
        if path == f"/repos/ROCm/TheRock/actions/runs/{RUN_ID}/jobs":
            return httpx.Response(200, text=(GH / f"jobs_run_{RUN_ID}.json").read_text("utf-8"))
        if path == f"/repos/ROCm/TheRock/actions/jobs/{JOB_ID}/logs":
            log = (GH / f"job_{JOB_ID}_log_excerpt.txt").read_text("utf-8")
            return httpx.Response(200, text=log)
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(handler)
