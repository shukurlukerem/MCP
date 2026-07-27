"""
Direct data-pull across the Google Workspace surface.

The MCP/LLM path is for *reasoning* over a user's data. This module is the
plain path: given a connected corporate account, read the actual records —
mail, files, events, docs, sheets, contacts, tasks, chat, meet — and return
normalised JSON. Both the user-facing API (`app/api/google.py`) and the
internal API used by SABAH.OS call these functions.
"""

import base64
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.google_client import GoogleAuthError, google_request
from app.core.google_scopes import SERVICES
from app.models.credential import GoogleCredential


def _api(service: str, path: str) -> str:
    base = SERVICES[service].api_base
    return f"{base}{path}"


def _decode_body(data: str) -> str:
    """Gmail encodes bodies as URL-safe base64 with the padding stripped."""
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _gmail_plaintext(payload: dict) -> str:
    """Walk a Gmail MIME tree and return the first text/plain part found."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    if mime == "text/plain" and body.get("data"):
        return _decode_body(body["data"])
    for part in payload.get("parts", []) or []:
        text = _gmail_plaintext(part)
        if text:
            return text
    if body.get("data"):
        return _decode_body(body["data"])
    return ""


# ── Gmail ────────────────────────────────────────────────────────────────────

async def gmail_messages(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    query: str = "",
    limit: int = 20,
    label_ids: Optional[List[str]] = None,
) -> List[dict]:
    listing = await google_request(
        cred,
        session,
        "GET",
        _api("gmail", "/users/me/messages"),
        params={
            "q": query or None,
            "maxResults": min(max(limit, 1), 100),
            "labelIds": label_ids or None,
        },
    )

    messages = []
    for stub in listing.get("messages", []) or []:
        detail = await google_request(
            cred,
            session,
            "GET",
            _api("gmail", f"/users/me/messages/{stub['id']}"),
            params={"format": "full"},
        )
        headers = {
            h["name"].lower(): h["value"]
            for h in (detail.get("payload", {}).get("headers", []) or [])
        }
        messages.append(
            {
                "id": detail.get("id"),
                "thread_id": detail.get("threadId"),
                "snippet": detail.get("snippet"),
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "date": headers.get("date", ""),
                "labels": detail.get("labelIds", []),
                "body": _gmail_plaintext(detail.get("payload", {}))[:20000],
            }
        )
    return messages


async def gmail_labels(cred: GoogleCredential, session: AsyncSession) -> List[dict]:
    data = await google_request(cred, session, "GET", _api("gmail", "/users/me/labels"))
    return data.get("labels", []) or []


async def gmail_send(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    to: str,
    subject: str,
    body: str,
) -> dict:
    raw = f"To: {to}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}"
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode().rstrip("=")
    return await google_request(
        cred,
        session,
        "POST",
        _api("gmail", "/users/me/messages/send"),
        json_body={"raw": encoded},
    )


# ── Drive ────────────────────────────────────────────────────────────────────

async def drive_files(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    query: str = "",
    limit: int = 50,
    order_by: str = "modifiedTime desc",
) -> List[dict]:
    data = await google_request(
        cred,
        session,
        "GET",
        _api("drive", "/files"),
        params={
            "q": query or None,
            "pageSize": min(max(limit, 1), 200),
            "orderBy": order_by,
            "fields": (
                "files(id,name,mimeType,size,modifiedTime,createdTime,owners(emailAddress),"
                "webViewLink,parents,shared,trashed)"
            ),
            # Corporate deployment: shared drives count as company data.
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        },
    )
    return data.get("files", []) or []


async def drive_file(cred: GoogleCredential, session: AsyncSession, file_id: str) -> dict:
    return await google_request(
        cred,
        session,
        "GET",
        _api("drive", f"/files/{file_id}"),
        params={
            "fields": "*",
            "supportsAllDrives": "true",
        },
    )


async def drive_export_text(
    cred: GoogleCredential,
    session: AsyncSession,
    file_id: str,
) -> dict:
    """Export a Google-native document as plain text."""
    result = await google_request(
        cred,
        session,
        "GET",
        _api("drive", f"/files/{file_id}/export"),
        params={"mimeType": "text/plain"},
    )
    return {"file_id": file_id, "text": result.get("raw", "") if isinstance(result, dict) else ""}


# ── Calendar ─────────────────────────────────────────────────────────────────

async def calendar_list(cred: GoogleCredential, session: AsyncSession) -> List[dict]:
    data = await google_request(cred, session, "GET", _api("calendar", "/users/me/calendarList"))
    return data.get("items", []) or []


async def calendar_events(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    limit: int = 50,
    query: str = "",
) -> List[dict]:
    data = await google_request(
        cred,
        session,
        "GET",
        _api("calendar", f"/calendars/{calendar_id}/events"),
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": min(max(limit, 1), 250),
            "singleEvents": "true",
            "orderBy": "startTime",
            "q": query or None,
        },
    )
    return data.get("items", []) or []


# ── Docs / Sheets / Slides ───────────────────────────────────────────────────

async def docs_document(cred: GoogleCredential, session: AsyncSession, document_id: str) -> dict:
    return await google_request(cred, session, "GET", _api("docs", f"/documents/{document_id}"))


async def sheets_values(
    cred: GoogleCredential,
    session: AsyncSession,
    spreadsheet_id: str,
    range_a1: str = "A1:Z1000",
) -> dict:
    return await google_request(
        cred,
        session,
        "GET",
        _api("sheets", f"/spreadsheets/{spreadsheet_id}/values/{range_a1}"),
    )


async def sheets_metadata(
    cred: GoogleCredential, session: AsyncSession, spreadsheet_id: str
) -> dict:
    return await google_request(
        cred,
        session,
        "GET",
        _api("sheets", f"/spreadsheets/{spreadsheet_id}"),
        params={"includeGridData": "false"},
    )


async def slides_presentation(
    cred: GoogleCredential, session: AsyncSession, presentation_id: str
) -> dict:
    return await google_request(
        cred, session, "GET", _api("slides", f"/presentations/{presentation_id}")
    )


# ── People / Tasks / Forms / Chat / Meet ─────────────────────────────────────

async def contacts(
    cred: GoogleCredential, session: AsyncSession, *, limit: int = 100
) -> List[dict]:
    data = await google_request(
        cred,
        session,
        "GET",
        _api("contacts", "/people/me/connections"),
        params={
            "pageSize": min(max(limit, 1), 500),
            "personFields": "names,emailAddresses,phoneNumbers,organizations,photos",
        },
    )
    return data.get("connections", []) or []


async def directory_people(
    cred: GoogleCredential, session: AsyncSession, *, limit: int = 100
) -> List[dict]:
    """Colleagues in the company Workspace directory."""
    data = await google_request(
        cred,
        session,
        "GET",
        _api("contacts", "/people:listDirectoryPeople"),
        params={
            "readMask": "names,emailAddresses,organizations,phoneNumbers",
            "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
            "pageSize": min(max(limit, 1), 500),
        },
    )
    return data.get("people", []) or []


async def task_lists(cred: GoogleCredential, session: AsyncSession) -> List[dict]:
    data = await google_request(cred, session, "GET", _api("tasks", "/users/@me/lists"))
    return data.get("items", []) or []


async def tasks_in_list(
    cred: GoogleCredential,
    session: AsyncSession,
    list_id: str,
    *,
    show_completed: bool = True,
) -> List[dict]:
    data = await google_request(
        cred,
        session,
        "GET",
        _api("tasks", f"/lists/{list_id}/tasks"),
        params={"showCompleted": str(show_completed).lower(), "maxResults": 100},
    )
    return data.get("items", []) or []


async def forms_responses(
    cred: GoogleCredential, session: AsyncSession, form_id: str
) -> List[dict]:
    data = await google_request(cred, session, "GET", _api("forms", f"/forms/{form_id}/responses"))
    return data.get("responses", []) or []


async def chat_spaces(cred: GoogleCredential, session: AsyncSession) -> List[dict]:
    data = await google_request(cred, session, "GET", _api("chat", "/spaces"))
    return data.get("spaces", []) or []


async def chat_messages(
    cred: GoogleCredential, session: AsyncSession, space: str, *, limit: int = 50
) -> List[dict]:
    space_id = space if space.startswith("spaces/") else f"spaces/{space}"
    data = await google_request(
        cred,
        session,
        "GET",
        _api("chat", f"/{space_id}/messages"),
        params={"pageSize": min(max(limit, 1), 200)},
    )
    return data.get("messages", []) or []


async def meet_spaces(cred: GoogleCredential, session: AsyncSession) -> List[dict]:
    data = await google_request(cred, session, "GET", _api("meet", "/conferenceRecords"))
    return data.get("conferenceRecords", []) or []


async def profile(cred: GoogleCredential, session: AsyncSession) -> dict:
    return await google_request(
        cred,
        session,
        "GET",
        _api("contacts", "/people/me"),
        params={"personFields": "names,emailAddresses,organizations,photos,locations"},
    )


# ── Aggregate snapshot ───────────────────────────────────────────────────────

# Cheap, bounded reads per service — enough for an overview without pulling a
# user's whole mailbox on page load.
async def workspace_snapshot(
    cred: GoogleCredential,
    session: AsyncSession,
    *,
    services: Optional[List[str]] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    wanted = services or (cred.services or [])
    snapshot: Dict[str, Any] = {"email": cred.google_account_email, "services": {}}

    async def collect(key: str, coro) -> None:
        if key not in wanted:
            return
        try:
            snapshot["services"][key] = await coro
        except GoogleAuthError as exc:
            # One service being unavailable (scope not granted, API disabled)
            # must not blank out the whole snapshot.
            snapshot["services"][key] = {"error": str(exc)[:300]}

    await collect("gmail", gmail_messages(cred, session, limit=limit))
    await collect("drive", drive_files(cred, session, limit=limit))
    await collect("calendar", calendar_events(cred, session, limit=limit))
    await collect("tasks", task_lists(cred, session))
    await collect("contacts", contacts(cred, session, limit=limit))
    await collect("chat", chat_spaces(cred, session))
    return snapshot


# ── Resource dispatch ────────────────────────────────────────────────────────
# A single name → reader map so the HTTP API and the internal API used by
# SABAH.OS expose the same surface without duplicating twenty endpoints.

RESOURCES = {
    "profile": (profile, "contacts"),
    "snapshot": (workspace_snapshot, None),
    "gmail.messages": (gmail_messages, "gmail"),
    "gmail.labels": (gmail_labels, "gmail"),
    "gmail.send": (gmail_send, "gmail"),
    "drive.files": (drive_files, "drive"),
    "drive.file": (drive_file, "drive"),
    "drive.export": (drive_export_text, "drive"),
    "calendar.list": (calendar_list, "calendar"),
    "calendar.events": (calendar_events, "calendar"),
    "docs.document": (docs_document, "docs"),
    "sheets.values": (sheets_values, "sheets"),
    "sheets.metadata": (sheets_metadata, "sheets"),
    "slides.presentation": (slides_presentation, "slides"),
    "contacts.list": (contacts, "contacts"),
    "contacts.directory": (directory_people, "contacts"),
    "tasks.lists": (task_lists, "tasks"),
    "tasks.items": (tasks_in_list, "tasks"),
    "forms.responses": (forms_responses, "forms"),
    "chat.spaces": (chat_spaces, "chat"),
    "chat.messages": (chat_messages, "chat"),
    "meet.records": (meet_spaces, "meet"),
}

# Readers whose first argument after (cred, session) is positional.
_POSITIONAL_ARGS = {
    "drive.file": ["file_id"],
    "drive.export": ["file_id"],
    "docs.document": ["document_id"],
    "sheets.values": ["spreadsheet_id", "range_a1"],
    "sheets.metadata": ["spreadsheet_id"],
    "slides.presentation": ["presentation_id"],
    "tasks.items": ["list_id"],
    "forms.responses": ["form_id"],
    "chat.messages": ["space"],
}


async def fetch(
    resource: str,
    cred: GoogleCredential,
    session: AsyncSession,
    params: Optional[dict] = None,
) -> Any:
    """Read one named resource. Raises ``GoogleAuthError`` for unknown names."""
    entry = RESOURCES.get(resource)
    if entry is None:
        raise GoogleAuthError(
            f"Unknown resource {resource!r}. Available: {sorted(RESOURCES)}"
        )

    reader, _service = entry
    params = dict(params or {})
    positional = []
    for name in _POSITIONAL_ARGS.get(resource, []):
        if name in params:
            positional.append(params.pop(name))
        elif name != "range_a1":  # range_a1 has a default
            raise GoogleAuthError(f"Resource {resource!r} requires parameter {name!r}")

    return await reader(cred, session, *positional, **params)
