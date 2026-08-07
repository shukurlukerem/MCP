"""
Service-to-service API used by SABAH.OS (Django).

Django owns the user identity: it decides who may sign in, whether the account
exists, and which employee a Google account belongs to. This router lets Django
push the tokens it obtained during "Continue with Google", read a user's Google
data on their behalf, and trigger automation runs — all authenticated with the
shared ``X-Internal-Key`` header, never a user JWT.
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import build_authorization_url
from app.core.config import settings
from app.core.db import get_db
from app.core.google_client import (
    GoogleAuthError,
    get_credential,
    revoke_credential,
    upsert_credential,
)
from app.core.google_scopes import describe_services
from app.core.security import require_internal_key
from app.models.automation_run import AutomationRun
from app.models.mcp_server import MCPServer
from app.services import google_data

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_key)],
    include_in_schema=False,
)


# ── Credential lifecycle ─────────────────────────────────────────────────────

class CredentialPush(BaseModel):
    """Tokens Django obtained from Google on the user's behalf."""

    sabah_user_id: str
    google_sub: str
    email: str
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    scopes: List[str] = Field(default_factory=list)


class CredentialStatus(BaseModel):
    connected: bool
    email: Optional[str] = None
    services: List[str] = []
    scopes: List[str] = []
    token_expiry: Optional[str] = None
    revoked: bool = False


@router.post("/google/credentials", response_model=CredentialStatus)
async def push_credentials(payload: CredentialPush, db: AsyncSession = Depends(get_db)):
    """
    Store (or refresh) a user's Google credential.

    The corporate domain check runs here too — Django is trusted, but a second
    gate costs nothing and keeps this service safe if it is ever called directly.
    """
    if not settings.email_domain_allowed(payload.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Email domain not allowed for {payload.email}",
        )

    try:
        cred = await upsert_credential(
            db,
            google_sub=payload.google_sub,
            email=payload.email,
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
            expiry=payload.expires_at,
            scopes=payload.scopes,
            sabah_user_id=payload.sabah_user_id,
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return CredentialStatus(
        connected=True,
        email=cred.google_account_email,
        services=cred.services or [],
        scopes=cred.scopes or [],
        token_expiry=cred.token_expiry.isoformat() if cred.token_expiry else None,
    )


@router.get("/google/credentials/{sabah_user_id}", response_model=CredentialStatus)
async def credential_status(sabah_user_id: str, db: AsyncSession = Depends(get_db)):
    cred = await get_credential(db, sabah_user_id=sabah_user_id)
    if not cred:
        return CredentialStatus(connected=False)
    return CredentialStatus(
        connected=True,
        email=cred.google_account_email,
        services=cred.services or [],
        scopes=cred.scopes or [],
        token_expiry=cred.token_expiry.isoformat() if cred.token_expiry else None,
        revoked=cred.revoked,
    )


@router.delete("/google/credentials/{sabah_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(sabah_user_id: str, db: AsyncSession = Depends(get_db)):
    cred = await get_credential(db, sabah_user_id=sabah_user_id)
    if cred:
        await revoke_credential(cred, db)
    return None


@router.get("/google/auth-url")
async def auth_url(
    sabah_user_id: str = Query(...),
    redirect_to: Optional[str] = Query(default=None),
):
    """Consent URL bound to a SABAH.OS user, for the connect-only (no login) flow."""
    return build_authorization_url(sabah_user_id=sabah_user_id, redirect_to=redirect_to)


@router.get("/google/services")
async def services():
    return {
        "services": describe_services(settings.GOOGLE_SERVICES),
        "allowed_domains": settings.ALLOWED_EMAIL_DOMAINS,
    }


# ── Data pull on behalf of a SABAH.OS user ───────────────────────────────────

class ReadRequest(BaseModel):
    sabah_user_id: str
    resource: str
    params: dict = {}


@router.post("/google/read")
async def read(payload: ReadRequest, db: AsyncSession = Depends(get_db)) -> Any:
    cred = await get_credential(db, sabah_user_id=payload.sabah_user_id)
    if not cred:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="User has no connected Google account",
        )
    try:
        data = await google_data.fetch(payload.resource, cred, db, payload.params)
    except GoogleAuthError as exc:
        message = str(exc)
        code = (
            status.HTTP_412_PRECONDITION_FAILED
            if "reconnect required" in message
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=code, detail=message)
    return {"resource": payload.resource, "data": data}


# ── Automation runs ──────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    sabah_user_id: str
    instructions: str
    # Server names (e.g. ["gmail", "drive"]). Empty = every enabled Google server.
    servers: List[str] = Field(default_factory=list)
    tool_name: str = "assistant"
    input_payload: dict = {}


class RunAccepted(BaseModel):
    run_id: int
    status: str


@router.post("/automation/run", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(payload: RunRequest, db: AsyncSession = Depends(get_db)):
    """Queue an LLM ⇄ MCP run for a SABAH.OS user."""
    from app.workers.tasks import LLM_DISABLED_MESSAGE, execute_mcp_task, llm_available

    cred = await get_credential(db, sabah_user_id=payload.sabah_user_id)
    if not cred:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="User has no connected Google account",
        )

    # Same gate the user-facing route applies. Without it a deployment with no
    # LLM configured accepts every run with 202 and then fails it in the worker,
    # so the Integrations page shows a spinner that resolves into nothing.
    if not llm_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=LLM_DISABLED_MESSAGE,
        )

    query = select(MCPServer).where(MCPServer.enabled.is_(True))
    if payload.servers:
        query = query.where(MCPServer.name.in_(payload.servers))
    servers = (await db.execute(query)).scalars().all()
    if not servers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No enabled MCP servers match the request",
        )

    run = AutomationRun(
        user_id=cred.user_id,
        sabah_user_id=str(payload.sabah_user_id),
        mcp_server_id=servers[0].id if len(servers) == 1 else None,
        server_names=[s.name for s in servers],
        tool_name=payload.tool_name,
        input_payload={**payload.input_payload, "instructions": payload.instructions},
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Publishing to Celery is blocking socket I/O. Awaiting it directly on the
    # event loop stalls every other request in this process — the API runs a
    # single uvicorn worker — for as long as an unreachable broker takes to give
    # up, which is long enough for the proxy in front to answer 502 instead.
    try:
        await run_in_threadpool(execute_mcp_task.delay, run.id, cred.user_id)
    except Exception as exc:
        # The row is already committed; leaving it "pending" would make the UI
        # poll a run nobody will ever pick up.
        logger.exception("Could not queue automation run %s", run.id)
        run.status = "error"
        run.error_message = f"Could not queue the run: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The automation queue is unavailable — the run was not started.",
        )

    return RunAccepted(run_id=run.id, status="pending")


def _serialise_run(run: AutomationRun) -> dict:
    return {
        "run_id": run.id,
        "status": run.status,
        "tool_name": run.tool_name,
        "servers": run.server_names or [],
        "instructions": (run.input_payload or {}).get("instructions"),
        "output_payload": run.output_payload,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.get("/automation/runs")
async def list_runs(
    sabah_user_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AutomationRun)
        .where(AutomationRun.sabah_user_id == str(sabah_user_id))
        .order_by(AutomationRun.started_at.desc())
        .limit(limit)
    )
    return {"runs": [_serialise_run(r) for r in result.scalars().all()]}


@router.get("/automation/run/{run_id}")
async def get_run(
    run_id: int,
    sabah_user_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(AutomationRun, run_id)
    # Ownership is enforced even on the internal API: a bug in Django must not
    # turn into one employee reading another's automation output.
    if not run or run.sabah_user_id != str(sabah_user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return _serialise_run(run)


@router.get("/health")
async def internal_health(db: AsyncSession = Depends(get_db)):
    await db.execute(select(MCPServer.id).limit(1))
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
