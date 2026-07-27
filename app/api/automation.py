from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.automation_run import AutomationRun
from app.models.mcp_server import MCPServer
from app.workers.tasks import LLM_DISABLED_MESSAGE, execute_mcp_task, llm_available

router = APIRouter(prefix="/automation", tags=["automation"])


class RunRequest(BaseModel):
    instructions: str
    # Target servers by name (e.g. ["gmail", "drive"]). Empty = every enabled
    # server, which is the usual corporate case: "search my whole workspace".
    servers: List[str] = Field(default_factory=list)
    # Legacy single-server form — still accepted.
    mcp_server_id: Optional[int] = None
    tool_name: str = "assistant"
    input_payload: dict = {}


class RunResponse(BaseModel):
    run_id: int
    status: str


class RunStatusResponse(BaseModel):
    run_id: int
    status: str
    tool_name: str
    servers: List[str] = []
    output_payload: Optional[dict]
    error_message: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]


def _to_response(run: AutomationRun) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=run.id,
        status=run.status,
        tool_name=run.tool_name,
        servers=run.server_names or [],
        output_payload=run.output_payload,
        error_message=run.error_message,
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )


@router.get("/servers")
async def list_servers(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """MCP servers this deployment can drive."""
    result = await db.execute(
        select(MCPServer).where(MCPServer.enabled.is_(True)).order_by(MCPServer.name)
    )
    return {
        "servers": [
            {
                "id": s.id,
                "name": s.name,
                "provider": s.provider,
                "service": s.service,
                "description": s.description,
            }
            for s in result.scalars().all()
        ]
    }


@router.post("/run", status_code=status.HTTP_202_ACCEPTED, response_model=RunResponse)
async def trigger_run(
    request: RunRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Enqueue an LLM+MCP automation run.
    Returns immediately with run_id; poll /run/{id} for status.

    Requires an LLM to be configured. Without one, reject here rather than
    persisting a run that the worker could only ever fail.
    """
    if not llm_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=LLM_DISABLED_MESSAGE,
        )

    query = select(MCPServer).where(MCPServer.enabled.is_(True))
    if request.mcp_server_id is not None:
        query = query.where(MCPServer.id == request.mcp_server_id)
    elif request.servers:
        query = query.where(MCPServer.name.in_(request.servers))

    servers = (await db.execute(query)).scalars().all()
    if not servers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No enabled MCP server matches the request",
        )

    payload = dict(request.input_payload)
    payload["instructions"] = request.instructions

    run = AutomationRun(
        user_id=current_user["sub"],
        sabah_user_id=current_user.get("sabah_user_id"),
        mcp_server_id=servers[0].id if len(servers) == 1 else None,
        server_names=[s.name for s in servers],
        tool_name=request.tool_name,
        input_payload=payload,
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Dispatch Celery task — pass only JSON-serializable primitives
    execute_mcp_task.delay(run.id, current_user["sub"])

    return RunResponse(run_id=run.id, status="pending")


@router.get("/run/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll the status of an automation run. Users can only see their own runs."""
    run = await db.get(AutomationRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.user_id != current_user["sub"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return _to_response(run)


@router.get("/runs", response_model=list[RunStatusResponse])
async def list_runs(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List recent automation runs for the current user."""
    result = await db.execute(
        select(AutomationRun)
        .where(AutomationRun.user_id == current_user["sub"])
        .order_by(AutomationRun.started_at.desc())
        .limit(limit)
    )
    return [_to_response(r) for r in result.scalars().all()]
