"""
Google OAuth — "Continue with Google" for corporate accounts.

The consent screen asks for the full Workspace scope set in one pass, so a user
connects once and every service (Gmail, Drive, Calendar, Docs, Sheets, …) becomes
readable by the automation layer. Only accounts on the configured corporate
domain(s) are accepted.
"""

import logging
from datetime import timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.google_client import (
    GoogleAuthError,
    get_credential,
    revoke_credential,
    upsert_credential,
)
from app.core.google_scopes import describe_services, scopes_for, services_from_scopes
from app.core.security import (
    create_access_token,
    create_oauth_state,
    get_current_user,
    verify_oauth_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def google_scopes() -> list:
    return scopes_for(settings.GOOGLE_SERVICES)


def _build_flow(state: Optional[str] = None) -> Flow:
    config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(
        config,
        scopes=google_scopes(),
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
        state=state,
    )


def build_authorization_url(
    *,
    sabah_user_id: Optional[str] = None,
    redirect_to: Optional[str] = None,
) -> dict:
    """
    Build the Google consent URL plus a signed state.

    ``sabah_user_id`` is carried through the state so the callback can link the
    Google account to the SABAH.OS user without a server-side session.
    """
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured on this server",
        )

    state = create_oauth_state(
        {
            "sabah_user_id": str(sabah_user_id) if sabah_user_id else None,
            "redirect_to": redirect_to or settings.FRONTEND_REDIRECT_URL,
        }
    )
    flow = _build_flow(state=state)
    kwargs = {
        "access_type": "offline",         # required for a refresh token
        "include_granted_scopes": "true",
        "prompt": "consent",              # force refresh token on re-consent
        "state": state,
    }
    if settings.GOOGLE_HOSTED_DOMAIN:
        kwargs["hd"] = settings.GOOGLE_HOSTED_DOMAIN

    auth_url, _ = flow.authorization_url(**kwargs)
    return {"authorization_url": auth_url, "state": state}


class AuthURLResponse(BaseModel):
    authorization_url: str
    state: str


@router.get("/google/url", response_model=AuthURLResponse)
async def google_auth_url(
    redirect_to: Optional[str] = Query(
        default=None,
        description="Where to send the browser after the callback completes",
    ),
):
    """Return the Google consent URL (for clients that redirect themselves)."""
    return build_authorization_url(redirect_to=redirect_to)


@router.get("/google/login")
async def google_login(redirect_to: Optional[str] = Query(default=None)):
    """Begin the Google OAuth flow by redirecting to the consent screen."""
    return RedirectResponse(url=build_authorization_url(redirect_to=redirect_to)["authorization_url"])


def _callback_redirect(target: str, params: dict) -> RedirectResponse:
    separator = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{separator}{urlencode(params)}")


@router.get("/google/callback")
async def google_callback(
    code: Optional[str] = Query(default=None),
    state: str = Query(...),
    error: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth callback: exchange the code, verify the identity, enforce the corporate
    domain, store the encrypted credential, and hand a JWT back to the frontend.
    """
    state_data = verify_oauth_state(state)
    redirect_to = state_data.get("redirect_to") or settings.FRONTEND_REDIRECT_URL

    if error or not code:
        return _callback_redirect(
            redirect_to, {"status": "error", "code": error or "missing_code"}
        )

    try:
        flow = _build_flow(state=state)
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.warning("Google token exchange failed: %s", exc)
        return _callback_redirect(redirect_to, {"status": "error", "code": "token_exchange_failed"})

    credentials = flow.credentials
    try:
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except Exception as exc:
        logger.warning("Google ID token verification failed: %s", exc)
        return _callback_redirect(redirect_to, {"status": "error", "code": "invalid_id_token"})

    email = (id_info.get("email") or "").lower()
    if not id_info.get("email_verified", False):
        return _callback_redirect(redirect_to, {"status": "error", "code": "email_unverified"})

    # Corporate gate — only company Workspace accounts may connect.
    if not settings.email_domain_allowed(email):
        logger.info("Rejected non-corporate Google account: %s", email)
        return _callback_redirect(
            redirect_to,
            {"status": "error", "code": "domain_not_allowed", "email": email},
        )

    granted = list(getattr(credentials, "scopes", None) or google_scopes())
    expiry = credentials.expiry
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    try:
        cred = await upsert_credential(
            db,
            google_sub=id_info["sub"],
            email=email,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            expiry=expiry,
            scopes=granted,
            sabah_user_id=state_data.get("sabah_user_id"),
        )
    except GoogleAuthError as exc:
        logger.warning("Credential store failed for %s: %s", email, exc)
        return _callback_redirect(redirect_to, {"status": "error", "code": "no_refresh_token"})

    jwt_token = create_access_token(
        {"sub": cred.user_id, "email": email, "sabah_user_id": cred.sabah_user_id}
    )
    logger.info("Google account connected: %s (services=%s)", email, cred.services)

    return _callback_redirect(
        redirect_to,
        {
            "status": "success",
            "token": jwt_token,
            "email": email,
            "services": ",".join(cred.services or []),
        },
    )


class ConnectionStatus(BaseModel):
    connected: bool
    email: Optional[str] = None
    services: list = []
    scopes: list = []
    token_expiry: Optional[str] = None


@router.get("/google/status", response_model=ConnectionStatus)
async def google_status(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Whether the caller's Google account is connected, and what it unlocks."""
    cred = await get_credential(db, user_id=current_user["sub"])
    if not cred:
        return ConnectionStatus(connected=False)
    return ConnectionStatus(
        connected=True,
        email=cred.google_account_email,
        services=cred.services or services_from_scopes(cred.scopes or []),
        scopes=cred.scopes or [],
        token_expiry=cred.token_expiry.isoformat() if cred.token_expiry else None,
    )


@router.post("/google/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def google_disconnect(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke the Google grant and mark the stored credential unusable."""
    cred = await get_credential(db, user_id=current_user["sub"])
    if cred:
        await revoke_credential(cred, db)
    return None


@router.get("/google/services")
async def google_services():
    """Services this deployment requests on the consent screen."""
    return {
        "services": describe_services(settings.GOOGLE_SERVICES),
        "scopes": google_scopes(),
        "allowed_domains": settings.ALLOWED_EMAIL_DOMAINS,
    }
