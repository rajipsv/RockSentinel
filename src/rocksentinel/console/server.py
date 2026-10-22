"""Local dev server for the Console: serves web/ and the JSON API (stdlib only).

    python -m rocksentinel.console.server            # http://127.0.0.1:8765

Local only: binds to 127.0.0.1, accepts writes only from the same origin, and its
writes touch only the golden-set files. In Azure, web/ is served by Static Web Apps
and the api.py functions run as managed Functions behind its sign-in.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rocksentinel.console import api
from rocksentinel.evals.labeler import ROOT, Paths

WEB_DIR = ROOT / "web"
MAX_BODY = 16_384
_CASE = r"(?P<case>[A-Za-z0-9\-]+)"


def _q(query: dict, key: str) -> str | None:
    """First value of a query parameter, or None."""
    return (query.get(key) or [None])[0]


ROUTES = [
    ("GET", re.compile(r"^/api/proposals$"), lambda h, m, q: api.list_proposals(h.paths)),
    ("GET", re.compile(r"^/api/golden$"), lambda h, m, q: api.list_golden(h.paths)),
    ("GET", re.compile(r"^/api/evals$"),
     lambda h, m, q: api.evals(h.paths, confirmed_only=q.get("confirmed") == ["1"],
                               split=(q.get("split") or ["all"])[0])),
    ("GET", re.compile(rf"^/api/cases/{_CASE}/evidence$"),
     lambda h, m, q: api.evidence(m["case"], h.paths)),
    ("POST", re.compile(rf"^/api/proposals/{_CASE}/accept$"),
     lambda h, m, q: api.accept(m["case"], h.json_body(), h.paths)),
    ("POST", re.compile(rf"^/api/proposals/{_CASE}/reject$"),
     lambda h, m, q: api.reject(m["case"], h.paths)),
    ("GET", re.compile(r"^/api/workflows$"),
     lambda h, m, q: api.list_workflows(h.jobs(), since=_q(q, "since"), until=_q(q, "until"),
                                        repo=_q(q, "repo"))),
    ("GET", re.compile(r"^/api/runs$"),
     lambda h, m, q: api.list_runs(h.jobs(), workflow=_q(q, "workflow"), repo=_q(q, "repo"),
                                   since=_q(q, "since"), until=_q(q, "until"))),
    ("GET", re.compile(r"^/api/runs/(?P<run>\d+)$"),
     lambda h, m, q: api.run_detail(h.jobs(), int(m["run"]))),
    ("GET", re.compile(r"^/api/jobs/(?P<job>\d+)$"),
     lambda h, m, q: api.case_detail(h.jobs(), h.memory(), int(m["job"]),
                                     fetch_evidence=q.get("evidence") != ["0"])),
    ("GET", re.compile(r"^/api/signatures$"), lambda h, m, q: api.list_signatures(h.memory())),
]


class ConsoleHandler(BaseHTTPRequestHandler):
    paths: Paths = Paths()
    web_dir: Path = WEB_DIR
    stores: dict = {}   # lazily opened job store / memory store (shared, thread-safe)

    def jobs(self):
        if "jobs" not in self.stores:
            from rocksentinel.store.sqlite_store import SQLiteStore
            self.stores["jobs"] = SQLiteStore(self.paths.root / ".data" / "rocksentinel.db")
        return self.stores["jobs"]

    def memory(self):
        if "memory" not in self.stores:
            from rocksentinel.memory.store import MemoryStore
            self.stores["memory"] = MemoryStore(self.paths.root / ".data" / "rocksentinel.db")
        return self.stores["memory"]

    def log_message(self, fmt: str, *args: object) -> None:  # keep test output quiet
        pass

    # --- helpers --------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise api.ApiError(413, "body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise api.ApiError(400, "invalid JSON") from exc
        if not isinstance(data, dict):
            raise api.ApiError(400, "JSON object expected")
        return data

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host")

    # --- dispatch -------------------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        if method == "POST" and not self._same_origin():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request"})
        for verb, pattern, handler in ROUTES:
            m = pattern.match(url.path)
            if m and verb == method:
                try:
                    return self._json(HTTPStatus.OK, handler(self, m, parse_qs(url.query)))
                except api.ApiError as exc:
                    return self._json(exc.status, {"error": str(exc)})
        if method == "GET" and not url.path.startswith("/api/"):
            return self._static(url.path)
        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (self.web_dir / rel).resolve()
        if self.web_dir.resolve() not in target.parents or not target.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def make_server(port: int = 8765, paths: Paths | None = None,
                web_dir: Path | None = None, stores: dict | None = None) -> ThreadingHTTPServer:
    handler = type("BoundConsoleHandler", (ConsoleHandler,), {
        "paths": paths or Paths(), "web_dir": web_dir or WEB_DIR, "stores": stores or {},
    })
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the RockSentinel Console locally.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.port)
    print(f"RockSentinel Console on http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
