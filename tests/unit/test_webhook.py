"""Webhook receiver: signature, de-duplication, filters, storing a completed run (offline)."""

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from _recorded import RUN_ID, recorded_transport

from rocksentinel.config import load_config
from rocksentinel.github.client import GitHubClient
from rocksentinel.ingest.webhook import handle_delivery, verify_signature
from rocksentinel.store.sqlite_store import SQLiteStore

SECRET = "s3cret"
GH = Path(__file__).resolve().parents[1] / "fixtures" / "github"


def _run_payload(repo="ROCm/TheRock", action="completed", **overrides):
    runs = json.loads((GH / "runs_failure.json").read_text(encoding="utf-8"))["workflow_runs"]
    run = dict(next(r for r in runs if r["id"] == RUN_ID), **overrides)
    return {"action": action, "workflow_run": run, "repository": {"full_name": repo}}


def _send(env, payload, event="workflow_run", delivery="d-1", secret=SECRET, sign=True):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if sign:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
    return handle_delivery(headers, body, secret=SECRET, store=env["store"], gh=env["gh"],
                           config=env["config"])


@pytest.fixture
def env():
    calls = []
    store = SQLiteStore(":memory:")
    gh = GitHubClient("t", transport=recorded_transport(calls))
    yield {"store": store, "gh": gh, "config": load_config(), "calls": calls}
    gh.close()
    store.close()


def test_signature_check():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert verify_signature("k", body, good)
    assert not verify_signature("k", body + b" ", good)
    assert not verify_signature("k", body, None) and not verify_signature("", body, good)
    assert not verify_signature("k", body, good.replace("sha256=", "sha1="))


def test_completed_run_is_stored_and_failures_queued(env):
    status, body = _send(env, _run_payload())
    assert status == 200 and body["status"] == "stored" and body["run_id"] == RUN_ID
    assert body["jobs"] == 7 and body["queued"] == 6
    stored = env["store"].jobs_for_run(RUN_ID)
    assert len(stored) == 7 and {j.repo for j in stored} == {"ROCm/TheRock"}
    assert {j.workflow for j in stored} == {"test_artifacts.yml"}
    assert all(r.method == "GET" for r in env["calls"])        # read-only toward GitHub


def test_redelivery_is_acknowledged_once(env):
    assert _send(env, _run_payload())[1]["status"] == "stored"
    n = len(env["calls"])
    assert _send(env, _run_payload())[1]["status"] == "duplicate"
    assert len(env["calls"]) == n                              # no second GitHub read


def test_bad_or_missing_signature_is_rejected_before_anything_else(env):
    assert _send(env, _run_payload(), secret="wrong")[0] == 401
    assert _send(env, _run_payload(), sign=False)[0] == 401
    assert env["calls"] == [] and env["store"].count_jobs() == 0


@pytest.mark.parametrize("payload,event,reason", [
    (_run_payload(action="requested"), "workflow_run", "event"),
    (_run_payload(), "push", "event"),
    (_run_payload(repo="someone/unknown"), "workflow_run", "not configured"),
    (_run_payload(head_branch="feature-x"), "workflow_run", "filtered"),
    (_run_payload(repo="ROCm/rockrel", path=".github/workflows/lint.yml"), "workflow_run",
     "filtered"),
])
def test_other_events_are_ignored_with_202(env, payload, event, reason):
    status, body = _send(env, payload, event=event)
    assert status == 202 and body["status"] == "ignored" and reason in body["reason"]
    assert env["calls"] == []


def test_ping_malformed_and_github_outage(env):
    assert _send(env, {"zen": "hi"}, event="ping") == (200, {"status": "pong"})
    bad = _run_payload()
    del bad["workflow_run"]["head_sha"]
    assert _send(env, bad, delivery="d-2")[0] == 400
    # job list fails (unknown run -> 404): 503 so GitHub redelivers; not marked as seen
    status, _ = _send(env, _run_payload(id=1), delivery="d-3")
    assert status == 503 and env["store"].get_state("webhook:delivery:d-3") is None
