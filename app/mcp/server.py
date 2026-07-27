"""
This service in its *server* role: it exposes its own data — and, for the
signed-in employee, their whole Google Workspace — as MCP tools, so SABAH.OS
(or any other authorised agent) can query it over MCP instead of REST.
"""

import json
import logging
from contextvars import ContextVar
from typing import Any, Optional

from fastapi import Depends, Request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.google_client import GoogleAuthError, get_credential
from app.core.security import get_current_user
from app.models.automation_run import AutomationRun
from app.models.mcp_server import MCPServer
from app.services import google_data

logger = logging.getLogger(__name__)

mcp_server = Server("sabah-os-workspace-server")
sse_transport = SseServerTransport("/mcp/messages/")

# The MCP protocol has no per-call identity, so the JWT subject validated at the
# SSE handshake is carried in a context variable and every tool is scoped to it.
# Without this, one employee's session could read another's data by passing a
# different user_id argument.
current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_automation_runs",
            description="List your automation runs, optionally filtered by status",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "success", "error"],
                        "description": "Filter by run status",
                    },
                    "limit": {"type": "integer", "default": 20, "description": "Max results"},
                },
            },
        ),
        Tool(
            name="get_mcp_servers",
            description="List all registered MCP servers",
            inputSchema={
                "type": "object",
                "properties": {
                    "enabled_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "Return only enabled servers",
                    }
                },
            },
        ),
        Tool(
            name="get_run_detail",
            description="Get full detail of one of your automation runs by ID",
            inputSchema={
                "type": "object",
                "properties": {"run_id": {"type": "integer", "description": "AutomationRun ID"}},
                "required": ["run_id"],
            },
        ),
        Tool(
            name="google_workspace_snapshot",
            description=(
                "Overview of the signed-in employee's Google Workspace: recent mail, "
                "files, calendar events, task lists, contacts and chat spaces."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10, "description": "Items per service"}
                },
            },
        ),
        Tool(
            name="google_read",
            description=(
                "Read any Google Workspace resource for the signed-in employee. "
                "Resource names: " + ", ".join(sorted(google_data.RESOURCES)) + "."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "enum": sorted(google_data.RESOURCES),
                        "description": "Resource to read",
                    },
                    "params": {
                        "type": "object",
                        "description": "Reader arguments, e.g. {\"query\": \"newer_than:7d\", \"limit\": 20}",
                    },
                },
                "required": ["resource"],
            },
        ),
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    user_id = current_user_id.get()
    if not user_id:
        return [_json({"error": "No authenticated MCP session"})]

    async with async_session_factory() as session:
        try:
            if name == "get_automation_runs":
                return await _get_automation_runs(session, user_id, arguments)
            if name == "get_mcp_servers":
                return await _get_mcp_servers(session, arguments)
            if name == "get_run_detail":
                return await _get_run_detail(session, user_id, arguments)
            if name == "google_workspace_snapshot":
                return await _google_snapshot(session, user_id, arguments)
            if name == "google_read":
                return await _google_read(session, user_id, arguments)
        except GoogleAuthError as exc:
            return [_json({"error": str(exc)})]
        except Exception as exc:  # never leak a traceback into the model's context
            logger.exception("MCP tool %s failed", name)
            return [_json({"error": f"{type(exc).__name__}: {exc}"})]

    return [_json({"error": f"Unknown tool: {name}"})]


def _json(payload: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, indent=2, default=str))


async def _get_automation_runs(session: Any, user_id: str, args: dict) -> list[TextContent]:
    query = select(AutomationRun).where(AutomationRun.user_id == user_id)
    if args.get("status"):
        query = query.where(AutomationRun.status == args["status"])
    query = query.order_by(AutomationRun.started_at.desc()).limit(args.get("limit", 20))
    runs = (await session.execute(query)).scalars().all()
    return [
        _json(
            [
                {
                    "id": r.id,
                    "tool_name": r.tool_name,
                    "servers": r.server_names or [],
                    "status": r.status,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
                for r in runs
            ]
        )
    ]


async def _get_mcp_servers(session: Any, args: dict) -> list[TextContent]:
    query = select(MCPServer)
    if args.get("enabled_only", True):
        query = query.where(MCPServer.enabled.is_(True))
    servers = (await session.execute(query)).scalars().all()
    return [
        _json(
            [
                {
                    "id": s.id,
                    "name": s.name,
                    "provider": s.provider,
                    "service": s.service,
                    "transport": s.transport,
                    "url": s.url,
                    "auth_type": s.auth_type,
                    "enabled": s.enabled,
                }
                for s in servers
            ]
        )
    ]


async def _get_run_detail(session: Any, user_id: str, args: dict) -> list[TextContent]:
    run = await session.get(AutomationRun, args["run_id"])
    if not run or run.user_id != user_id:
        return [_json({"error": "Run not found"})]
    return [
        _json(
            {
                "id": run.id,
                "tool_name": run.tool_name,
                "servers": run.server_names or [],
                "status": run.status,
                "input_payload": run.input_payload,
                "output_payload": run.output_payload,
                "error_message": run.error_message,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
        )
    ]


async def _require_credential(session: Any, user_id: str):
    cred = await get_credential(session, user_id=user_id)
    if not cred:
        raise GoogleAuthError("No connected Google account for this session")
    return cred


async def _google_snapshot(session: Any, user_id: str, args: dict) -> list[TextContent]:
    cred = await _require_credential(session, user_id)
    data = await google_data.workspace_snapshot(cred, session, limit=args.get("limit", 10))
    return [_json(data)]


async def _google_read(session: Any, user_id: str, args: dict) -> list[TextContent]:
    cred = await _require_credential(session, user_id)
    data = await google_data.fetch(args["resource"], cred, session, args.get("params") or {})
    return [_json(data)]


# FastAPI route handlers registered in main.py

async def handle_sse(request: Request, current_user: dict = Depends(get_current_user)):
    """GET /mcp/ — SSE stream; JWT-authenticated before handshake."""
    token = current_user_id.set(current_user["sub"])
    try:
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )
    finally:
        current_user_id.reset(token)


async def handle_post_message(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    POST /mcp/messages/ — MCP message relay.

    Authenticated as well as the SSE stream: the relay is a public URL and the
    session id alone is not a credential.
    """
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)
