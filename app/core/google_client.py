"""
Google credential handling shared by the API, the internal service API and the
Celery worker: upsert, refresh, and authorised HTTP access to any Google API.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.google_scopes import ALLOWED_GOOGLE_HOSTS, services_from_scopes
from app.core.security import decrypt_token, encrypt_token
from app.models.credential import GoogleCredential

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Refresh a little before expiry so a long request never runs out mid-flight.
REFRESH_MARGIN = timedelta(minutes=5)


class GoogleAuthError(Exception):
    """Raised when a credential cannot be used or refreshed."""


async def get_credential(
    session: AsyncSession,
    *,
    user_id: Optional[str] = None,
    sabah_user_id: Optional[str] = None,
    email: Optional[str] = None,
) -> Optional[GoogleCredential]:
    """Look a credential up by any of the identifiers Django or the API holds."""
    query = select(GoogleCredential).where(GoogleCredential.revoked.is_(False))
    if user_id:
        query = query.where(GoogleCredential.user_id == user_id)
    elif sabah_user_id:
        query = query.where(GoogleCredential.sabah_user_id == str(sabah_user_id))
    elif email:
        query = query.where(GoogleCredential.google_account_email == email.lower())
    else:
        return None
    result = await session.execute(query)
    return result.scalars().first()


async def upsert_credential(
    session: AsyncSession,
    *,
    google_sub: str,
    email: str,
    access_token: str,
    refresh_token: Optional[str],
    expiry: Optional[datetime],
    scopes: list,
    sabah_user_id: Optional[str] = None,
) -> GoogleCredential:
    """
    Create or update the stored credential. Tokens are encrypted before they
    touch the database.

    Google only returns a refresh token on the first consent, so an absent
    ``refresh_token`` keeps the previously stored one instead of wiping it.
    """
    email = email.lower()
    result = await session.execute(
        select(GoogleCredential).where(GoogleCredential.user_id == google_sub)
    )
    cred = result.scalars().first()

    if cred is None:
        if not refresh_token:
            raise GoogleAuthError(
                "Google did not return a refresh token. Re-consent with "
                "access_type=offline&prompt=consent."
            )
        cred = GoogleCredential(user_id=google_sub)
        session.add(cred)

    cred.google_account_email = email
    cred.domain = email.rsplit("@", 1)[-1]
    cred.access_token = encrypt_token(access_token)
    if refresh_token:
        cred.refresh_token = encrypt_token(refresh_token)
    if expiry:
        cred.token_expiry = (
            expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry
        )
    cred.scopes = scopes
    cred.services = services_from_scopes(scopes)
    cred.revoked = False
    if sabah_user_id is not None:
        cred.sabah_user_id = str(sabah_user_id)

    await session.commit()
    await session.refresh(cred)
    return cred


async def refresh_if_needed(
    credential: GoogleCredential,
    session: AsyncSession,
) -> GoogleCredential:
    """Refresh the access token when it is expired or about to expire."""
    if (
        credential.token_expiry
        and credential.token_expiry > datetime.now(timezone.utc) + REFRESH_MARGIN
    ):
        return credential

    refresh_token = decrypt_token(credential.refresh_token)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    if response.status_code != 200:
        # invalid_grant means the user revoked access or changed their password;
        # the stored credential is dead and the user must reconnect.
        body = response.text[:500]
        if "invalid_grant" in body:
            credential.revoked = True
            await session.commit()
            raise GoogleAuthError(
                "Google access was revoked for this account — reconnect required"
            )
        raise GoogleAuthError(f"Token refresh failed ({response.status_code}): {body}")

    token = response.json()
    credential.access_token = encrypt_token(token["access_token"])
    if "expires_in" in token:
        credential.token_expiry = datetime.now(timezone.utc) + timedelta(
            seconds=int(token["expires_in"])
        )
    if token.get("scope"):
        credential.scopes = token["scope"].split()
        credential.services = services_from_scopes(credential.scopes)

    await session.commit()
    await session.refresh(credential)
    logger.info("Refreshed Google token for %s", credential.google_account_email)
    return credential


async def access_token_for(
    credential: GoogleCredential,
    session: AsyncSession,
) -> str:
    """Return a currently valid plaintext access token, refreshing if needed."""
    credential = await refresh_if_needed(credential, session)
    credential.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    return decrypt_token(credential.access_token)


async def google_request(
    credential: GoogleCredential,
    session: AsyncSession,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: float = 30.0,
) -> Any:
    """
    Perform an authorised call against any Google API.

    The host allowlist is the safety boundary: this helper attaches a live
    corporate OAuth token, so it must never be pointed at an arbitrary URL.
    """
    host = urlparse(url).netloc
    if host not in ALLOWED_GOOGLE_HOSTS:
        raise GoogleAuthError(f"Host not allowed for Google passthrough: {host!r}")

    token = await access_token_for(credential, session)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method.upper(),
            url,
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
        )

    if response.status_code >= 400:
        raise GoogleAuthError(
            f"Google API {method.upper()} {url} failed "
            f"({response.status_code}): {response.text[:500]}"
        )

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


async def revoke_credential(credential: GoogleCredential, session: AsyncSession) -> None:
    """Revoke the refresh token at Google, then mark the row revoked locally."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                GOOGLE_REVOKE_URL,
                data={"token": decrypt_token(credential.refresh_token)},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:  # revocation is best-effort — local state still wins
        logger.warning("Google revoke call failed: %s", exc)

    credential.revoked = True
    await session.commit()
