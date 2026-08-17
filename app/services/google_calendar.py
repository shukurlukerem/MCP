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

from app.core.google_client import google_request
from app.core.google_scopes import SERVICES
from app.models.credential import GoogleCredential

# Either Calendar scope is enough to create an event with a conference request.
CALENDAR_SCOPES = frozenset(SERVICES["calendar"].scopes)


def granted_conference_access(cred: GoogleCredential) -> bool:
    """
    True when this grant still carries a Calendar scope.

    Checked against what Google actually granted, not against what the app now
    requests: an employee who consented before the scope reduction can keep
    using Meet, while a newly signed-in user simply has no Calendar access and
    gets a clear refusal instead of a 403 from Google.
    """
    return bool(CALENDAR_SCOPES.intersection(cred.scopes or []))


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
    )
