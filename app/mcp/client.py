"""
MCP client side: which remote MCP servers exist, and how a user's credential is
attached to them when the LLM is handed the tool list.
"""

import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.google_client import access_token_for, refresh_if_needed  # noqa: F401
from app.core.google_scopes import SERVICES
from app.models.credential import GoogleCredential
from app.models.mcp_server import MCPServer

logger = logging.getLogger(__name__)

# Google Workspace remote MCP servers, keyed by service.
GOOGLE_MCP_URLS = {
    key: service.mcp_url for key, service in SERVICES.items() if service.mcp_url
}


def get_google_mcp_server_url(service: str) -> Optional[str]:
    """Return the MCP server URL for a given Google Workspace service."""
    return GOOGLE_MCP_URLS.get(service.lower())


def google_server_seed() -> List[dict]:
    """Rows the registry is seeded with on first boot."""
    return [
        {
            "name": key,
            "transport": "http",
            "url": service.mcp_url,
            "auth_type": "oauth",
            "provider": "google",
            "service": key,
            "enabled": True,
            "description": f"{service.label} (Google Workspace MCP server)",
            "config": {},
        }
        for key, service in SERVICES.items()
        if service.mcp_url
    ]


async def ensure_google_servers(session: AsyncSession) -> int:
    """
    Idempotently register the Google MCP servers.

    Runs on startup so a fresh deployment is usable without hand-written SQL.
    Existing rows are left alone — an operator who disables a server or repoints
    its URL should not have the next restart undo it.
    """
    existing = set((await session.execute(select(MCPServer.name))).scalars().all())
    created = 0
    for row in google_server_seed():
        if row["name"] in existing:
            continue
        session.add(MCPServer(**row))
        created += 1
    if created:
        await session.commit()
        logger.info("Registered %d Google MCP servers", created)
    return created


async def build_mcp_tools_config(
    credential: Optional[GoogleCredential],
    server_records: List[MCPServer],
    session: Optional[AsyncSession] = None,
) -> List[dict]:
    """
    Build the ``tools`` array (type="mcp") passed to the OpenAI Responses API.

    OAuth servers get a live Bearer token: the token is refreshed first, because
    a run can start minutes after it was queued and an expired token would fail
    every tool call in the conversation.
    """
    tools: List[dict] = []
    bearer: Optional[str] = None

    for srv in server_records:
        if not srv.enabled or not srv.url:
            continue

        tool: dict = {
            "type": "mcp",
            "server_label": srv.name,
            "server_url": srv.url,
            "require_approval": "never",
        }

        if srv.auth_type == "oauth":
            if not credential or session is None:
                logger.warning(
                    "Skipping MCP server %s — no credential available for OAuth", srv.name
                )
                continue
            if bearer is None:
                bearer = await access_token_for(credential, session)
            tool["headers"] = {"Authorization": f"Bearer {bearer}"}

        tools.append(tool)

    return tools
