"""FastAPI application entry point for mithril-proxy."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import load_config
from .logger import setup_logging
from .proxy import handle_message, handle_sse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: load config and initialise logging (fail-fast on bad config)
    load_config()
    setup_logging()
    yield
    # Shutdown: nothing to clean up currently


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
