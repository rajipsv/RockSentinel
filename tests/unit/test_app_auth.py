"""GitHub App auth: JWT, installation lookup, read-only token minting and refresh (offline)."""

import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from rocksentinel.github.app_auth import (
    READ_ONLY_PERMISSIONS,
    AppAuth,
    AppAuthError,
    load_app_auth,
)
from rocksentinel.github.client import GitHubClient, GitHubError

NOW = 1_800_000_000.0   # fake clock (seconds)


@pytest.fixture(scope="module")
def key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    return pem, k.public_key()


class FakeGitHub:
    """Mock GitHub: one installation for ROCm, none for others; counts token mints."""

    def __init__(self, ttl_s=3600):
        self.mints, self.requests, self.ttl_s, self.now = [], [], ttl_s, NOW

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/repos/ROCm/TheRock/installation":
            return httpx.Response(200, json={"id": 42})
        if path.endswith("/installation"):
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/app/installations/42/access_tokens" and request.method == "POST":
            self.mints.append(json.loads(request.content))
            n = len(self.mints)
            from datetime import UTC, datetime
            exp = datetime.fromtimestamp(self.now + self.ttl_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return httpx.Response(201, json={"token": f"ghs_test{n}", "expires_at": exp})
        if path.startswith("/repos/ROCm/TheRock/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": []})
        return httpx.Response(404)


def _auth(key, fake):
    clock = [NOW]
    auth = AppAuth("12345", key[0], api_url="https://api.github.com",
                   transport=httpx.MockTransport(fake), clock=lambda: clock[0])
    return auth, clock


def test_app_jwt_is_short_lived_and_signed_by_the_app(key):
    auth, _ = _auth(key, FakeGitHub())
    claims = jwt.decode(auth.app_jwt(), key[1], algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
    assert claims["iat"] < NOW       # backdated for clock skew


def test_installation_token_is_read_only_cached_and_refreshed(key):
    fake = FakeGitHub()
    auth, clock = _auth(key, fake)
    assert auth.installation_token("ROCm/TheRock") == "ghs_test1"
    assert auth.installation_token("rocm/other-repo") == "ghs_test1"   # same owner: cached
    assert fake.mints == [{"permissions": READ_ONLY_PERMISSIONS}]
    assert all(v == "read" for v in READ_ONLY_PERMISSIONS.values())
    mint = next(r for r in fake.requests if r.method == "POST")
    assert mint.headers["Authorization"].startswith("Bearer ey")   # app JWT, not a PAT
    clock[0] = NOW + 3600 - 200      # inside the 5-minute refresh margin
    fake.now = clock[0]
    assert auth.installation_token("ROCm/TheRock") == "ghs_test2"
    assert len(fake.mints) == 2


def test_not_installed_owner_is_a_github_error(key):
    auth, _ = _auth(key, FakeGitHub())
    with pytest.raises(AppAuthError, match="not installed") as e:
        auth.installation_token("someone/else")
    assert isinstance(e.value, GitHubError)     # poller and webhook skip, not crash
    with pytest.raises(AppAuthError):
        auth.token_for_path("/rate_limit")      # App tokens need a repo


def test_client_sends_installation_token_per_repo(key):
    fake = FakeGitHub()
    auth, _ = _auth(key, fake)
    with GitHubClient(auth.token_for_path, transport=httpx.MockTransport(fake)) as gh:
        assert gh.list_runs("ROCm/TheRock") == []
    api_call = fake.requests[-1]
    assert api_call.url.path == "/repos/ROCm/TheRock/actions/runs"
    assert api_call.headers["Authorization"] == "Bearer ghs_test1"


def test_load_app_auth_from_env_file(key, tmp_path, monkeypatch):
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=unused\n", encoding="utf-8")
    assert load_app_auth("https://api.github.com", env_file=env) is None   # PAT mode
    pem = tmp_path / "app.pem"
    pem.write_text(key[0], encoding="utf-8")
    env.write_text(f"GITHUB_APP_ID=12345\nGITHUB_APP_PRIVATE_KEY_PATH={pem}\n", encoding="utf-8")
    auth = load_app_auth("https://api.github.com", env_file=env)
    assert isinstance(auth, AppAuth)
    auth.close()
    env.write_text(f"GITHUB_APP_ID=1\nGITHUB_APP_PRIVATE_KEY_PATH={tmp_path / 'nope.pem'}\n",
                   encoding="utf-8")
    with pytest.raises(AppAuthError, match="does not point to a file"):
        load_app_auth("https://api.github.com", env_file=env)
