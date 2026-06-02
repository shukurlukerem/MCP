import json
from typing import Any

from fastapi import Depends, Request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.security import get_current_user
from app.models.automation_run import AutomationRun
from app.models.mcp_server import MCPServer

mcp_server = Server("internal-data-server")
sse_transport = SseServerTransport("/mcp/messages/")


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_automation_runs",
            description="List automation runs for a user, optionally filtered by status",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User ID to filter by"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "success", "error"],
                        "description": "Filter by run status",
                    },
                    "limit": {"type": "integer", "default": 20, "description": "Max results"},
                },
                "required": ["user_id"],
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
            description="Get full detail of a single automation run by ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "integer", "description": "AutomationRun ID"}
                },
                "required": ["run_id"],
            },
        ),
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    async with async_session_factory() as session:
        if name == "get_automation_runs":
            return await _get_automation_runs(session, arguments)
        elif name == "get_mcp_servers":
            return await _get_mcp_servers(session, arguments)
        elif name == "get_run_detail":
            return await _get_run_detail(session, arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _get_automation_runs(session: Any, args: dict) -> list[TextContent]:
    query = select(AutomationRun).where(AutomationRun.user_id == args["user_id"])
    if "status" in args:
        query = query.where(AutomationRun.status == args["status"])
    query = query.limit(args.get("limit", 20)).order_by(AutomationRun.started_at.desc())
    result = await session.execute(query)
    runs = result.scalars().all()
    data = [
        {
            "id": r.id,
            "tool_name": r.tool_name,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in runs
    ]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


async def _get_mcp_servers(session: Any, args: dict) -> list[TextContent]:
    query = select(MCPServer)
    if args.get("enabled_only", True):
        query = query.where(MCPServer.enabled.is_(True))
    result = await session.execute(query)
    servers = result.scalars().all()
    data = [
        {
            "id": s.id,
            "name": s.name,
            "transport": s.transport,
            "url": s.url,
            "auth_type": s.auth_type,
            "enabled": s.enabled,
        }
        for s in servers
    ]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


async def _get_run_detail(session: Any, args: dict) -> list[TextContent]:
    run = await session.get(AutomationRun, args["run_id"])
    if not run:
        return [TextContent(type="text", text=json.dumps({"error": "Run not found"}))]
    data = {
        "id": run.id,
        "user_id": run.user_id,
        "tool_name": run.tool_name,
        "status": run.status,
        "input_payload": run.input_payload,
        "output_payload": run.output_payload,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


# FastAPI route handlers registered in main.py

async def handle_sse(request: Request, current_user: dict = Depends(get_current_user)):
    """GET /mcp/ — SSE stream; JWT-authenticated before handshake."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )


async def handle_post_message(request: Request):
    """POST /mcp/messages/ — MCP message relay (no direct auth; session validated via SSE)."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)
