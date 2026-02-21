"""FastAPI application entry point for mithril-proxy."""

from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()
from fastapi.responses import JSONResponse

from .bridge import init_bridge, shutdown_all_stdio, validate_stdio_commands
from .config import get_stdio_destinations, load_config
from .logger import setup_logging
from .proxy import handle_message, handle_sse, handle_streamable_http_get, handle_streamable_http_post
from .secrets import load_secrets


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup order matters:
    # 1. load_config  — required by validate_stdio_commands
    # 2. load_secrets — required by subprocess env injection; must precede validation
    # 3. setup_logging — needs config for log path
    # 4. init_bridge  — creates asyncio.Lock inside the running event loop
    # 5. validate     — fail-fast executable check with secrets available
    load_config()
    load_secrets()
    setup_logging()
    init_bridge()
    validate_stdio_commands(get_stdio_destinations())
    yield
    # Shutdown: terminate all managed stdio subprocesses
    await shutdown_all_stdio()


app = FastAPI(title="mithril-proxy", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/{destination}/sse")
async def sse_endpoint(destination: str, request: Request):
    return await handle_sse(request, destination)


@app.post("/{destination}/message")
async def message_endpoint(destination: str, request: Request):
    return await handle_message(request, destination)


@app.post("/{destination}/mcp")
async def mcp_post_endpoint(destination: str, request: Request):
    return await handle_streamable_http_post(request, destination)


@app.get("/{destination}/mcp")
async def mcp_get_endpoint(destination: str, request: Request):
    return await handle_streamable_http_get(request, destination)
