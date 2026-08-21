"""
Tests for the restricted-grant clearing job.

The job's whole point is to make a truthful claim about Google's side, so the
cases that matter most are the failure ones: a grant this job could not actually
revoke must never be reported as cleared, or the operator submits for verification
believing restricted grants are gone when Google still lists them.
"""

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete

from app.core.security import encrypt_token
from app.models.credential import GoogleCredential
from app.services.restricted_grants import (
    ALREADY_GONE,
    FOUND,
    LOCAL_ONLY,
    REVOKED,
    clear_restricted_grants,
    find_restricted_grants,
)

DRIVE = "https://www.googleapis.com/auth/drive"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR = "https://www.googleapis.com/auth/calendar"


@pytest.fixture(autouse=True)
def _fernet(monkeypatch, fernet_key):
    """Give the encryption helpers a usable key for these tests."""
    monkeypatch.setattr("app.core.config.settings.FERNET_KEY", fernet_key)
    monkeypatch.setattr("app.core.security._fernet", None)


@pytest_asyncio.fixture(autouse=True)
async def _clean_credentials(db_session):
    """
    Start each test with an empty credential table.

    The suite shares one in-memory engine and `db_session` only rolls back, so
    rows this module commits would otherwise leak into the next test and change
    what the scan finds.
    """
    await db_session.execute(delete(GoogleCredential))
    await db_session.commit()


def _mock_revoke(monkeypatch, *, status_code=200, text=""):
    """Stand in for Google's revoke endpoint."""
    calls: list[dict] = []

    class _Response:
        def __init__(self):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None, headers=None):
            calls.append({"url": url, "data": data})
            if isinstance(status_code, Exception):
                raise status_code
            return _Response()

    monkeypatch.setattr("app.services.restricted_grants.httpx.AsyncClient", _Client)
    return calls


async def _add(session, *, email, scopes, revoked=False, refresh="refresh-token"):
    cred = GoogleCredential(
        user_id=f"sub-{email}",
        google_account_email=email,
        access_token=encrypt_token("access-token"),
        refresh_token=encrypt_token(refresh) if refresh else "not-decryptable",
        scopes=scopes,
        services=[],
        revoked=revoked,
    )
    session.add(cred)
    await session.commit()
    return cred


@pytest.mark.asyncio
class TestFindRestrictedGrants:
    async def test_only_returns_grants_carrying_a_restricted_scope(self, db_session):
        await _add(db_session, email="legacy@sabahhub.com", scopes=[DRIVE, CALENDAR])
        await _add(db_session, email="clean@sabahhub.com", scopes=[CALENDAR])

        found = await find_restricted_grants(db_session)

        assert [c.google_account_email for c in found] == ["legacy@sabahhub.com"]

    async def test_skips_already_revoked_rows(self, db_session):
        await _add(
            db_session, email="gone@sabahhub.com", scopes=[GMAIL_READONLY], revoked=True
        )

        assert await find_restricted_grants(db_session) == []


@pytest.mark.asyncio
class TestClearRestrictedGrants:
    async def test_dry_run_changes_nothing(self, db_session, monkeypatch):
        calls = _mock_revoke(monkeypatch)
        cred = await _add(db_session, email="legacy@sabahhub.com", scopes=[DRIVE])

        report = await clear_restricted_grants(db_session, apply=False)

        assert report.affected == 1
        assert report.outcomes[0].status == FOUND
        assert report.outcomes[0].restricted_scopes == [DRIVE]
        assert calls == [], "dry run must not call Google"
        assert cred.revoked is False

    async def test_apply_revokes_and_marks_the_row(self, db_session, monkeypatch):
        calls = _mock_revoke(monkeypatch)
        cred = await _add(db_session, email="legacy@sabahhub.com", scopes=[DRIVE])

        report = await clear_restricted_grants(db_session, apply=True)

        assert report.outcomes[0].status == REVOKED
        assert report.needs_manual_followup == []
        assert cred.revoked is True
        assert calls[0]["data"] == {"token": "refresh-token"}

    async def test_token_already_invalid_counts_as_cleared(self, db_session, monkeypatch):
        _mock_revoke(monkeypatch, status_code=400, text='{"error": "invalid_token"}')
        await _add(db_session, email="legacy@sabahhub.com", scopes=[GMAIL_READONLY])

        report = await clear_restricted_grants(db_session, apply=True)

        assert report.outcomes[0].status == ALREADY_GONE
        assert len(report.cleared) == 1
        assert report.needs_manual_followup == []

    async def test_failed_revoke_is_reported_as_still_live(self, db_session, monkeypatch):
        _mock_revoke(monkeypatch, status_code=500, text="boom")
        cred = await _add(db_session, email="legacy@sabahhub.com", scopes=[DRIVE])

        report = await clear_restricted_grants(db_session, apply=True)

        assert report.outcomes[0].status == LOCAL_ONLY
        assert [o.email for o in report.needs_manual_followup] == ["legacy@sabahhub.com"]
        assert report.cleared == []
        # Locally unusable either way — but the report must not claim it is gone.
        assert cred.revoked is True

    async def test_network_error_is_reported_as_still_live(self, db_session, monkeypatch):
        _mock_revoke(monkeypatch, status_code=httpx.ConnectError("no route"))
        await _add(db_session, email="legacy@sabahhub.com", scopes=[DRIVE])

        report = await clear_restricted_grants(db_session, apply=True)

        assert report.outcomes[0].status == LOCAL_ONLY
        assert len(report.needs_manual_followup) == 1

    async def test_undecryptable_token_is_reported_as_still_live(
        self, db_session, monkeypatch
    ):
        calls = _mock_revoke(monkeypatch)
        cred = await _add(db_session, email="legacy@sabahhub.com", scopes=[DRIVE])
        # Ciphertext from a different key — decryption fails, so there is no token
        # to hand Google and the grant survives on their side.
        cred.refresh_token = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
        await db_session.commit()

        report = await clear_restricted_grants(db_session, apply=True)

        assert report.outcomes[0].status == LOCAL_ONLY
        assert "decrypt" in report.outcomes[0].detail
        assert calls == [], "must not call Google without a token"
        assert len(report.needs_manual_followup) == 1

    async def test_limit_caps_how_many_are_touched(self, db_session, monkeypatch):
        _mock_revoke(monkeypatch)
        for i in range(3):
            await _add(db_session, email=f"legacy{i}@sabahhub.com", scopes=[DRIVE])

        report = await clear_restricted_grants(db_session, apply=True, limit=1)

        assert report.scanned == 3
        assert report.affected == 1

    async def test_clean_estate_reports_nothing(self, db_session, monkeypatch):
        _mock_revoke(monkeypatch)
        await _add(db_session, email="clean@sabahhub.com", scopes=[CALENDAR])

        report = await clear_restricted_grants(db_session, apply=True)

        assert report.scanned == 0
        assert report.affected == 0
        assert report.needs_manual_followup == []
