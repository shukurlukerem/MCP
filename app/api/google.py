"""
Direct Google Workspace data access for a connected corporate account.

Every route resolves the caller's stored credential, refreshes the access token
when needed, and reads live data. This is the non-LLM path: SABAH.OS uses it to
show a user's mail, files, calendar and the rest without paying for a model call.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.google_client import GoogleAuthError, get_credential, google_request
from app.core.security import get_current_user
from app.models.credential import GoogleCredential
from app.services import google_data

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/google", tags=["google"])


async def current_credential(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GoogleCredential:
    cred = await get_credential(db, user_id=current_user["sub"])
    if not cred:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="No connected Google account — complete Continue with Google first",
        )
    return cred


def _handle(exc: GoogleAuthError):
    message = str(exc)
    if "reconnect required" in message:
        raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail=message)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message)


# ── Named resources ──────────────────────────────────────────────────────────

class ResourceRequest(BaseModel):
    resource: str
    params: dict = {}


@router.get("/resources")
async def list_resources():
    """Every resource name accepted by `POST /google/read`."""
    return {
        "resources": [
            {"resource": name, "service": service}
            for name, (_reader, service) in sorted(google_data.RESOURCES.items())
        ]
    }


@router.post("/read")
async def read_resource(
    request: ResourceRequest,
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Read any named Google resource — the general entry point."""
    try:
        return {"resource": request.resource, "data": await google_data.fetch(
            request.resource, cred, db, request.params
        )}
    except GoogleAuthError as exc:
        _handle(exc)


@router.get("/snapshot")
async def snapshot(
    limit: int = Query(default=10, ge=1, le=50),
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    """One call, every connected service — for a dashboard overview."""
    try:
        return await google_data.workspace_snapshot(cred, db, limit=limit)
    except GoogleAuthError as exc:
        _handle(exc)


# ── Convenience routes (thin wrappers over the same readers) ─────────────────

@router.get("/profile")
async def profile(
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await google_data.profile(cred, db)
    except GoogleAuthError as exc:
        _handle(exc)


@router.get("/gmail/messages")
async def gmail_messages(
    q: str = Query(default="", description="Gmail search query, e.g. 'from:hr newer_than:7d'"),
    limit: int = Query(default=20, ge=1, le=100),
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {"messages": await google_data.gmail_messages(cred, db, query=q, limit=limit)}
    except GoogleAuthError as exc:
        _handle(exc)


@router.get("/drive/files")
async def drive_files(
    q: str = Query(default="", description="Drive query, e.g. \"mimeType='application/pdf'\""),
    limit: int = Query(default=50, ge=1, le=200),
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {"files": await google_data.drive_files(cred, db, query=q, limit=limit)}
    except GoogleAuthError as exc:
        _handle(exc)


@router.get("/calendar/events")
async def calendar_events(
    calendar_id: str = Query(default="primary"),
    time_min: Optional[str] = Query(default=None, description="RFC3339 lower bound"),
    time_max: Optional[str] = Query(default=None, description="RFC3339 upper bound"),
    limit: int = Query(default=50, ge=1, le=250),
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {
            "events": await google_data.calendar_events(
                cred,
                db,
                calendar_id=calendar_id,
                time_min=time_min,
                time_max=time_max,
                limit=limit,
            )
        }
    except GoogleAuthError as exc:
        _handle(exc)


@router.get("/contacts")
async def contacts(
    directory: bool = Query(default=False, description="Read the company directory instead"),
    limit: int = Query(default=100, ge=1, le=500),
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    try:
        if directory:
            return {"people": await google_data.directory_people(cred, db, limit=limit)}
        return {"people": await google_data.contacts(cred, db, limit=limit)}
    except GoogleAuthError as exc:
        _handle(exc)


@router.get("/tasks")
async def tasks(
    list_id: Optional[str] = Query(default=None),
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    try:
        if list_id:
            return {"tasks": await google_data.tasks_in_list(cred, db, list_id)}
        return {"lists": await google_data.task_lists(cred, db)}
    except GoogleAuthError as exc:
        _handle(exc)


# ── Escape hatch ─────────────────────────────────────────────────────────────

class PassthroughRequest(BaseModel):
    method: str = "GET"
    url: str
    params: dict = {}
    json_body: Optional[dict] = None


@router.post("/passthrough")
async def passthrough(
    request: PassthroughRequest = Body(...),
    cred: GoogleCredential = Depends(current_credential),
    db: AsyncSession = Depends(get_db),
):
    """
    Authorised call to any Google API host on the allowlist.

    Google ships new endpoints faster than this service can wrap them; this keeps
    the platform from being blocked on a missing wrapper. The host allowlist in
    `app.core.google_scopes` is what stops the corporate token leaving Google.
    """
    try:
        return await google_request(
            cred,
            db,
            request.method,
            request.url,
            params=request.params or None,
            json_body=request.json_body,
        )
    except GoogleAuthError as exc:
        _handle(exc)
