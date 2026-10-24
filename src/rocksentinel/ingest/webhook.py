"""GitHub webhook receiver: `workflow_run` completed -> store the run's jobs, queue failures.

Webhooks make ingestion near real-time for repos where the GitHub App is installed; the
poller stays as the catch-up path (missed deliveries, repos without the App). Both write
through the same idempotent upserts, so a run seen twice is harmless.

Framework-free: `handle_delivery(headers, body, ...)` returns (HTTP status, JSON body). The
local server below and (later) an Azure Function call it. It only reads from GitHub.

Checks, in order:
1. body size limit, then the HMAC-SHA256 signature (X-Hub-Signature-256) with the webhook
   secret, compared in constant time. Unsigned or wrongly signed deliveries get 401;
2. a delivery id seen before is acknowledged and skipped (GitHub retries deliveries);
3. only `workflow_run` with action `completed`, for an enabled repo in config/repos.toml,
   matching that repo's branch and workflow filters, is processed. Everything else gets
   202 "ignored", so GitHub doesn't keep retrying.

    python -m rocksentinel.ingest.webhook --port 8790    # local; secret from GITHUB_WEBHOOK_SECRET
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json

from rocksentinel.github.client import GitHubError, _run_from_api
from rocksentinel.ingest.poller import to_job_result

MAX_BODY = 5 * 1024 * 1024        # GitHub caps payloads at 25 MB; workflow_run is ~20 KB
_DELIVERY_KEY = "webhook:delivery:{}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _matches(cfg, run) -> bool:
    branches = [b for b in cfg.branch_filters() if b]
    workflows = [w for w in cfg.workflow_filters() if w]
    return ((not branches or run.branch in branches)
            and (not workflows or run.workflow in workflows))


def handle_event(event: str, payload: dict, store, gh, config) -> tuple[int, dict]:
    """Process one verified event. Returns (status, body)."""
    from rocksentinel.config import profile_for

    if event == "ping":
        return 200, {"status": "pong"}
    if event != "workflow_run" or payload.get("action") != "completed":
        return 202, {"status": "ignored", "reason": f"event {event}/{payload.get('action')}"}
    repo = (payload.get("repository") or {}).get("full_name", "")
    cfg = config.repo(repo)
    if cfg is None or not cfg.enabled:
        return 202, {"status": "ignored", "reason": "repository not configured"}
    try:
        run = _run_from_api(payload["workflow_run"])
    except (KeyError, TypeError):
        return 400, {"error": "malformed workflow_run payload"}
    if not _matches(cfg, run):
        return 202, {"status": "ignored", "reason": "branch or workflow filtered out"}
    try:
        jobs = gh.list_jobs(repo, run.run_id, failed_only=False, all_attempts=True)
    except GitHubError as exc:
        # 503 asks GitHub to redeliver later; the poller also catches the run up
        return 503, {"error": f"could not read jobs: {type(exc).__name__}"}
    profile = profile_for(repo, config)
    results = [to_job_result(repo, run, j, profile) for j in jobs]
    written = store.upsert_jobs(results)
    queued = sum(store.enqueue(r) for r in results if r.failed)
    return 200, {"status": "stored", "run_id": run.run_id, "jobs": written, "queued": queued}


def handle_delivery(headers: dict[str, str], body: bytes, *, secret: str, store, gh,
                    config) -> tuple[int, dict]:
    """Full receiver: size, signature, de-duplication, then `handle_event`."""
    h = {k.lower(): v for k, v in headers.items()}
    if len(body) > MAX_BODY:
        return 413, {"error": "payload too large"}
    if not verify_signature(secret, body, h.get("x-hub-signature-256")):
        return 401, {"error": "bad signature"}
    delivery = h.get("x-github-delivery", "")
    if delivery and store.get_state(_DELIVERY_KEY.format(delivery)):
        return 200, {"status": "duplicate"}
    try:
        payload = json.loads(body)
    except ValueError:
        return 400, {"error": "body is not JSON"}
    status, result = handle_event(h.get("x-github-event", ""), payload, store, gh, config)
    if delivery and status < 500:   # remember it only once handled, so retries still work
        store.set_state(_DELIVERY_KEY.format(delivery), "1")
    return status, result


def main() -> None:  # pragma: no cover - thin local wrapper
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from rocksentinel.config import load_config
    from rocksentinel.github.client import GitHubClient, read_env
    from rocksentinel.store.sqlite_store import SQLiteStore

    parser = argparse.ArgumentParser(description="Local GitHub webhook receiver.")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--db", default=".data/rocksentinel.db")
    args = parser.parse_args()
    secret = read_env("GITHUB_WEBHOOK_SECRET")
    if not secret:
        parser.error("GITHUB_WEBHOOK_SECRET is not set (environment or .env)")
    config = load_config()
    store = SQLiteStore(args.db)
    gh = GitHubClient(api_url=config.settings.github_api_url)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                status, result = 413, {"error": "payload too large"}
            else:
                status, result = handle_delivery(dict(self.headers), self.rfile.read(size),
                                                 secret=secret, store=store, gh=gh,
                                                 config=config)
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *a):  # keep request lines short; no headers or bodies
            print(f"webhook {self.command} -> {a[1] if len(a) > 1 else ''}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Webhook receiver on http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        gh.close()
        store.close()


if __name__ == "__main__":
    main()
