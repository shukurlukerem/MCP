"""
Incremental Google Calendar synchronisation.

A syncToken turns the hourly run into "what changed since last time". Google
enforces two rules that are easy to get wrong and silent when you do: a token
request may not carry `timeMin`/`timeMax`/`orderBy`, and a response only carries
`nextSyncToken` on its final page. `orderBy` in particular fails *quietly* — the
request succeeds and simply never yields a token, so every run stays a full sync
and nobody notices. These tests pin both, plus the token-expiry path that is the
whole reason the feature can be trusted unattended.
"""

import pytest

from app.core.google_client import GoogleAuthError
from app.models.credential import GoogleCredential
from app.services import google_calendar


@pytest.fixture
def cred() -> GoogleCredential:
    return GoogleCredential(
        user_id="google-sub-1",
        sabah_user_id="1",
        google_account_email="employee@example.com",
        access_token="x",
        refresh_token="y",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )


@pytest.fixture
def captured(monkeypatch):
    """Record the params of each google_request call and reply with `response`."""
    calls: list[dict] = []
    box = {"response": {"items": []}, "raise": None}

    async def fake_request(credential, session, method, url, **kwargs):
        calls.append({"method": method, "url": url, "params": kwargs.get("params") or {}})
        if box["raise"] is not None:
            raise box["raise"]
        return box["response"]

    monkeypatch.setattr(google_calendar, "google_request", fake_request)
    return {"calls": calls, "box": box}


# ── Request shape ────────────────────────────────────────────────────────────

class TestRequestParameters:
    @pytest.mark.asyncio
    async def test_full_sync_anchors_on_time_min_and_omits_order_by(self, cred, captured):
        await google_calendar.list_events_sync(
            cred, None, time_min="2026-08-31T00:00:00+04:00"
        )

        params = captured["calls"][0]["params"]
        assert params["timeMin"] == "2026-08-31T00:00:00+04:00"
        assert params["singleEvents"] == "true"
        assert params["showDeleted"] == "true"
        assert "syncToken" not in params
        # `orderBy` suppresses nextSyncToken, which would keep every run a full
        # sync while looking perfectly healthy.
        assert "orderBy" not in params

    @pytest.mark.asyncio
    async def test_incremental_sends_only_the_token(self, cred, captured):
        await google_calendar.list_events_sync(
            cred, None, sync_token="tok-1", time_min="2026-08-31T00:00:00+04:00"
        )

        params = captured["calls"][0]["params"]
        assert params["syncToken"] == "tok-1"
        # Google answers 400 if any of these accompanies a syncToken.
        assert "timeMin" not in params
        assert "timeMax" not in params
        assert "orderBy" not in params
        # These two must repeat what the token was minted under.
        assert params["singleEvents"] == "true"
        assert params["showDeleted"] == "true"

    @pytest.mark.asyncio
    async def test_page_size_is_clamped_to_googles_maximum(self, cred, captured):
        await google_calendar.list_events_sync(
            cred, None, time_min="2026-08-31T00:00:00Z", max_results=99999
        )

        assert captured["calls"][0]["params"]["maxResults"] == 2500


# ── Response ─────────────────────────────────────────────────────────────────

class TestResponse:
    @pytest.mark.asyncio
    async def test_next_sync_token_is_surfaced(self, cred, captured):
        captured["box"]["response"] = {
            "items": [{"id": "e1"}],
            "nextSyncToken": "tok-2",
            "timeZone": "Asia/Baku",
        }

        result = await google_calendar.list_events_sync(
            cred, None, time_min="2026-08-31T00:00:00Z"
        )

        assert result["next_sync_token"] == "tok-2"
        assert result["calendar_timezone"] == "Asia/Baku"
        assert result["incremental"] is False

    @pytest.mark.asyncio
    async def test_mid_walk_page_carries_no_token(self, cred, captured):
        """Google attaches nextSyncToken to the last page only."""
        captured["box"]["response"] = {"items": [{"id": "e1"}], "nextPageToken": "p2"}

        result = await google_calendar.list_events_sync(
            cred, None, sync_token="tok-1"
        )

        assert result["next_page_token"] == "p2"
        assert result["next_sync_token"] == ""
        assert result["incremental"] is True

    @pytest.mark.asyncio
    async def test_cancelled_events_are_kept_in_the_page(self, cred, captured):
        """showDeleted is what lets the importer retract a deleted event."""
        captured["box"]["response"] = {
            "items": [{"id": "e1", "status": "cancelled"}],
            "nextSyncToken": "tok-2",
        }

        result = await google_calendar.list_events_sync(cred, None, sync_token="tok-1")

        assert result["items"] == [{"id": "e1", "status": "cancelled"}]


# ── Token expiry ─────────────────────────────────────────────────────────────

class TestSyncTokenExpiry:
    @pytest.mark.asyncio
    async def test_410_becomes_sync_token_expired(self, cred, captured):
        captured["box"]["raise"] = GoogleAuthError("Gone", status_code=410)

        with pytest.raises(google_calendar.SyncTokenExpired):
            await google_calendar.list_events_sync(cred, None, sync_token="stale")

    @pytest.mark.asyncio
    async def test_400_naming_the_sync_token_becomes_sync_token_expired(self, cred, captured):
        """Some calendars report an aged-out token as 400, not 410."""
        captured["box"]["raise"] = GoogleAuthError(
            "Google API GET ... failed (400): Sync token is no longer valid",
            status_code=400,
        )

        with pytest.raises(google_calendar.SyncTokenExpired):
            await google_calendar.list_events_sync(cred, None, sync_token="stale")

    @pytest.mark.asyncio
    async def test_an_unrelated_400_stays_a_failure(self, cred, captured):
        captured["box"]["raise"] = GoogleAuthError(
            "Google API GET ... failed (400): Invalid calendar id", status_code=400
        )

        with pytest.raises(GoogleAuthError):
            await google_calendar.list_events_sync(cred, None, sync_token="tok-1")

    @pytest.mark.asyncio
    async def test_a_410_on_a_full_sync_is_not_swallowed(self, cred, captured):
        """No token was sent, so a 410 means something else entirely."""
        captured["box"]["raise"] = GoogleAuthError("Gone", status_code=410)

        with pytest.raises(GoogleAuthError):
            await google_calendar.list_events_sync(
                cred, None, time_min="2026-08-31T00:00:00Z"
            )
