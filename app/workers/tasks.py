import asyncio
import json
from datetime import datetime, timezone

from celery import shared_task
from openai import OpenAI
from sqlalchemy import select

from app.core.config import settings
from app.core.db import async_session_factory
from app.mcp.client import build_mcp_tools_config, refresh_google_token_if_needed
from app.models.automation_run import AutomationRun
from app.models.credential import GoogleCredential
from app.models.mcp_server import MCPServer


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def execute_mcp_task(self, run_id: int, user_id: str) -> dict:
    """
    Celery task (sync) that wraps the async LLM+MCP orchestration loop.
    Each invocation gets its own event loop via asyncio.run().
    """
    try:
        return asyncio.run(_execute_async(run_id, user_id))
    except Exception as exc:
        asyncio.run(_mark_run_error(run_id, str(exc)))
        raise self.retry(exc=exc)


async def _execute_async(run_id: int, user_id: str) -> dict:
    async with async_session_factory() as session:
        # Load run record
        run = await session.get(AutomationRun, run_id)
        if not run:
            raise ValueError(f"AutomationRun {run_id} not found")

        mcp_server = await session.get(MCPServer, run.mcp_server_id)
        if not mcp_server or not mcp_server.enabled:
            raise ValueError(f"MCPServer {run.mcp_server_id} not found or disabled")

        # Load user Google credential (may be absent for non-OAuth servers)
        cred_result = await session.execute(
            select(GoogleCredential).where(GoogleCredential.user_id == user_id)
        )
        credential = cred_result.scalars().first()

        # Refresh token if credential exists
        if credential:
            credential = await refresh_google_token_if_needed(credential, session)

        # Build the MCP tools config for the OpenAI Responses API
        mcp_tools = await build_mcp_tools_config(credential, [mcp_server])

        # Call OpenAI with remote MCP servers as tools
        instructions = run.input_payload.get("instructions", "")
        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            input=instructions,
            tools=mcp_tools if mcp_tools else [],
            max_output_tokens=4096,
        )

        # Extract text output from response
        output = _extract_output(response)

        # Update run to success
        run.output_payload = output
        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()

        return {"run_id": run_id, "status": "success"}


async def _mark_run_error(run_id: int, error_message: str) -> None:
    async with async_session_factory() as session:
        run = await session.get(AutomationRun, run_id)
        if run:
            run.status = "error"
            run.error_message = error_message[:4000]  # guard against oversized traces
            run.finished_at = datetime.now(timezone.utc)
            await session.commit()


def _extract_output(response) -> dict:
    """Extract text and MCP tool calls from an OpenAI Responses API result."""
    text = getattr(response, "output_text", "") or ""
    tool_calls = []

    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "mcp_call":
            tool_calls.append(
                {
                    "tool": getattr(item, "name", None),
                    "server": getattr(item, "server_label", None),
                    "arguments": getattr(item, "arguments", None),
                    "output": getattr(item, "output", None),
                    "error": getattr(item, "error", None),
                }
            )

    usage = getattr(response, "usage", None)
    return {
        "text": text,
        "tool_calls": tool_calls,
        "status": getattr(response, "status", None),
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
            "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        },
    }
