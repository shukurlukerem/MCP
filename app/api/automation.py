from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.automation_run import AutomationRun
from app.models.mcp_server import MCPServer
from app.workers.tasks import execute_mcp_task

router = APIRouter(prefix="/automation", tags=["automation"])


class RunRequest(BaseModel):
    mcp_server_id: int
    tool_name: str
    input_payload: dict = {}
    instructions: str


class RunResponse(BaseModel):
    run_id: int
    status: str


class RunStatusResponse(BaseModel):
    run_id: int
    status: str
    tool_name: str
    output_payload: Optional[dict]
    error_message: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]


@router.post("/run", status_code=status.HTTP_202_ACCEPTED, response_model=RunResponse)
async def trigger_run(
    request: RunRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Enqueue an LLM+MCP automation run.
    Returns immediately with run_id; poll /run/{id} for status.
    """
    # Validate the MCP server exists and is enabled
    server = await db.get(MCPServer, request.mcp_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if not server.enabled:
        raise HTTPException(status_code=400, detail="MCP server is disabled")

    user_id = current_user["sub"]

    # Merge instructions into input_payload for the worker
    payload = dict(request.input_payload)
    payload["instructions"] = request.instructions

    run = AutomationRun(
        user_id=user_id,
        mcp_server_id=request.mcp_server_id,
        tool_name=request.tool_name,
        input_payload=payload,
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Dispatch Celery task — pass only JSON-serializable primitives
    execute_mcp_task.delay(run.id, user_id)

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

    return RunStatusResponse(
        run_id=run.id,
        status=run.status,
        tool_name=run.tool_name,
        output_payload=run.output_payload,
        error_message=run.error_message,
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )


@router.get("/runs", response_model=list[RunStatusResponse])
async def list_runs(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
):
    """List recent automation runs for the current user."""
    result = await db.execute(
        select(AutomationRun)
        .where(AutomationRun.user_id == current_user["sub"])
        .order_by(AutomationRun.started_at.desc())
        .limit(limit)
    )
    runs = result.scalars().all()
    return [
        RunStatusResponse(
            run_id=r.id,
            status=r.status,
            tool_name=r.tool_name,
            output_payload=r.output_payload,
            error_message=r.error_message,
            started_at=r.started_at.isoformat() if r.started_at else None,
            finished_at=r.finished_at.isoformat() if r.finished_at else None,
        )
        for r in runs
    ]
