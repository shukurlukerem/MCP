import asyncio
import logging
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import select

from app.core.config import settings
from app.core.db import async_session_factory
from app.core.google_client import GoogleAuthError, get_credential
from app.mcp.client import build_mcp_tools_config
from app.models.automation_run import AutomationRun
from app.models.mcp_server import MCPServer

logger = logging.getLogger(__name__)

# The LLM is an optional add-on, not a hard dependency. Everything else this
# service does — Google OAuth, the direct /google read endpoints, the MCP server
# it exposes — works without it, so a missing `openai` package must not stop the
# app (or the worker) from booting.
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - depends on which extras are installed
    OpenAI = None

LLM_DISABLED_MESSAGE = (
    "LLM automation is disabled on this deployment. To enable it, install the "
    "optional `openai` package and set OPENAI_API_KEY. The direct /google "
    "endpoints and the MCP server work without it."
)


def llm_available() -> bool:
    """True when an automation run can actually be executed."""
    return OpenAI is not None and bool(settings.OPENAI_API_KEY)


SYSTEM_PROMPT = (
    "You are the SABAH.OS workspace assistant. You operate on the signed-in "
    "employee's own corporate Google Workspace account through the MCP tools "
    "provided. Use the tools to look up real data rather than guessing, and cite "
    "concrete records (subjects, file names, event titles) in your answer."
)


class PermanentTaskError(Exception):
    """A failure that retrying cannot fix (revoked grant, deleted run, bad config)."""


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def execute_mcp_task(self, run_id: int, user_id: str) -> dict:
    """
    Celery task (sync) wrapping the async LLM+MCP orchestration loop.
    Each invocation gets its own event loop via asyncio.run().
    """
    try:
        return asyncio.run(_execute_async(run_id, user_id))
    except PermanentTaskError as exc:
        # No retry: the same input will fail identically every time.
        asyncio.run(_mark_run_error(run_id, str(exc)))
        logger.error("Run %s failed permanently: %s", run_id, exc)
        return {"run_id": run_id, "status": "error", "error": str(exc)}
    except Exception as exc:
        asyncio.run(_mark_run_error(run_id, str(exc)))
        raise self.retry(exc=exc)


async def _execute_async(run_id: int, user_id: str) -> dict:
    async with async_session_factory() as session:
        run = await session.get(AutomationRun, run_id)
        if not run:
            raise PermanentTaskError(f"AutomationRun {run_id} not found")

        # Re-checked here and not only at the API: a run queued while the LLM was
        # configured can be picked up after the key was removed.
        if not llm_available():
            raise PermanentTaskError(LLM_DISABLED_MESSAGE)

        servers = await _resolve_servers(session, run)
        if not servers:
            raise PermanentTaskError("No enabled MCP server matches this run")

        credential = await get_credential(session, user_id=user_id)
        needs_oauth = any(s.auth_type == "oauth" for s in servers)
        if needs_oauth and not credential:
            raise PermanentTaskError(
                "No connected Google account for this user — reconnect required"
            )

        try:
            mcp_tools = await build_mcp_tools_config(credential, servers, session)
        except GoogleAuthError as exc:
            raise PermanentTaskError(str(exc)) from exc

        if not mcp_tools:
            raise PermanentTaskError("No usable MCP tools could be built for this run")

        instructions = (run.input_payload or {}).get("instructions", "")
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            instructions=SYSTEM_PROMPT,
            input=instructions,
            tools=mcp_tools,
            max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        )

        run.output_payload = _extract_output(response)
        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()

        logger.info("Run %s finished over servers %s", run_id, [s.name for s in servers])
        return {"run_id": run_id, "status": "success"}


async def _resolve_servers(session, run: AutomationRun) -> list:
    """A run targets either one registered server or a named set of them."""
    if run.server_names:
        result = await session.execute(
            select(MCPServer).where(
                MCPServer.name.in_(run.server_names),
                MCPServer.enabled.is_(True),
            )
        )
        return list(result.scalars().all())

    if run.mcp_server_id:
        server = await session.get(MCPServer, run.mcp_server_id)
        return [server] if server and server.enabled else []

    return []


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
        if getattr(item, "type", None) == "mcp_call":
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
