"""
The rules that make this deployment corporate-safe: only company Workspace
accounts connect, the internal API is closed without the shared key, and one
consent unlocks the whole service surface.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.google_scopes import (
    DEFAULT_SERVICES,
    SERVICES,
    describe_services,
    scopes_for,
    services_from_scopes,
)
from app.mcp.client import build_mcp_tools_config, ensure_google_servers, google_server_seed
from app.models.credential import GoogleCredential
from app.models.mcp_server import MCPServer
from app.services import google_data


# ── Scope catalogue ──────────────────────────────────────────────────────────

class TestScopeCatalogue:
    def test_default_services_exclude_admin_only_ones(self):
        assert "gmail" in DEFAULT_SERVICES
        assert "drive" in DEFAULT_SERVICES
        # A regular employee cannot grant admin scopes, so asking for them would
        # break the consent screen for everyone.
        assert "admin_directory" not in DEFAULT_SERVICES
        assert "admin_reports" not in DEFAULT_SERVICES

    def test_scopes_always_include_identity_and_are_deduplicated(self):
        scopes = scopes_for(["gmail", "gmail", "drive"])
        assert scopes[:3] == ["openid", "email", "profile"]
        assert len(scopes) == len(set(scopes))

    def test_unknown_service_is_ignored(self):
        assert scopes_for(["not-a-service"]) == ["openid", "email", "profile"]

    def test_granted_scopes_map_back_to_services(self):
        granted = [
            "openid",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar",
        ]
        services = services_from_scopes(granted)
        assert "gmail" in services
        assert "calendar" in services
        assert "drive" not in services

    def test_every_service_is_describable(self):
        described = describe_services(list(SERVICES))
        assert len(described) == len(SERVICES)
        assert all(entry["label"] and entry["scopes"] for entry in described)


# ── Corporate domain gate ────────────────────────────────────────────────────

class TestDomainGate:
    def _settings(self, **overrides) -> Settings:
        base = {
            "ENVIRONMENT": "development",
            "ALLOWED_EMAIL_DOMAINS": ["sabahhub.com", "devers.tech"],
        }
        # _env_file=None so the developer's local .env cannot change the result.
        return Settings(_env_file=None, **{**base, **overrides})

    def test_company_domains_are_allowed(self):
        settings = self._settings()
        assert settings.email_domain_allowed("karam@sabahhub.com")
        assert settings.email_domain_allowed("Karam@SABAHHUB.com")
        assert settings.email_domain_allowed("dev@devers.tech")

    def test_outside_domains_are_refused(self):
        settings = self._settings()
        assert not settings.email_domain_allowed("someone@gmail.com")
        # A lookalike suffix must not slip through a naive endswith check.
        assert not settings.email_domain_allowed("attacker@notsabahhub.com")

    def test_domains_are_normalised(self):
        settings = self._settings(ALLOWED_EMAIL_DOMAINS=["  @SabahHub.com  "])
        assert settings.ALLOWED_EMAIL_DOMAINS == ["sabahhub.com"]

    def test_production_refuses_an_open_domain_list(self):
        with pytest.raises(ValueError, match="ALLOWED_EMAIL_DOMAINS"):
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                SECRET_KEY="k" * 40,
                FERNET_KEY="",
                ALLOWED_EMAIL_DOMAINS=[],
            )

    def test_production_refuses_placeholder_secrets(self):
        with pytest.raises(ValueError) as exc:
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                SECRET_KEY="changeme",
                FERNET_KEY="",
                ALLOWED_EMAIL_DOMAINS=["sabahhub.com"],
                GOOGLE_CLIENT_ID="id",
                GOOGLE_CLIENT_SECRET="secret",
                INTERNAL_API_KEY="x" * 40,
                PUBLIC_BASE_URL="https://mcp.sabahhub.com",
            )
        assert "SECRET_KEY" in str(exc.value)
        assert "FERNET_KEY" in str(exc.value)

    def test_valid_production_config_boots(self):
        from cryptography.fernet import Fernet

        settings = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            SECRET_KEY="k" * 40,
            FERNET_KEY=Fernet.generate_key().decode(),
            INTERNAL_API_KEY="x" * 40,
            GOOGLE_CLIENT_ID="id.apps.googleusercontent.com",
            GOOGLE_CLIENT_SECRET="secret",
            ALLOWED_EMAIL_DOMAINS=["sabahhub.com"],
            PUBLIC_BASE_URL="https://mcp.sabahhub.com",
            ALLOWED_ORIGINS=["https://sabahos.com"],
        )
        assert settings.is_production


# ── Internal API auth ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestInternalAPIAuth:
    async def test_internal_routes_reject_a_missing_key(self, client):
        response = await client.get("/internal/google/credentials/1")
        assert response.status_code in (401, 503)

    async def test_internal_routes_reject_a_wrong_key(self, client, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.INTERNAL_API_KEY", "correct-key")
        response = await client.get(
            "/internal/google/credentials/1", headers={"X-Internal-Key": "wrong-key"}
        )
        assert response.status_code == 401

    async def test_internal_routes_accept_the_shared_key(self, client, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.INTERNAL_API_KEY", "correct-key")
        response = await client.get(
            "/internal/google/credentials/999", headers={"X-Internal-Key": "correct-key"}
        )
        assert response.status_code == 200
        assert response.json()["connected"] is False

    async def test_pushed_credentials_must_be_on_a_company_domain(self, client, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.INTERNAL_API_KEY", "correct-key")
        monkeypatch.setattr(
            "app.core.config.settings.ALLOWED_EMAIL_DOMAINS", ["sabahhub.com"]
        )
        response = await client.post(
            "/internal/google/credentials",
            headers={"X-Internal-Key": "correct-key"},
            json={
                "sabah_user_id": "7",
                "google_sub": "sub-outsider",
                "email": "outsider@gmail.com",
                "access_token": "ya29.token",
                "refresh_token": "1//refresh",
                "scopes": ["openid"],
            },
        )
        assert response.status_code == 403


# ── Credential storage ───────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestCredentialStorage:
    async def test_push_stores_encrypted_tokens_and_derived_services(
        self, client, db_session, monkeypatch
    ):
        monkeypatch.setattr("app.core.config.settings.INTERNAL_API_KEY", "correct-key")
        monkeypatch.setattr("app.core.config.settings.ALLOWED_EMAIL_DOMAINS", [])

        response = await client.post(
            "/internal/google/credentials",
            headers={"X-Internal-Key": "correct-key"},
            json={
                "sabah_user_id": "42",
                "google_sub": "sub-42",
                "email": "Employee@sabahhub.com",
                "access_token": "ya29.plaintext",
                "refresh_token": "1//plaintext-refresh",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "scopes": [
                    "openid",
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/drive",
                ],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["connected"] is True
        assert set(body["services"]) == {"gmail", "drive"}

        cred = (
            await db_session.execute(
                select(GoogleCredential).where(GoogleCredential.user_id == "sub-42")
            )
        ).scalars().first()
        assert cred is not None
        assert cred.google_account_email == "employee@sabahhub.com"  # normalised
        assert cred.domain == "sabahhub.com"
        assert cred.sabah_user_id == "42"
        # Ciphertext, not the raw token.
        assert cred.access_token != "ya29.plaintext"
        assert cred.refresh_token != "1//plaintext-refresh"

    async def test_push_is_idempotent_for_the_same_google_account(
        self, client, db_session, monkeypatch
    ):
        monkeypatch.setattr("app.core.config.settings.INTERNAL_API_KEY", "correct-key")
        monkeypatch.setattr("app.core.config.settings.ALLOWED_EMAIL_DOMAINS", [])
        payload = {
            "sabah_user_id": "43",
            "google_sub": "sub-43",
            "email": "repeat@sabahhub.com",
            "access_token": "ya29.first",
            "refresh_token": "1//refresh",
            "scopes": ["openid"],
        }

        await client.post(
            "/internal/google/credentials",
            headers={"X-Internal-Key": "correct-key"},
            json=payload,
        )
        # Google omits the refresh token on later consents.
        await client.post(
            "/internal/google/credentials",
            headers={"X-Internal-Key": "correct-key"},
            json={**payload, "access_token": "ya29.second", "refresh_token": None},
        )

        rows = (
            await db_session.execute(
                select(GoogleCredential).where(GoogleCredential.user_id == "sub-43")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].refresh_token  # retained, not wiped


# ── MCP server registry ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestServerRegistry:
    async def test_seed_covers_every_service_with_an_mcp_endpoint(self):
        seeded = {row["name"] for row in google_server_seed()}
        expected = {key for key, service in SERVICES.items() if service.mcp_url}
        assert seeded == expected

    async def test_ensure_is_idempotent(self, db_session):
        first = await ensure_google_servers(db_session)
        second = await ensure_google_servers(db_session)
        assert first > 0
        assert second == 0

    async def test_oauth_servers_get_a_bearer_token_once_per_run(
        self, db_session, monkeypatch
    ):
        monkeypatch.setattr(
            "app.mcp.client.access_token_for",
            _fake_access_token(),
        )
        credential = GoogleCredential(
            user_id="sub-tools",
            google_account_email="tools@sabahhub.com",
            access_token="ciphertext",
            refresh_token="ciphertext",
            scopes=[],
            services=[],
        )
        servers = [
            MCPServer(name="gmail", transport="http", url="https://g/mcp", auth_type="oauth", enabled=True),
            MCPServer(name="drive", transport="http", url="https://d/mcp", auth_type="oauth", enabled=True),
            MCPServer(name="off", transport="http", url="https://o/mcp", auth_type="none", enabled=False),
        ]

        tools = await build_mcp_tools_config(credential, servers, db_session)

        assert [t["server_label"] for t in tools] == ["gmail", "drive"]
        assert all(t["headers"]["Authorization"] == "Bearer live-token" for t in tools)
        # One refresh serves every server in the run.
        assert _fake_access_token.calls == 1

    async def test_oauth_servers_are_skipped_without_a_credential(self, db_session):
        servers = [
            MCPServer(name="gmail", transport="http", url="https://g/mcp", auth_type="oauth", enabled=True)
        ]
        assert await build_mcp_tools_config(None, servers, db_session) == []


def _fake_access_token():
    async def _inner(credential, session):
        _fake_access_token.calls += 1
        return "live-token"

    _fake_access_token.calls = 0
    return _inner


_fake_access_token.calls = 0


# ── Resource dispatch ────────────────────────────────────────────────────────

class TestResourceDispatch:
    def test_every_resource_maps_to_a_callable(self):
        assert all(callable(reader) for reader, _ in google_data.RESOURCES.values())

    def test_core_resources_are_registered(self):
        for name in (
            "gmail.messages",
            "drive.files",
            "calendar.events",
            "docs.document",
            "sheets.values",
            "contacts.list",
            "tasks.lists",
            "chat.spaces",
            "snapshot",
        ):
            assert name in google_data.RESOURCES

    @pytest.mark.asyncio
    async def test_unknown_resource_is_refused(self, db_session):
        from app.core.google_client import GoogleAuthError

        with pytest.raises(GoogleAuthError, match="Unknown resource"):
            await google_data.fetch("gmail.everything", None, db_session, {})

    @pytest.mark.asyncio
    async def test_missing_required_parameter_is_refused(self, db_session):
        from app.core.google_client import GoogleAuthError

        with pytest.raises(GoogleAuthError, match="document_id"):
            await google_data.fetch("docs.document", None, db_session, {})
