import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.api.auth import router as auth_router
from app.api.automation import router as automation_router
from app.api.google import router as google_router
from app.api.internal import router as internal_router
from app.mcp.client import ensure_google_servers
from app.mcp.server import handle_post_message, handle_sse

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: verify DB connectivity and seed the Google MCP server registry
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    async with async_session_factory() as session:
        await ensure_google_servers(session)
    logger.info(
        "MCP Automation Service started (env=%s, google_services=%s)",
        settings.ENVIRONMENT,
        ",".join(settings.GOOGLE_SERVICES),
    )
    yield
    # Shutdown: release all DB connections
    await engine.dispose()


app = FastAPI(
    title="MCP Automation Service",
    description=(
        "AI automation backend with an MCP server and corporate Google Workspace "
        "integration for SABAH.OS."
    ),
    version="2.0.0",
    lifespan=lifespan,
    root_path=settings.ROOT_PATH,
    # Schema endpoints stay off in production — they advertise the internal
    # surface of a service that holds live corporate OAuth tokens.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers
app.include_router(auth_router)
app.include_router(automation_router)
app.include_router(google_router)
app.include_router(internal_router)

# MCP SSE endpoints registered as FastAPI routes (not app.mount)
# so that Depends(get_current_user) is evaluated before the SSE handshake.
app.add_api_route("/mcp/", handle_sse, methods=["GET"], tags=["mcp"])
app.add_api_route("/mcp/messages/", handle_post_message, methods=["POST"], tags=["mcp"])


@app.get("/health", tags=["health"])
async def health():
    """Liveness — the process is up."""
    return {"status": "ok", "version": app.version}


@app.get("/ready", tags=["health"])
async def ready():
    """Readiness — dependencies the service cannot serve traffic without."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ready", "environment": settings.ENVIRONMENT}
