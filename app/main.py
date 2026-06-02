from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.db import engine
from app.api.auth import router as auth_router
from app.api.automation import router as automation_router
from app.mcp.server import handle_post_message, handle_sse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: verify DB connectivity
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: None)
    yield
    # Shutdown: release all DB connections
    await engine.dispose()


app = FastAPI(
    title="MCP Automation Service",
    description="AI automation backend with MCP server and Google Workspace integration",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
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

# MCP SSE endpoints registered as FastAPI routes (not app.mount)
# so that Depends(get_current_user) is evaluated before the SSE handshake.
app.add_api_route("/mcp/", handle_sse, methods=["GET"], tags=["mcp"])
app.add_api_route("/mcp/messages/", handle_post_message, methods=["POST"], tags=["mcp"])


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}
