"""Console: Quality API functions and the local HTTP server (no network beyond 127.0.0.1)."""

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rocksentinel.console import api
from rocksentinel.console.server import make_server
from rocksentinel.evals.labeler import Paths
from rocksentinel.store.sqlite_store import SQLiteStore

REPO = Path(__file__).resolve().parents[2]
@pytest.fixture
def paths(tmp_path):
    (tmp_path / "evals" / "golden").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "logs").mkdir(parents=True)
    rows = [json.loads(x) for x in (REPO / "evals/golden/dataset.jsonl").read_text("utf-8")
            .splitlines() if x][:2]
    for r in rows:
        shutil.copy(REPO / r["log_fixture"], tmp_path / r["log_fixture"])
    (tmp_path / "evals/golden/dataset.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return Paths(root=tmp_path)


def test_golden_lists_cases_with_split_and_repo(paths):
    g = api.list_golden(paths)
    assert g["count"] == 2 and all(c["repo"] and c["split"] for c in g["items"])
    assert all(c["in_store"] is False for c in g["items"])  # no job store given


def test_evals_endpoint_shape(paths):
    r = api.evals(paths)
    assert r["n_cases"] == 2 and set(r["systems"]) == {"B0", "B1"}
    assert api.evals(paths, confirmed_only=True)["n_cases"] == 0
    assert api.evals(paths, split="test")["n_cases"] == sum(
        json.loads(x).get("split") == "test"
        for x in paths.dataset.read_text("utf-8").splitlines() if x)


# --- HTTP ----------------------------------------------------------------------------------

@pytest.fixture
def server(paths):
    jobs = SQLiteStore(":memory:")
    srv = make_server(port=0, paths=paths, web_dir=REPO / "web", stores={"jobs": jobs})
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    jobs.close()


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.headers.get("Content-Type"), res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_http_static_and_api(server):
    status, ctype, body = _req(server + "/")
    assert status == 200 and ctype.startswith("text/html") and b"RockSentinel" in body
    for path in ("/api/overview", "/api/repos", "/api/signatures", "/api/golden"):
        assert _req(server + path)[0] == 200, path
    assert json.loads(_req(server + "/api/overview")[2])["summary"]["failed_jobs"] == 0


def test_http_review_endpoints_are_gone_and_bad_paths_404(server):
    assert _req(server + "/api/proposals")[0] == 404
    assert _req(server + "/api/proposals/cand-1/accept", "POST", {})[0] == 404
    assert _req(server + "/api/nope")[0] == 404
    assert _req(server + "/../pyproject.toml")[0] == 404
    assert _req(server + "/api/jobs/1/triage", "POST", {})[0] == 404   # unknown job


def test_http_blocks_cross_origin_writes(server):
    status, _, _ = _req(server + "/api/jobs/1/triage", "POST", {},
                        headers={"Origin": "https://evil.example"})
    assert status == 403
