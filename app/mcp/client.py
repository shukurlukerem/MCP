from datetime import datetime, timedelta, timezone
from typing import Optional

from authlib.integrations.httpx_client import AsyncOAuth2Client
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_token, encrypt_token
from app.models.credential import GoogleCredential
from app.models.mcp_server import MCPServer

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Google Workspace MCP server base URLs
GOOGLE_MCP_URLS = {
    "gmail": "https://gmail.googleapis.com/mcp",
    "drive": "https://www.googleapis.com/drive/v3/mcp",
    "calendar": "https://www.googleapis.com/calendar/v3/mcp",
}


async def refresh_google_token_if_needed(
    credential: GoogleCredential,
    session: AsyncSession,
) -> GoogleCredential:
    """Refresh Google access token if it expires within 5 minutes."""
    if credential.token_expiry and credential.token_expiry > datetime.now(timezone.utc) + timedelta(minutes=5):
        return credential

    async with AsyncOAuth2Client(
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
    ) as client:
        token = await client.refresh_token(
            GOOGLE_TOKEN_URL,
            refresh_token=decrypt_token(credential.refresh_token),
        )

    credential.access_token = encrypt_token(token["access_token"])
    if "expires_at" in token:
        credential.token_expiry = datetime.fromtimestamp(token["expires_at"], tz=timezone.utc)
    elif "expires_in" in token:
        credential.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=token["expires_in"])

    session.add(credential)
    await session.commit()
    await session.refresh(credential)
    return credential


async def build_mcp_tools_config(
    credential: Optional[GoogleCredential],
    server_records: list[MCPServer],
) -> list[dict]:
    """
    Build the `tools` array (type="mcp") passed to the OpenAI Responses API.
    Decrypts and attaches OAuth access tokens as a Bearer header per server.
    """
    tools = []
    for srv in server_records:
        if not srv.enabled:
            continue

        tool: dict = {
            "type": "mcp",
            "server_label": srv.name,
            "server_url": srv.url,
            "require_approval": "never",
        }

        if srv.auth_type == "oauth" and credential:
            token = decrypt_token(credential.access_token)
            tool["headers"] = {"Authorization": f"Bearer {token}"}

        tools.append(tool)

    return tools


def get_google_mcp_server_url(service: str) -> Optional[str]:
    """Return the MCP server URL for a given Google Workspace service."""
    return GOOGLE_MCP_URLS.get(service.lower())
