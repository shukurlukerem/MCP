"""
Scope tiers, and the switch that pauses Workspace access.

These tests exist because of a concrete production failure: the app requested
sensitive and restricted scopes while unverified, so every user hit "Google hasn't
verified this app" and the project's lifetime 100-user cap was exhausted. The
invariants below are what keep that from silently coming back — most of all that
``include_granted_scopes`` stays off, which is the one setting that would make a
scope reduction *look* applied while Google still re-attached the old scopes.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from app.core.google_policy import (
    CAPABILITY_CALENDAR_CONFERENCE,
    CAPABILITY_WORKSPACE,
    WorkspaceDisabledError,
    ensure_allowed,
)
from app.core.google_scopes import (
    CALENDAR_TIER_SCOPES,
    LOGIN_SCOPES,
    RESTRICTED_SCOPES,
    SERVICES,
    WORKSPACE_SCOPES,
    granted_restricted_scopes,
    scopes_for_tier,
    services_from_scopes,
)


def _authorization_scopes(monkeypatch, tier: str) -> list[str]:
    """Build a real authorization URL for *tier* and return its scope parameter."""
    from app.api import auth as auth_api

    monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr("app.core.config.settings.GOOGLE_OAUTH_SCOPE_TIER", tier)

    url = auth_api.build_authorization_url()["authorization_url"]
    params = parse_qs(urlparse(url).query)
    return params["scope"][0].split()


# ── The scope catalogue ──────────────────────────────────────────────────────

class TestScopeTiers:
    def test_login_only_requests_identity_and_nothing_else(self, monkeypatch):
        assert _authorization_scopes(monkeypatch, "login_only") == [
            "openid",
            "email",
            "profile",
        ]

    def test_full_requests_exactly_the_workspace_set(self, monkeypatch):
        assert _authorization_scopes(monkeypatch, "full") == WORKSPACE_SCOPES

    def test_calendar_tier_asks_for_identity_and_calendar_only(self, monkeypatch):
        """
        The tier that turns the calendar page on. Anything beyond Calendar here
        would mean an employee consenting to Gmail and Drive to see their agenda.
        """
        scopes = _authorization_scopes(monkeypatch, "calendar")

        assert scopes == CALENDAR_TIER_SCOPES
        assert scopes[:3] == ["openid", "email", "profile"]
        assert set(SERVICES["calendar"].scopes).issubset(scopes)
        assert not any("gmail" in scope or "drive" in scope for scope in scopes)

    def test_calendar_tier_can_both_read_and_mint_a_meet_link(self, monkeypatch):
        """
        Read-only would have been narrower, but the Meet button is a write and is
        already shipped — a read-only grant would break it for everyone who
        consents after the switch.
        """
        from app.services.google_calendar import (
            CALENDAR_READ_SCOPES,
            CALENDAR_SCOPES,
        )

        scopes = set(_authorization_scopes(monkeypatch, "calendar"))

        assert scopes & CALENDAR_SCOPES
        assert scopes & CALENDAR_READ_SCOPES

    def test_calendar_tier_carries_no_restricted_scope(self, monkeypatch):
        """No restricted scope means no annual paid CASA assessment."""
        scopes = _authorization_scopes(monkeypatch, "calendar")

        assert granted_restricted_scopes(scopes) == []

    def test_login_only_is_the_default(self):
        # A deployment that never sets the variable must not ask for sensitive
        # scopes. This is the setting that keeps sign-in working while unverified.
        from app.core.config import Settings

        assert Settings().GOOGLE_OAUTH_SCOPE_TIER == "login_only"

    def test_an_unrecognised_tier_falls_back_to_identity_only(self):
        # Failing *open* here would mean a typo silently requesting sensitive
        # scopes — the exact failure this module exists to prevent.
        assert scopes_for_tier("ful") == LOGIN_SCOPES
        assert scopes_for_tier("") == LOGIN_SCOPES

    def test_config_rejects_an_invalid_tier(self):
        from pydantic import ValidationError

        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(GOOGLE_OAUTH_SCOPE_TIER="everything")


class TestNoRestrictedScopes:
    def test_no_restricted_scope_is_ever_requested(self, monkeypatch):
        # Restricted scopes oblige an annual paid CASA assessment. Neither tier may
        # carry one.
        for tier in ("login_only", "full"):
            requested = set(_authorization_scopes(monkeypatch, tier))
            assert not requested & RESTRICTED_SCOPES

    def test_the_removed_scopes_are_the_three_restricted_ones(self):
        assert RESTRICTED_SCOPES == {
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        }

    def test_drive_is_requested_as_drive_file(self):
        assert "https://www.googleapis.com/auth/drive.file" in WORKSPACE_SCOPES
        assert "https://www.googleapis.com/auth/drive" not in WORKSPACE_SCOPES

    def test_gmail_is_send_and_labels_only(self):
        gmail = [s for s in WORKSPACE_SCOPES if "gmail" in s]
        assert gmail == [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.labels",
        ]


class TestLegacyGrants:
    """
    Reducing what we request does not revoke what was granted, so stored grants
    must still read back honestly — both to avoid telling a user their Drive access
    is gone when Google says otherwise, and to let the re-consent job find them.
    """

    LEGACY_GRANT = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/calendar",
    ]

    def test_a_legacy_grant_still_maps_to_its_services(self):
        services = services_from_scopes(self.LEGACY_GRANT)
        assert "gmail" in services
        assert "drive" in services
        assert "calendar" in services

    def test_restricted_scopes_in_a_legacy_grant_are_detected(self):
        assert granted_restricted_scopes(self.LEGACY_GRANT) == [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.readonly",
        ]

    def test_a_clean_grant_reports_nothing_to_re_consent(self):
        assert granted_restricted_scopes(WORKSPACE_SCOPES) == []
        assert granted_restricted_scopes([]) == []


# ── include_granted_scopes ───────────────────────────────────────────────────

class TestIncrementalAuthIsOff:
    def test_include_granted_scopes_is_absent(self, monkeypatch):
        """
        The single most likely way for the fix to look like it failed.

        With `include_granted_scopes=true`, Google re-attaches every scope the user
        previously granted, so a returning employee would still see the unverified
        app screen even though this request asks only for identity.
        """
        from app.api import auth as auth_api

        monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_ID", "test-client-id")
        monkeypatch.setattr("app.core.config.settings.GOOGLE_CLIENT_SECRET", "test-secret")

        url = auth_api.build_authorization_url()["authorization_url"]
        params = parse_qs(urlparse(url).query)

        assert "include_granted_scopes" not in params
        # These two must survive: without them there is no refresh token.
        assert params["access_type"] == ["offline"]
        # `select_account` gives the returning user the account chooser rather
        # than the bare email/password form; `consent` keeps the refresh token.
        assert set(params["prompt"][0].split()) == {"select_account", "consent"}


# ── Fail-closed behaviour ────────────────────────────────────────────────────

class TestWorkspacePause:
    def test_workspace_calls_fail_closed_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "app.core.config.settings.GOOGLE_WORKSPACE_INTEGRATIONS_ENABLED", False
        )
        with pytest.raises(WorkspaceDisabledError) as excinfo:
            ensure_allowed(CAPABILITY_WORKSPACE)
        # A user-facing sentence, not a stack trace and not a silent no-op.
        assert "temporarily paused" in str(excinfo.value)

    def test_an_unknown_capability_gets_no_exemption(self, monkeypatch):
        monkeypatch.setattr(
            "app.core.config.settings.GOOGLE_WORKSPACE_INTEGRATIONS_ENABLED", False
        )
        with pytest.raises(WorkspaceDisabledError):
            ensure_allowed("typo_capability")

    def test_meet_stays_available_while_the_read_surface_is_paused(self, monkeypatch):
        # Narrow, user-initiated, sensitive-not-restricted: it keeps working for
        # grants that already carry Calendar access.
        monkeypatch.setattr(
            "app.core.config.settings.GOOGLE_WORKSPACE_INTEGRATIONS_ENABLED", False
        )
        monkeypatch.setattr(
            "app.core.config.settings.GOOGLE_CALENDAR_CONFERENCE_ENABLED", True
        )
        ensure_allowed(CAPABILITY_CALENDAR_CONFERENCE)  # must not raise

    def test_meet_can_be_paused_independently(self, monkeypatch):
        monkeypatch.setattr(
            "app.core.config.settings.GOOGLE_CALENDAR_CONFERENCE_ENABLED", False
        )
        with pytest.raises(WorkspaceDisabledError):
            ensure_allowed(CAPABILITY_CALENDAR_CONFERENCE)

    @pytest.mark.asyncio
    async def test_google_request_refuses_before_touching_the_network(self, monkeypatch):
        """
        The guard sits at the single chokepoint, so no call site can bypass it —
        including one added later that forgets the pause exists.
        """
        import httpx

        from app.core.google_client import google_request

        monkeypatch.setattr(
            "app.core.config.settings.GOOGLE_WORKSPACE_INTEGRATIONS_ENABLED", False
        )

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a paused capability reached the network")

        monkeypatch.setattr(httpx.AsyncClient, "request", explode)

        with pytest.raises(WorkspaceDisabledError):
            await google_request(
                None, None, "GET", "https://gmail.googleapis.com/gmail/v1/users/me/labels"
            )

    @pytest.mark.asyncio
    async def test_read_routes_answer_503_with_the_reason(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "app.core.config.settings.GOOGLE_WORKSPACE_INTEGRATIONS_ENABLED", False
        )
        response = await client.get("/google/snapshot", headers=auth_headers)
        assert response.status_code == 503
        assert "temporarily paused" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_arbitrary_url_passthrough_is_gone(self, client, auth_headers):
        """
        It attached a live corporate token to a caller-supplied URL. Removed, not
        merely flag-gated, so it cannot be switched back on with the rest.
        """
        response = await client.post(
            "/google/passthrough",
            headers=auth_headers,
            json={"method": "GET", "url": "https://gmail.googleapis.com/gmail/v1/users/me/labels"},
        )
        assert response.status_code == 404


# ── The public disclosure endpoint ───────────────────────────────────────────

class TestServicesDisclosure:
    @pytest.mark.asyncio
    async def test_it_is_public_and_reports_the_posture(self, client):
        # Unauthenticated on purpose: an OAuth reviewer checks the public page
        # against it, and CI compares the landing page's scope list to it.
        response = await client.get("/auth/google/services")
        assert response.status_code == 200

        body = response.json()
        assert body["scope_tier"] == "login_only"
        assert body["requested_scopes"] == LOGIN_SCOPES
        assert body["workspace_scopes"] == WORKSPACE_SCOPES
        assert sorted(RESTRICTED_SCOPES) == body["restricted_scopes_removed"]
