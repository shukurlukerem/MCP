"""
Google Calendar writes — the Google Meet conference lifecycle.

SABAH.OS (Django) is the system of record for calendar events, and only Google
can mint a Meet link. Django therefore owns the ``CalendarEvent`` model and
builds the Google event resource; this module owns the credential and the API
call. Django used to hold a second copy of the refresh token and exchange it
itself, which meant two services both refreshing the same grant with two
different sets of client credentials. This service is the only token holder now.

Kept out of ``google_data`` deliberately: that module is the broad read surface
that pauses wholesale during Google verification, whereas minting a Meet link is
a narrow, user-initiated write that stays available to grants which already
carry a Calendar scope.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from typing import Optional

from app.core.google_client import GoogleAuthError, google_request
from app.core.google_policy import (
    CAPABILITY_CALENDAR_CONFERENCE,
    CAPABILITY_CALENDAR_SYNC,
)
from app.core.google_scopes import SERVICES
from app.models.credential import GoogleCredential

# Either Calendar scope is enough to create an event with a conference request.
CALENDAR_SCOPES = frozenset(SERVICES["calendar"].scopes)
# Reading the user's own calendar also works under the read-only scope, which a
# grant may carry even though it can never mint a conference.
CALENDAR_READ_SCOPES = CALENDAR_SCOPES | {
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
}

# One page of a calendar window. Google caps this at 2500; 250 keeps a single
# response small enough that a slow link does not time out mid-sync.
EVENTS_PAGE_SIZE = 250


def granted_conference_access(cred: GoogleCredential) -> bool:
    """
    True when this grant still carries a Calendar scope.

    Checked against what Google actually granted, not against what the app now
    requests: an employee who consented before the scope reduction can keep
    using Meet, while a newly signed-in user simply has no Calendar access and
    gets a clear refusal instead of a 403 from Google.
    """
    return bool(CALENDAR_SCOPES.intersection(cred.scopes or []))


def granted_calendar_read(cred: GoogleCredential) -> bool:
    """True when this grant can read the user's calendar."""
    return bool(CALENDAR_READ_SCOPES.intersection(cred.scopes or []))


def _api(path: str) -> str:
    return f"{SERVICES['calendar'].api_base}{path}"


def _meet_link(event: dict) -> str:
    """Pull the Meet URL out of a created event resource."""
    link = event.get("hangoutLink", "")
    if link:
        return link
    # Newer responses carry it only under conferenceData.entryPoints.
    for entry in (event.get("conferenceData") or {}).get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return ""


async def create_conference(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    event: dict,
    request_id: str,
    calendar_id: str = "primary",
) -> dict:
    """
    Mirror an event into the organiser's calendar with a Meet conference attached.

    ``request_id`` must be stable per event — Google de-duplicates conference
    creation on it, so a retry returns the same Meet link instead of a second one.
    """
    body = dict(event)
    body["conferenceData"] = {
        "createRequest": {
            "requestId": request_id,
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }
    }

    data = await google_request(
        cred,
        session,
        "POST",
        _api(f"/calendars/{calendar_id}/events"),
        params={"conferenceDataVersion": 1, "sendUpdates": "none"},
        json_body=body,
        capability=CAPABILITY_CALENDAR_CONFERENCE,
    )

    return {
        "meet_link": _meet_link(data),
        "google_event_id": data.get("id", ""),
        "google_calendar_id": cred.google_account_email,
    }


async def update_event(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    google_event_id: str,
    event: dict,
    calendar_id: str = "primary",
) -> dict:
    """Push an edit to a previously mirrored event."""
    return await google_request(
        cred,
        session,
        "PATCH",
        _api(f"/calendars/{calendar_id}/events/{google_event_id}"),
        params={"sendUpdates": "none"},
        json_body=event,
        capability=CAPABILITY_CALENDAR_CONFERENCE,
    )


async def delete_event(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    google_event_id: str,
    calendar_id: str = "primary",
) -> dict:
    """Delete a previously mirrored event."""
    return await google_request(
        cred,
        session,
        "DELETE",
        _api(f"/calendars/{calendar_id}/events/{google_event_id}"),
        params={"sendUpdates": "none"},
        capability=CAPABILITY_CALENDAR_CONFERENCE,
    )


async def list_events(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    page_token: Optional[str] = None,
    max_results: int = EVENTS_PAGE_SIZE,
) -> dict:
    """
    One page of the user's own events in [time_min, time_max).

    ``singleEvents`` expands recurrences into concrete occurrences, which is what
    SABAH.OS stores (it materialises series too), and ``showDeleted`` keeps
    cancellations in the page so the importer can retract an event that vanished
    from Google rather than leaving a ghost on the grid.
    """
    data = await google_request(
        cred,
        session,
        "GET",
        _api(f"/calendars/{calendar_id}/events"),
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": min(max(max_results, 1), 2500),
            "singleEvents": "true",
            "orderBy": "startTime",
            "showDeleted": "true",
            "pageToken": page_token or None,
        },
        capability=CAPABILITY_CALENDAR_SYNC,
    )
    return {
        "items": data.get("items", []) or [],
        "next_page_token": data.get("nextPageToken", "") or "",
        "calendar_timezone": data.get("timeZone", "") or "",
        "calendar_id": calendar_id,
    }


# ── Incremental synchronisation ──────────────────────────────────────────────
# `list_events` above answers "what is in this window right now" and is what the
# calendar page reads. The pair below answers "what changed since last time",
# which is a different Google request: a syncToken cannot be combined with
# `timeMin`, `timeMax` or `orderBy` (Google answers 400), and a response only
# carries `nextSyncToken` on its final page.


class SyncTokenExpired(Exception):
    """
    Google refused the stored syncToken — the caller must redo a full sync.

    Routine rather than exceptional: Google expires sync tokens on its own
    schedule and after certain calendar-wide changes, and the documented
    response is to discard the token and start over. Kept separate from
    ``GoogleAuthError`` precisely so a caller does not treat it as a failure.
    """


# Google signals an aged-out token with 410 Gone. Some calendars answer 400 with
# `"Sync token is no longer valid"` in the body instead, which means the same
# thing and must not be reported to the member as a hard error.
def _sync_token_rejected(exc: GoogleAuthError) -> bool:
    code = getattr(exc, "status_code", None)
    if code == 410:
        return True
    return code == 400 and "sync token" in str(exc).lower()


async def list_events_sync(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    calendar_id: str = "primary",
    sync_token: str = "",
    time_min: Optional[str] = None,
    page_token: Optional[str] = None,
    max_results: int = EVENTS_PAGE_SIZE,
) -> dict:
    """
    One page of either a full sync (``time_min``) or an incremental one
    (``sync_token``).

    ``singleEvents`` and ``showDeleted`` are sent on both, because Google
    requires an incremental request to repeat the settings its token was minted
    under. ``orderBy`` is sent on neither: it suppresses ``nextSyncToken``
    entirely, which would silently keep every run a full sync.

    Raises ``SyncTokenExpired`` when the token has aged out, so the caller can
    drop it and start again.
    """
    if sync_token:
        params: dict = {"syncToken": sync_token}
    else:
        params = {"timeMin": time_min}

    params.update(
        {
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": min(max(max_results, 1), 2500),
            "pageToken": page_token or None,
        }
    )

    try:
        data = await google_request(
            cred,
            session,
            "GET",
            _api(f"/calendars/{calendar_id}/events"),
            params=params,
            capability=CAPABILITY_CALENDAR_SYNC,
        )
    except GoogleAuthError as exc:
        if sync_token and _sync_token_rejected(exc):
            raise SyncTokenExpired(str(exc)) from exc
        raise

    return {
        "items": data.get("items", []) or [],
        "next_page_token": data.get("nextPageToken", "") or "",
        # Present only on the last page; an empty value mid-walk is expected.
        "next_sync_token": data.get("nextSyncToken", "") or "",
        "calendar_timezone": data.get("timeZone", "") or "",
        "calendar_id": calendar_id,
        "incremental": bool(sync_token),
    }
