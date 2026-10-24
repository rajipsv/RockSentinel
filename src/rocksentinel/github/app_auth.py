"""GitHub App authentication: app JWT -> per-installation, read-only access tokens.

How a GitHub App signs in (no personal token involved):
1. Sign a short-lived JWT (10 minutes max) with the app's private key (RS256).
2. With that JWT, find the app's installation for a repo's owner
   (GET /repos/{owner}/{repo}/installation), cached per owner.
3. Exchange the JWT for an installation access token (POST
   /app/installations/{id}/access_tokens). The request narrows the token to READ-only
   permissions, so even if the App were granted more, these tokens can't write. The
   token lasts about an hour; it is cached and refreshed 5 minutes before expiry.

Step 3 is a POST, but it only mints a token for the App itself; nothing in any repository
changes. All repository access stays GET-only (see client.py).

Settings (environment, else the git-ignored .env): GITHUB_APP_ID and
GITHUB_APP_PRIVATE_KEY_PATH (a .pem file outside the repo; in Azure the key lives in
Key Vault). Tokens and the key are never logged.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from rocksentinel.github.client import GitHubError

# Read-only by request: the minted token can never exceed these, whatever the App holds.
READ_ONLY_PERMISSIONS = {"actions": "read", "contents": "read", "metadata": "read"}
REFRESH_MARGIN_S = 300
JWT_LIFETIME_S = 540          # under GitHub's 10-minute cap
CLOCK_SKEW_S = 60             # backdate iat, as GitHub recommends
_REPO_IN_PATH = re.compile(r"^/repos/([^/]+)/([^/]+)(?:/|$)")


class AppAuthError(GitHubError):
    """Raised as a GitHubError so callers (poller, webhook) skip one repo, not crash."""


class AppAuth:
    """Mints and caches installation tokens. Thread-safe; one instance per GitHub App."""

    def __init__(self, app_id: str, private_key_pem: str, *, api_url: str,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time):
        if not app_id or not private_key_pem:
            raise AppAuthError("GitHub App id and private key are required")
        self._app_id = str(app_id)
        self._key = private_key_pem
        self._clock = clock
        self._http = httpx.Client(base_url=api_url, transport=transport, timeout=30, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rocksentinel"})
        self._installations: dict[str, int] = {}             # owner -> installation id
        self._tokens: dict[int, tuple[str, float]] = {}      # installation -> (token, expiry)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # --- step 1: app JWT ------------------------------------------------------------------
    def app_jwt(self) -> str:
        now = int(self._clock())
        claims = {"iat": now - CLOCK_SKEW_S, "exp": now + JWT_LIFETIME_S, "iss": self._app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def _app_request(self, method: str, path: str, **kw) -> dict:
        resp = self._http.request(method, path, headers={
            "Authorization": f"Bearer {self.app_jwt()}"}, **kw)
        if resp.status_code == 404:
            raise AppAuthError(f"GitHub App is not installed for {path.split('/')[2]}")
        if resp.status_code >= 400:
            # status only: GitHub error bodies are safe, but keep logs minimal and token-free
            raise AppAuthError(f"GitHub App auth failed: {method} {path} -> {resp.status_code}")
        return resp.json()

    # --- step 2: which installation covers this repo -------------------------------------
    def installation_id(self, repo: str) -> int:
        owner = repo.split("/")[0].lower()
        with self._lock:
            if owner in self._installations:
                return self._installations[owner]
        inst = int(self._app_request("GET", f"/repos/{repo}/installation")["id"])
        with self._lock:
            self._installations[owner] = inst
        return inst

    # --- step 3: read-only installation token ---------------------------------------------
    def installation_token(self, repo: str) -> str:
        inst = self.installation_id(repo)
        with self._lock:
            cached = self._tokens.get(inst)
            if cached and cached[1] - REFRESH_MARGIN_S > self._clock():
                return cached[0]
        data = self._app_request("POST", f"/app/installations/{inst}/access_tokens",
                                 json={"permissions": READ_ONLY_PERMISSIONS})
        expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        with self._lock:
            self._tokens[inst] = (data["token"], expiry)
        return data["token"]

    def token_for_path(self, path: str) -> str:
        """Token for a REST path like /repos/{owner}/{repo}/actions/... (used by the client)."""
        m = _REPO_IN_PATH.match(path)
        if not m:
            raise AppAuthError(f"no repository in path {path!r}; App tokens are per installation")
        return self.installation_token(f"{m.group(1)}/{m.group(2)}")


def load_app_auth(api_url: str, env_file: Path | None = None,
                  transport: httpx.BaseTransport | None = None) -> AppAuth | None:
    """AppAuth from GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH, or None if not configured."""
    from rocksentinel.github.client import read_env

    app_id = read_env("GITHUB_APP_ID", env_file)
    key_path = read_env("GITHUB_APP_PRIVATE_KEY_PATH", env_file)
    if not app_id or not key_path:
        return None
    path = Path(key_path).expanduser()
    if not path.is_file():
        raise AppAuthError("GITHUB_APP_PRIVATE_KEY_PATH does not point to a file")
    return AppAuth(app_id, path.read_text(encoding="utf-8"), api_url=api_url,
                   transport=transport)
