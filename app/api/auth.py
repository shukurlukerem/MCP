"""
Google OAuth — "Continue with Google" for corporate accounts.

The consent screen asks for the full Workspace scope set in one pass, so a user
connects once and every service (Gmail, Drive, Calendar, Docs, Sheets, …) becomes
readable by the automation layer. Only accounts on the configured corporate
domain(s) are accepted.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

# Google routinely returns the granted scopes in a different order (and adds
# `openid`/`https://www.googleapis.com/auth/userinfo.*` when `include_granted_scopes`
# is on). By default oauthlib treats any such difference as an error and raises
# `Warning("Scope has changed from ... to ...")` from inside fetch_token, which
# surfaces here as a generic "token_exchange_failed". Relaxing the check lets the
# exchange succeed; we still record whatever scopes Google actually granted.
# Must be set before oauthlib runs the token exchange (read from os.environ at
# fetch time — pydantic settings/.env do NOT populate os.environ, so set it here).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow
from oauthlib.oauth2 import OAuth2Error
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.google_client import get_credential, revoke_credential
from app.core.google_scopes import describe_services, scopes_for, services_from_scopes
from app.core.security import (
    create_oauth_state,
    get_current_user,
    verify_oauth_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _mask(value: Optional[str]) -> str:
    """Redact a credential for logging — keep only enough to identify it."""
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


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
    next_path: Optional[str] = None,
) -> dict:
    """
    Build the Google consent URL plus a signed state.

    ``sabah_user_id`` is carried through the state so the callback can link the
    Google account to the SABAH.OS user without a server-side session.
    ``next_path`` is the in-app path the SPA should land on after sign-in; it is
    threaded through the state and handed back on the final redirect.
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
            "next": next_path or "/",
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
    next: Optional[str] = Query(
        default=None,
        description="In-app path the SPA should land on after sign-in",
    ),
    redirect_to: Optional[str] = Query(
        default=None,
        description="Where to send the browser after the callback completes",
    ),
):
    """Return the Google consent URL (for clients that redirect themselves)."""
    return build_authorization_url(redirect_to=redirect_to, next_path=next)


@router.get("/google/login")
async def google_login(
    next: Optional[str] = Query(default=None),
    redirect_to: Optional[str] = Query(default=None),
):
    """Begin the Google OAuth flow by redirecting to the consent screen."""
    return RedirectResponse(
        url=build_authorization_url(redirect_to=redirect_to, next_path=next)["authorization_url"]
    )


def _callback_redirect(target: str, params: dict) -> RedirectResponse:
    # Drop None values so urlencode never emits "code=None".
    clean = {k: v for k, v in params.items() if v is not None}
    separator = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{separator}{urlencode(clean)}")


async def _complete_via_sabah(claims: dict, tokens: dict, *, next_path: str, link_user_id) -> dict:
    """
    Hand the Google-verified identity to SABAH.OS (Django) to resolve the user and
    mint the one-time login code. Django owns the user model; this service only
    brokers Google. Returns the redirect params to forward to the SPA.
    """
    if not settings.INTERNAL_API_KEY:
        logger.error("Google callback: INTERNAL_API_KEY unset — cannot reach SABAH.OS")
        return {"status": "error", "code": "sabah_not_configured", "next": next_path}

    url = f"{settings.SABAH_API_BASE_URL.rstrip('/')}/api/v1/auth/google/internal/complete/"
    try:
        async with httpx.AsyncClient(timeout=settings.SABAH_API_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={"X-Internal-Key": settings.INTERNAL_API_KEY},
                json={
                    "claims": claims,
                    "tokens": tokens,
                    "next": next_path,
                    "link_user_id": link_user_id,
                },
            )
    except httpx.HTTPError as exc:
        # str(exc) is empty for timeouts, which reads as a bare "unreachable".
        # Log the exception TYPE + repr + target so the real cause is unambiguous:
        # ConnectError/ConnectTimeout = Django not listening / bad host; ReadTimeout
        # = Django reachable but slower than SABAH_API_TIMEOUT. The X-Internal-Key
        # header is never logged.
        logger.warning(
            "Google callback: SABAH.OS request failed [%s]: %r (url=%s, timeout=%ss)",
            type(exc).__name__,
            exc,
            url,
            settings.SABAH_API_TIMEOUT,
        )
        return {"status": "error", "code": "sabah_unreachable", "next": next_path}

    if resp.status_code != 200:
        logger.warning("Google callback: SABAH.OS returned %s: %s", resp.status_code, resp.text[:200])
        return {"status": "error", "code": "sabah_error", "next": next_path}
    try:
        return resp.json()
    except ValueError:
        return {"status": "error", "code": "sabah_bad_response", "next": next_path}


@router.get("/google/callback")
async def google_callback(
    code: Optional[str] = Query(default=None),
    state: str = Query(...),
    error: Optional[str] = Query(default=None),
):
    """
    OAuth callback: exchange the code and verify the Google identity, then delegate
    to SABAH.OS to resolve the user and mint the session code. This service holds
    the Google credentials; Django holds the user model.
    """
    state_data = verify_oauth_state(state)
    redirect_to = state_data.get("redirect_to") or settings.FRONTEND_REDIRECT_URL
    next_path = state_data.get("next") or "/"

    if error or not code:
        return _callback_redirect(
            redirect_to, {"status": "error", "code": error or "missing_code", "next": next_path}
        )

    try:
        flow = _build_flow(state=state)
        flow.fetch_token(code=code)
    except OAuth2Error as exc:
        # oauthlib parsed Google's error body — this is the precise reason
        # (invalid_grant, redirect_uri_mismatch, invalid_client, …). None of
        # error/description contains a secret or the authorization code.
        logger.warning(
            "Google token exchange failed: error=%s description=%s "
            "(redirect_uri=%s, client_id=%s)",
            getattr(exc, "error", "unknown"),
            getattr(exc, "description", str(exc)),
            settings.GOOGLE_REDIRECT_URI,
            _mask(settings.GOOGLE_CLIENT_ID),
        )
        return _callback_redirect(
            redirect_to, {"status": "error", "code": "token_exchange_failed", "next": next_path}
        )
    except Exception as exc:
        # Anything oauthlib did not classify — e.g. a "Scope has changed" Warning
        # (handled by OAUTHLIB_RELAX_TOKEN_SCOPE above) or a network error. Log the
        # type + message so the real cause is visible; never log code/secrets.
        logger.warning(
            "Google token exchange failed (unexpected %s): %s "
            "(redirect_uri=%s, client_id=%s)",
            type(exc).__name__,
            exc,
            settings.GOOGLE_REDIRECT_URI,
            _mask(settings.GOOGLE_CLIENT_ID),
        )
        return _callback_redirect(
            redirect_to, {"status": "error", "code": "token_exchange_failed", "next": next_path}
        )

    credentials = flow.credentials
    try:
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except Exception as exc:
        logger.warning("Google ID token verification failed: %s", exc)
        return _callback_redirect(
            redirect_to, {"status": "error", "code": "invalid_id_token", "next": next_path}
        )

    expiry = credentials.expiry
    expires_in = None
    if expiry:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        expires_in = max(0, int((expiry - datetime.now(timezone.utc)).total_seconds()))

    claims = {
        "sub": id_info.get("sub"),
        "email": (id_info.get("email") or "").lower(),
        "email_verified": bool(id_info.get("email_verified", False)),
        "name": id_info.get("name", "") or "",
        "picture": id_info.get("picture", "") or "",
    }
    tokens = {
        "access_token": credentials.token or "",
        "refresh_token": credentials.refresh_token or "",
        "expires_in": expires_in,
        "scope": " ".join(getattr(credentials, "scopes", None) or google_scopes()),
        "id_token": credentials.id_token,
    }

    params = await _complete_via_sabah(
        claims, tokens, next_path=next_path, link_user_id=state_data.get("sabah_user_id")
    )
    return _callback_redirect(redirect_to, params)


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
