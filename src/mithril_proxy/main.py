"""FastAPI application entry point for mithril-proxy."""

from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()
from fastapi.responses import JSONResponse

from .bridge import init_bridge, shutdown_all_stdio, validate_stdio_commands
from .config import get_stdio_destinations, load_config
from .detector import init_detector, load_patterns, reload_patterns
from .logger import setup_logging
from .proxy import (
    handle_message,
    handle_sse,
    handle_streamable_http_delete,
    handle_streamable_http_get,
    handle_streamable_http_post,
)
from .secrets import load_secrets
from .utils import source_ip as _source_ip


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup order matters:
    # 1. load_config  — required by validate_stdio_commands
    # 2. load_secrets — required by subprocess env injection; must precede validation
    # 3. setup_logging — needs config for log path
    # 4. load_patterns — regex patterns for detection (fast, synchronous)
    # 5. init_detector — load AI model (may be slow; logged if unavailable)
    # 6. init_bridge  — creates asyncio.Lock inside the running event loop
    # 7. validate     — fail-fast executable check with secrets available
    load_config()
    load_secrets()
    setup_logging()
    load_patterns()
    init_detector()
    init_bridge()
    validate_stdio_commands(get_stdio_destinations())

    # Register SIGHUP to reload regex patterns without restart.
    # Use loop.add_signal_handler (not signal.signal) to avoid deadlock:
    # signal.signal handlers interrupt the thread and can deadlock if the
    # thread already holds the patterns lock.
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, reload_patterns)

    yield
    # Shutdown: terminate all managed stdio subprocesses
    await shutdown_all_stdio()


app = FastAPI(title="mithril-proxy", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/admin/reload-patterns")
async def admin_reload_patterns(request: Request):
    """Reload regex patterns from patterns.d/. Restricted to localhost."""
    client_ip = _source_ip(request)
    if client_ip not in ("127.0.0.1", "::1"):
        return JSONResponse(
            status_code=403,
            content={"error": "Admin endpoints are restricted to localhost"},
        )
    try:
        count = reload_patterns()
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"error": "Pattern reload failed"},
        )
    return {"loaded": count}


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


@app.delete("/{destination}/mcp")
async def mcp_delete_endpoint(destination: str, request: Request):
    return await handle_streamable_http_delete(request, destination)
