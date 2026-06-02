from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.automation_run import AutomationRun
from app.models.mcp_server import MCPServer
from app.workers.tasks import _execute_async, _mark_run_error


@pytest_asyncio.fixture
async def seeded_server(db_session):
    server = MCPServer(
        name="test-server",
        transport="http",
        url="http://example.com/mcp",
        auth_type="none",
        enabled=True,
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    return server


@pytest_asyncio.fixture
async def pending_run(db_session, seeded_server):
    run = AutomationRun(
        user_id="test-user-123",
        mcp_server_id=seeded_server.id,
        tool_name="search_emails",
        input_payload={"instructions": "Find latest emails"},
        status="pending",
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


@pytest.mark.asyncio
async def test_automation_run_created_on_trigger(client, auth_headers, seeded_server):
    resp = await client.post(
        "/automation/run",
        json={
            "mcp_server_id": seeded_server.id,
            "tool_name": "search_emails",
            "input_payload": {},
            "instructions": "Search inbox",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_automation_run_initial_status_is_pending(client, auth_headers, seeded_server):
    resp = await client.post(
        "/automation/run",
        json={
            "mcp_server_id": seeded_server.id,
            "tool_name": "search_emails",
            "input_payload": {},
            "instructions": "Search inbox",
        },
        headers=auth_headers,
    )
    run_id = resp.json()["run_id"]
    status_resp = await client.get(f"/automation/run/{run_id}", headers=auth_headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_automation_run_user_isolation(client, db_session, seeded_server):
    from app.core.security import create_access_token

    # Create a run as user-A
    token_a = create_access_token({"sub": "user-A", "email": "a@test.com"})
    headers_a = {"Authorization": f"Bearer {token_a}"}
    resp = await client.post(
        "/automation/run",
        json={
            "mcp_server_id": seeded_server.id,
            "tool_name": "search_emails",
            "input_payload": {},
            "instructions": "Test",
        },
        headers=headers_a,
    )
    run_id = resp.json()["run_id"]

    # User-B should not be able to access user-A's run
    token_b = create_access_token({"sub": "user-B", "email": "b@test.com"})
    headers_b = {"Authorization": f"Bearer {token_b}"}
    forbidden_resp = await client.get(f"/automation/run/{run_id}", headers=headers_b)
    assert forbidden_resp.status_code == 403


@pytest.mark.asyncio
async def test_celery_task_updates_run_to_success(db_session, pending_run):
    mock_response = MagicMock()
    mock_response.output_text = "Found 5 emails"
    mock_response.output = []
    mock_response.status = "completed"
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    with patch("app.workers.tasks.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_client.responses.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch("app.workers.tasks.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await _execute_async(pending_run.id, "test-user-123")

    await db_session.refresh(pending_run)
    assert pending_run.status == "success"
    assert pending_run.output_payload is not None
    assert "text" in pending_run.output_payload


@pytest.mark.asyncio
async def test_celery_task_sets_finished_at(db_session, pending_run):
    mock_response = MagicMock()
    mock_response.output_text = "Done"
    mock_response.output = []
    mock_response.status = "completed"
    mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

    with patch("app.workers.tasks.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_client.responses.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch("app.workers.tasks.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await _execute_async(pending_run.id, "test-user-123")

    await db_session.refresh(pending_run)
    assert pending_run.finished_at is not None


@pytest.mark.asyncio
async def test_mark_run_error(db_session, pending_run):
    with patch("app.workers.tasks.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        await _mark_run_error(pending_run.id, "Something went wrong")

    await db_session.refresh(pending_run)
    assert pending_run.status == "error"
    assert pending_run.error_message == "Something went wrong"
    assert pending_run.finished_at is not None
