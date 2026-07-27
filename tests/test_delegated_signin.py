"""
The MCP service owns the Google OAuth flow but delegates the SABAH.OS session to
Django: after verifying the Google identity, the callback POSTs the claims to
Django's /auth/google/internal/complete/ and forwards whatever redirect params
Django returns. These cover that hand-off and the `next` threading.
"""

from unittest.mock import AsyncMock

import pytest

from app.api import auth as auth_module
from app.core.security import create_oauth_state, verify_oauth_state


# ── _complete_via_sabah — the server-to-server call to Django ─────────────────


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    """Stand-in for httpx.AsyncClient as an async context manager."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, headers, json))
        if self._exc:
            raise self._exc
        return self._resp


@pytest.mark.asyncio
async def test_complete_via_sabah_posts_claims_with_internal_key(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "INTERNAL_API_KEY", "shared-key")
    monkeypatch.setattr(auth_module.settings, "SABAH_API_BASE_URL", "https://api.example.com/")
    fake = _FakeClient(resp=_FakeResp(200, {"status": "success", "code": "abc", "next": "/x"}))
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", lambda *a, **k: fake)

    result = await auth_module._complete_via_sabah(
        {"email": "e@x.com", "sub": "s"}, {"access_token": "t"}, next_path="/x", link_user_id=None
    )

    assert result == {"status": "success", "code": "abc", "next": "/x"}
    url, headers, body = fake.calls[0]
    assert url == "https://api.example.com/api/v1/auth/google/internal/complete/"
    assert headers["X-Internal-Key"] == "shared-key"
    assert body["claims"]["email"] == "e@x.com"
    assert body["next"] == "/x"


@pytest.mark.asyncio
async def test_complete_via_sabah_reports_not_configured_without_key(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "INTERNAL_API_KEY", "")
    result = await auth_module._complete_via_sabah({}, {}, next_path="/", link_user_id=None)
    assert result["status"] == "error"
    assert result["code"] == "sabah_not_configured"


@pytest.mark.asyncio
async def test_complete_via_sabah_handles_unreachable_django(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "INTERNAL_API_KEY", "k")
    fake = _FakeClient(exc=auth_module.httpx.HTTPError("connection refused"))
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", lambda *a, **k: fake)
    result = await auth_module._complete_via_sabah({}, {}, next_path="/", link_user_id=None)
    assert result["code"] == "sabah_unreachable"


@pytest.mark.asyncio
async def test_complete_via_sabah_handles_non_200(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "INTERNAL_API_KEY", "k")
    fake = _FakeClient(resp=_FakeResp(500, None, text="boom"))
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", lambda *a, **k: fake)
    result = await auth_module._complete_via_sabah({}, {}, next_path="/", link_user_id=None)
    assert result["code"] == "sabah_error"


# ── consent URL threads the `next` path through the signed state ──────────────


def test_build_authorization_url_threads_next_into_state(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(auth_module.settings, "GOOGLE_CLIENT_SECRET", "sec")
    data = auth_module.build_authorization_url(next_path="/tasks")
    assert verify_oauth_state(data["state"])["next"] == "/tasks"


# ── callback delegates to Django and forwards its redirect params ────────────


class _FakeCreds:
    token = "ya29.access"
    refresh_token = "1//refresh"
    id_token = "id.tok.en"
    expiry = None
    scopes = ["openid", "email"]


class _FakeFlow:
    credentials = _FakeCreds()

    def fetch_token(self, code=None):
        return None


@pytest.mark.asyncio
async def test_callback_forwards_django_redirect_params(client, monkeypatch):
    monkeypatch.setattr(auth_module.settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(auth_module.settings, "GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setattr(auth_module.settings, "FRONTEND_REDIRECT_URL", "https://sabahos.com/auth/google/callback")
    monkeypatch.setattr(auth_module, "_build_flow", lambda state=None: _FakeFlow())
    monkeypatch.setattr(
        auth_module.id_token,
        "verify_oauth2_token",
        lambda *a, **k: {"sub": "s", "email": "e@x.com", "email_verified": True, "name": "E"},
    )
    delegate = AsyncMock(return_value={"status": "success", "code": "login-code-1", "next": "/tasks"})
    monkeypatch.setattr(auth_module, "_complete_via_sabah", delegate)

    state = create_oauth_state(
        {"redirect_to": "https://sabahos.com/auth/google/callback", "next": "/tasks", "sabah_user_id": None}
    )
    resp = await client.get(f"/auth/google/callback?code=abc&state={state}")

    assert resp.status_code == 307  # RedirectResponse, not followed
    location = resp.headers["location"]
    assert location.startswith("https://sabahos.com/auth/google/callback")
    assert "status=success" in location
    assert "code=login-code-1" in location
    # The MCP service must not mint or leak its own token any more.
    assert "token=" not in location
    # And it actually delegated with the verified claims.
    assert delegate.await_count == 1
    assert delegate.await_args.args[0]["email"] == "e@x.com"


@pytest.mark.asyncio
async def test_callback_forwards_error_from_django(client, monkeypatch):
    monkeypatch.setattr(auth_module.settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(auth_module.settings, "GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setattr(auth_module.settings, "FRONTEND_REDIRECT_URL", "https://sabahos.com/auth/google/callback")
    monkeypatch.setattr(auth_module, "_build_flow", lambda state=None: _FakeFlow())
    monkeypatch.setattr(
        auth_module.id_token,
        "verify_oauth2_token",
        lambda *a, **k: {"sub": "s", "email": "stranger@x.com", "email_verified": True},
    )
    monkeypatch.setattr(
        auth_module,
        "_complete_via_sabah",
        AsyncMock(return_value={"status": "error", "code": "not_registered", "next": "/"}),
    )

    state = create_oauth_state(
        {"redirect_to": "https://sabahos.com/auth/google/callback", "next": "/", "sabah_user_id": None}
    )
    resp = await client.get(f"/auth/google/callback?code=abc&state={state}")
    assert resp.status_code == 307
    assert "code=not_registered" in resp.headers["location"]
