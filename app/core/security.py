import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from app.core.config import settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)

_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(settings.FERNET_KEY.encode())
    return _fernet


class DecryptionError(Exception):
    pass


def encrypt_token(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError("Token decryption failed — invalid or tampered ciphertext") from exc


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    )
    payload["exp"] = expire
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    if not token:
        raise CREDENTIALS_EXCEPTION
    try:
        payload = decode_access_token(token)
    except JWTError:
        raise CREDENTIALS_EXCEPTION
    if payload.get("sub") is None:
        raise CREDENTIALS_EXCEPTION
    return payload


# ── Signed OAuth state ───────────────────────────────────────────────────────
# The state travels through Google and back, so it must be tamper-proof without
# any server-side session store (the API may be running behind several nodes).

def create_oauth_state(data: dict) -> str:
    return create_access_token(
        {**data, "purpose": "oauth_state"},
        expires_delta=timedelta(seconds=settings.OAUTH_STATE_TTL_SECONDS),
    )


def verify_oauth_state(state: str) -> dict:
    try:
        payload = decode_access_token(state)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state",
        ) from exc
    if payload.get("purpose") != "oauth_state":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state",
        )
    return payload


# ── Service-to-service auth (SABAH.OS Django → this service) ─────────────────

async def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    """
    Guard for /internal/* routes. Django holds the same secret; the comparison
    is constant-time so a wrong key leaks nothing through timing.
    """
    expected = settings.INTERNAL_API_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API is not configured on this server",
        )
    if not x_internal_key or not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key",
        )
