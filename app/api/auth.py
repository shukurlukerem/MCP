import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import create_access_token, encrypt_token
from app.models.credential import GoogleCredential

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.events",
]

_client_config = {
    "web": {
        "client_id": None,
        "client_secret": None,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [],
    }
}


def _build_flow() -> Flow:
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
        scopes=GOOGLE_SCOPES,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
    )


@router.get("/google/login")
async def google_login():
    """Generate Google OAuth authorization URL and redirect the user."""
    flow = _build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return RedirectResponse(url=auth_url)


@router.get("/google/callback")
async def google_callback(
    code: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange the OAuth code for tokens, store encrypted credentials,
    and return an internal JWT.
    """
    try:
        flow = _build_flow()
        flow.fetch_token(code=code)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Token exchange failed: {exc}",
        )

    google_token = flow.credentials.token
    google_refresh = flow.credentials.refresh_token
    expiry = flow.credentials.expiry

    # Verify ID token to get user info
    try:
        id_info = id_token.verify_oauth2_token(
            flow.credentials.id_token,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"ID token verification failed: {exc}",
        )

    user_id = id_info["sub"]
    email = id_info["email"]

    # Upsert GoogleCredential
    result = await db.execute(
        select(GoogleCredential).where(GoogleCredential.user_id == user_id)
    )
    cred = result.scalars().first()

    if cred is None:
        cred = GoogleCredential(user_id=user_id, google_account_email=email)
        db.add(cred)

    cred.access_token = encrypt_token(google_token)
    if google_refresh:
        cred.refresh_token = encrypt_token(google_refresh)
    if expiry:
        cred.token_expiry = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry
    cred.scopes = GOOGLE_SCOPES

    await db.commit()

    jwt_token = create_access_token({"sub": user_id, "email": email})
    return {"access_token": jwt_token, "token_type": "bearer"}
