"""SSE proxy + session management for mithril-proxy."""

from __future__ import annotations

import asyncio
import re
import time
from typing import AsyncIterator, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import get_destination
from .logger import log_request

# --------------------------------------------------------------------------- #
#  Session map: session_id → upstream message URL                             #
# --------------------------------------------------------------------------- #

_session_map: dict[str, str] = {}
_session_lock = asyncio.Lock()


async def _register_session(session_id: str, upstream_url: str) -> None:
    async with _session_lock:
        _session_map[session_id] = upstream_url


async def _remove_session(session_id: str) -> None:
    async with _session_lock:
        _session_map.pop(session_id, None)


async def _get_session_url(session_id: str) -> Optional[str]:
    async with _session_lock:
        return _session_map.get(session_id)


# --------------------------------------------------------------------------- #
#  Helper: extract Bearer token prefix for log correlation                    #
# --------------------------------------------------------------------------- #

def _user_from_request(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token[:8] if token else "anonymous"
    return "anonymous"


def _source_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# --------------------------------------------------------------------------- #
#  Upstream headers — pass everything except Host                             #
# --------------------------------------------------------------------------- #

def _upstream_headers(request: Request) -> dict[str, str]:
    skip = {"host", "content-length", "transfer-encoding"}
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in skip
    }


# --------------------------------------------------------------------------- #
#  Retry wrapper                                                               #
# --------------------------------------------------------------------------- #

_RETRY_DELAYS = [0.5, 1.0, 2.0]


async def _connect_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(len(_RETRY_DELAYS)):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code < 500:
                return response
            last_exc = RuntimeError(f"Upstream returned {response.status_code}")
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            last_exc = exc
        if attempt < len(_RETRY_DELAYS) - 1:
            await asyncio.sleep(_RETRY_DELAYS[attempt])
    raise last_exc


# --------------------------------------------------------------------------- #
#  SSE endpoint  GET /{destination}/sse                                       #
# --------------------------------------------------------------------------- #

_SESSION_ID_RE = re.compile(r"[?&]sessionId=([^&\s]+)")
_ENDPOINT_URL_RE = re.compile(r"(https?://[^\s]+|/[^\s]*)")


def _rewrite_endpoint_event(
    data: str,
    destination: str,
    session_id: str,
) -> str:
    """Replace upstream message endpoint URL with our proxy URL."""
    return f"/{destination}/message?session_id={session_id}"


async def handle_sse(request: Request, destination: str) -> Response:
    dest_config = get_destination(destination)
    if dest_config is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Unknown destination: {destination}"},
        )

    if dest_config.type == "stdio":
        from .bridge import handle_stdio_sse
        from .secrets import get_destination_env
        subprocess_env = {**dest_config.env, **get_destination_env(destination)}
        return await handle_stdio_sse(request, destination, dest_config, subprocess_env)

    upstream_base = dest_config.url

    upstream_url = f"{upstream_base}/sse"
    headers = _upstream_headers(request)
    user = _user_from_request(request)
    source_ip = _source_ip(request)
    start = time.monotonic()

    async def event_stream() -> AsyncIterator[bytes]:
        session_id: Optional[str] = None
        error_msg: Optional[str] = None
        status_code = 200

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", upstream_url, headers=headers) as upstream:
                    status_code = upstream.status_code
                    if upstream.status_code >= 400:
                        body = await upstream.aread()
                        error_msg = f"Upstream returned {upstream.status_code}"
                        yield body
                        return

                    # Buffer to accumulate SSE fields for one event
                    event_type: Optional[str] = None
                    data_lines: list[str] = []

                    async for raw_line in upstream.aiter_lines():
                        if raw_line.startswith("event:"):
                            event_type = raw_line[len("event:"):].strip()
                            yield (raw_line + "\n").encode()

                        elif raw_line.startswith("data:"):
                            data_value = raw_line[len("data:"):].strip()

                            if event_type == "endpoint":
                                # Extract sessionId from the upstream URL
                                m = _SESSION_ID_RE.search(data_value)
                                if m:
                                    session_id = m.group(1)
                                    await _register_session(
                                        session_id,
                                        _build_upstream_message_url(upstream_base, data_value),
                                    )
                                    rewritten = _rewrite_endpoint_event(
                                        data_value, destination, session_id
                                    )
                                    yield f"data: {rewritten}\n".encode()
                                else:
                                    yield (raw_line + "\n").encode()
                                event_type = None
                            else:
                                yield (raw_line + "\n").encode()

                        elif raw_line == "":
                            # Blank line = end of SSE event
                            event_type = None
                            data_lines = []
                            yield b"\n"

                        else:
                            yield (raw_line + "\n").encode()

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            status_code = 502
            error_msg = str(exc)
            import json as _json
            yield (
                f"event: error\ndata: {_json.dumps({'error': 'upstream unavailable'})}\n\n"
            ).encode()
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            if session_id:
                await _remove_session(session_id)
            log_request(
                user=user,
                source_ip=source_ip,
                destination=destination,
                mcp_method=None,
                status_code=status_code,
                latency_ms=latency_ms,
                error=error_msg,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _build_upstream_message_url(upstream_base: str, endpoint_data: str) -> str:
    """Construct the full upstream message URL from the endpoint event data."""
    # endpoint_data may be a relative path like /messages?sessionId=abc
    # or a full URL
    if endpoint_data.startswith("http"):
        return endpoint_data
    parsed = urlparse(upstream_base)
    return urlunparse(parsed._replace(path=endpoint_data.split("?")[0], query=endpoint_data.split("?")[1] if "?" in endpoint_data else ""))


# --------------------------------------------------------------------------- #
#  Message endpoint  POST /{destination}/message                              #
# --------------------------------------------------------------------------- #

async def handle_message(
    request: Request,
    destination: str,
) -> Response:
    dest_config = get_destination(destination)
    if dest_config is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Unknown destination: {destination}"},
        )

    session_id = request.query_params.get("session_id")
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing session_id query parameter"},
        )

    if dest_config.type == "stdio":
        from .bridge import handle_stdio_message
        return await handle_stdio_message(request, destination, session_id)

    upstream_url = await _get_session_url(session_id)
    if upstream_url is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session not found: {session_id}"},
        )

    headers = _upstream_headers(request)
    user = _user_from_request(request)
    source_ip = _source_ip(request)
    start = time.monotonic()

    body = await request.body()

    # Extract MCP method from JSON-RPC body for logging
    mcp_method: Optional[str] = None
    try:
        import json
        payload = json.loads(body)
        mcp_method = payload.get("method")
    except Exception:
        pass

    error_msg: Optional[str] = None
    status_code = 502

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream_response = await _connect_with_retries(
                client,
                "POST",
                upstream_url,
                headers=headers,
                content=body,
            )
            status_code = upstream_response.status_code
            response_body = upstream_response.content
            response_headers = dict(upstream_response.headers)
            # Strip hop-by-hop headers
            for h in ("transfer-encoding", "connection", "keep-alive"):
                response_headers.pop(h, None)

    except Exception as exc:
        error_msg = str(exc)
        latency_ms = (time.monotonic() - start) * 1000
        log_request(
            user=user,
            source_ip=source_ip,
            destination=destination,
            mcp_method=mcp_method,
            status_code=status_code,
            latency_ms=latency_ms,
            error=error_msg,
        )
        return JSONResponse(
            status_code=502,
            content={"error": "Upstream unreachable", "detail": str(exc)},
        )

    latency_ms = (time.monotonic() - start) * 1000
    log_request(
        user=user,
        source_ip=source_ip,
        destination=destination,
        mcp_method=mcp_method,
        status_code=status_code,
        latency_ms=latency_ms,
        error=error_msg,
    )

    return Response(
        content=response_body,
        status_code=status_code,
        headers=response_headers,
    )
