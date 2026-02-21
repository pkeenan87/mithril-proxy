"""stdio-to-Streamable HTTP bridge for mithril-proxy.

Each stdio destination gets a single subprocess shared across all sessions.
A per-destination StdioDestinationBridge holds:
  - The running subprocess
  - A pending dict mapping internal_id → (future, original_id)
  - A sessions set of active Mcp-Session-Id values
  - Per-stream notification queues (one per GET /mcp stream)

The stdout reader task dispatches subprocess output:
  - Lines with a matching pending id → resolve the waiting POST future
  - Lines with no id (notifications) → broadcast to all notification queues

Restart policy: up to 3 restarts with delays [0.5s, 1.0s, 2.0s].
On exhaustion, all pending futures fail (503) and all GET streams are closed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .logger import get_logger, log_request
from .utils import source_ip as _source_ip

if TYPE_CHECKING:
    from .config import DestinationConfig

# --------------------------------------------------------------------------- #
#  Constants                                                                   #
# --------------------------------------------------------------------------- #

_RETRY_DELAYS = [0.5, 1.0, 2.0]
_SHUTDOWN_GRACE = 5.0
_MAX_CONNECTIONS_PER_DEST = int(os.environ.get("MAX_STDIO_CONNECTIONS", "10"))
_MAX_QUEUE_SIZE = 256

STDIO_RESPONSE_TIMEOUT_SECS = float(os.environ.get("STDIO_RESPONSE_TIMEOUT_SECS", "30"))

# Allowlisted parent-process env keys passed to subprocesses.
# Intentionally minimal — secrets are supplied exclusively via extra_env
# (populated from config/secrets.yml), never from the parent environment.
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TEMP", "TMP", "TERM", "SHELL",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "NPM_CONFIG_CACHE",
})

# UUID v4 format — used to validate session_id before lookup
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Kept for backward compatibility (previously used by _stdin_writer)
_SENTINEL = object()


# --------------------------------------------------------------------------- #
#  Per-destination bridge                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class StdioDestinationBridge:
    """Holds all state for one stdio destination's subprocess and sessions."""
    destination: str
    process: Optional[asyncio.subprocess.Process] = None
    stdout_task: Optional[asyncio.Task] = None
    stderr_task: Optional[asyncio.Task] = None
    # internal_id → (future, original_client_id)
    pending: dict = field(default_factory=dict)
    _counter: int = 0
    # Active Mcp-Session-Id values
    sessions: set = field(default_factory=set)
    # stream_uuid → asyncio.Queue (one per active GET /mcp stream)
    notification_queues: dict = field(default_factory=dict)
    # session_id → set of stream_uuids (for cleanup on DELETE)
    session_stream_uuids: dict = field(default_factory=dict)
    # Serializes stdin writes to prevent interleaved bytes
    stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Prevents concurrent subprocess spawns
    spawn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_stdio_bridges: dict[str, StdioDestinationBridge] = {}
_bridges_create_lock: Optional[asyncio.Lock] = None


def init_bridge() -> None:
    """Initialize bridge primitives. Must be called inside a running event loop."""
    global _bridges_create_lock
    _bridges_create_lock = asyncio.Lock()


def _get_bridges_create_lock() -> asyncio.Lock:
    """Return the creation lock, initializing lazily if init_bridge() was not called."""
    global _bridges_create_lock
    if _bridges_create_lock is None:
        _bridges_create_lock = asyncio.Lock()
    return _bridges_create_lock


async def _get_or_create_bridge(destination: str) -> StdioDestinationBridge:
    """Thread-safe get-or-create a StdioDestinationBridge for destination."""
    if destination in _stdio_bridges:
        return _stdio_bridges[destination]
    async with _get_bridges_create_lock():
        if destination in _stdio_bridges:
            return _stdio_bridges[destination]
        bridge = StdioDestinationBridge(destination=destination)
        _stdio_bridges[destination] = bridge
        return bridge


# --------------------------------------------------------------------------- #
#  Command validation                                                          #
# --------------------------------------------------------------------------- #

def validate_stdio_commands(configs: dict) -> None:
    """Fail-fast check that every stdio destination's executable exists on PATH.

    Non-stdio destinations are skipped.  Raises ValueError on first bad entry.
    """
    for name, dest_config in configs.items():
        if dest_config.type != "stdio":
            continue
        cmd = dest_config.command
        if not cmd:
            raise ValueError(f"stdio destination '{name}' has no command configured.")
        executable = shlex.split(cmd)[0]
        if shutil.which(executable) is None:
            raise ValueError(
                f"stdio destination '{name}': command executable '{executable}' not found on PATH."
            )


# --------------------------------------------------------------------------- #
#  Subprocess helpers                                                          #
# --------------------------------------------------------------------------- #

async def _spawn_process(
    command: str,
    extra_env: dict[str, str],
) -> asyncio.subprocess.Process:
    """Spawn *command* with a minimal, allowlisted environment.

    Only keys in ``_SAFE_ENV_KEYS`` are inherited from the parent process.
    All secrets and MCP-specific variables come exclusively from ``extra_env``.
    """
    args = shlex.split(command)
    safe_base = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env = {**safe_base, **extra_env}
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Send SIGTERM to *process* if it is still running."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass


async def _stderr_reader(
    process: asyncio.subprocess.Process,
    destination: str,
    session_id: str,
) -> None:
    """Read stderr and log each line at WARNING level. Never forwarded to clients."""
    logger = get_logger()
    try:
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            logger.warning(
                "subprocess stderr",
                extra={
                    "destination": destination,
                    "session_id": session_id,
                    "stderr_line": line.decode(errors="replace").rstrip(),
                },
            )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Per-destination stdout reader (long-lived task)                            #
# --------------------------------------------------------------------------- #

async def _stdio_stdout_reader(
    bridge: StdioDestinationBridge,
    dest_config: "DestinationConfig",
    subprocess_env: dict[str, str],
) -> None:
    """Long-lived per-destination task that dispatches stdout lines.

    Outer loop handles subprocess restarts (up to len(_RETRY_DELAYS) + 1 lifetimes).
    Inner loop reads lines and dispatches:
      - id matching pending → resolve POST future
      - no id (or unknown id) → broadcast to notification queues
    On stdout EOF, pending futures are failed and a restart is attempted.
    After all retries exhausted, all GET streams receive a close sentinel.
    """
    logger = get_logger()

    for attempt in range(len(_RETRY_DELAYS) + 1):
        # Read stdout until EOF
        try:
            while True:
                line = await bridge.process.stdout.readline()
                if not line:
                    break
                line_str = line.rstrip(b"\n").decode(errors="replace")
                try:
                    msg = json.loads(line_str)
                except Exception:
                    logger.warning(
                        "subprocess stdout: malformed JSON",
                        extra={
                            "destination": bridge.destination,
                            "line": line_str[:256],
                        },
                    )
                    continue

                msg_id = msg.get("id")
                if msg_id is not None and msg_id in bridge.pending:
                    future, original_id = bridge.pending.pop(msg_id)
                    msg["id"] = original_id
                    log_request(
                        user="stdio",
                        source_ip="localhost",
                        destination=bridge.destination,
                        mcp_method=None,
                        status_code=200,
                        latency_ms=0.0,
                        rpc_id=original_id,
                        response_body=line_str,
                    )
                    if not future.done():
                        future.set_result(msg)
                else:
                    # Notification — broadcast to all active GET streams
                    log_request(
                        user="stdio",
                        source_ip="localhost",
                        destination=bridge.destination,
                        mcp_method=None,
                        status_code=200,
                        latency_ms=0.0,
                        response_body=line_str,
                    )
                    for q in list(bridge.notification_queues.values()):
                        try:
                            q.put_nowait(line_str)
                        except asyncio.QueueFull:
                            pass

        except Exception as exc:
            logger.warning(
                "subprocess stdout reader error",
                extra={"destination": bridge.destination, "error": str(exc)},
            )

        # stdout EOF — subprocess exited
        exit_code = await bridge.process.wait()
        logger.warning(
            "subprocess exited",
            extra={
                "destination": bridge.destination,
                "exit_code": exit_code,
                "attempt": attempt + 1,
            },
        )

        # Fail all pending futures so their POST handlers can return 503
        pending_exc = RuntimeError(f"subprocess exited with code {exit_code}")
        for internal_id, (future, original_id) in list(bridge.pending.items()):
            if not future.done():
                future.set_exception(pending_exc)
        bridge.pending.clear()

        if attempt >= len(_RETRY_DELAYS):
            break

        # Restart with exponential backoff
        retry_delay = _RETRY_DELAYS[attempt]
        logger.info(
            "subprocess restarting",
            extra={
                "destination": bridge.destination,
                "retry_in_seconds": retry_delay,
                "restart_attempt": attempt + 1,
            },
        )
        await asyncio.sleep(retry_delay)

        async with bridge.spawn_lock:
            try:
                new_process = await _spawn_process(dest_config.command, subprocess_env)
                bridge.process = new_process
                bridge.stderr_task = asyncio.create_task(
                    _stderr_reader(new_process, bridge.destination, "bridge")
                )
                logger.info(
                    "subprocess restarted",
                    extra={
                        "destination": bridge.destination,
                        "pid": new_process.pid,
                        "restart_attempt": attempt + 1,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "subprocess restart failed",
                    extra={
                        "destination": bridge.destination,
                        "restart_attempt": attempt + 1,
                        "error": str(exc),
                    },
                )
                break

    # All retries exhausted — close all active GET streams and clean up
    logger.warning(
        "subprocess exhausted all retries, closing bridge",
        extra={"destination": bridge.destination},
    )
    for q in list(bridge.notification_queues.values()):
        # Drain to make room for the close sentinel so GET streams don't block.
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(None)  # None signals stream close
        except asyncio.QueueFull:
            pass  # Should not happen after drain
    bridge.sessions.clear()
    bridge.session_stream_uuids.clear()
    _stdio_bridges.pop(bridge.destination, None)


# --------------------------------------------------------------------------- #
#  Subprocess ensure                                                           #
# --------------------------------------------------------------------------- #

async def _ensure_subprocess(
    bridge: StdioDestinationBridge,
    dest_config: "DestinationConfig",
    subprocess_env: dict[str, str],
) -> None:
    """Ensure the bridge's subprocess is running. Thread-safe via spawn_lock.

    If the stdout reader task is still alive (managing retries), returns without
    spawning to avoid interfering with the retry cycle.
    """
    async with bridge.spawn_lock:
        # If process is running, nothing to do
        if bridge.process is not None and bridge.process.returncode is None:
            return
        # If stdout reader is alive (e.g., sleeping between retries), don't interfere
        if bridge.stdout_task is not None and not bridge.stdout_task.done():
            return

        logger = get_logger()
        process = await _spawn_process(dest_config.command, subprocess_env)
        bridge.process = process
        logger.info(
            "subprocess started",
            extra={
                "destination": bridge.destination,
                "pid": process.pid,
                "executable": shlex.split(dest_config.command)[0],
            },
        )
        bridge.stdout_task = asyncio.create_task(
            _stdio_stdout_reader(bridge, dest_config, subprocess_env)
        )
        bridge.stderr_task = asyncio.create_task(
            _stderr_reader(process, bridge.destination, "bridge")
        )


# --------------------------------------------------------------------------- #
#  Shutdown                                                                    #
# --------------------------------------------------------------------------- #

async def shutdown_all_stdio() -> None:
    """Gracefully terminate all active stdio bridges. Called at proxy shutdown."""
    logger = get_logger()
    bridges = list(_stdio_bridges.values())
    if not bridges:
        return

    logger.info(
        "shutdown: terminating stdio bridges",
        extra={"count": len(bridges)},
    )

    processes = [b.process for b in bridges if b.process is not None]
    for process in processes:
        _terminate_process(process)

    async def _wait_one(proc: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_GRACE)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    await asyncio.gather(*(_wait_one(p) for p in processes), return_exceptions=True)
    _stdio_bridges.clear()


# --------------------------------------------------------------------------- #
#  Streamable HTTP handlers                                                    #
# --------------------------------------------------------------------------- #

async def handle_stdio_streamable_http_post(
    request: Request,
    destination: str,
    dest_config: "DestinationConfig",
    subprocess_env: dict[str, str],
) -> Response:
    """Handle ``POST /{destination}/mcp`` for stdio-backed destinations.

    On the first request (no Mcp-Session-Id header), a new session UUID is
    created and returned in the response header.  Subsequent requests include
    the header and are routed to the shared subprocess for that destination.
    """
    start = time.monotonic()
    source_ip = _source_ip(request)

    bridge = await _get_or_create_bridge(destination)

    try:
        await _ensure_subprocess(bridge, dest_config, subprocess_env)
    except Exception as exc:
        get_logger().warning(
            "subprocess start failed",
            extra={"destination": destination, "error": str(exc)},
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Failed to start subprocess"},
        )

    # Read and parse body
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    if isinstance(payload, list):
        return JSONResponse(
            status_code=400,
            content={"error": "Batch JSON-RPC is not supported"},
        )

    original_id = payload.get("id")
    mcp_method = payload.get("method")
    request_body_str = body.decode(errors="replace")

    # Validate Mcp-Session-Id header
    session_id_header = request.headers.get("mcp-session-id")
    new_session = False

    if session_id_header is None:
        # A notification (no id) cannot initiate a session — the client has no
        # way to receive the session ID that would need to be returned.
        if original_id is None:
            return JSONResponse(
                status_code=400,
                content={"error": "Cannot initiate a session with a notification (missing 'id')"},
            )
        # New session — enforce connection cap
        if len(bridge.sessions) >= _MAX_CONNECTIONS_PER_DEST:
            return JSONResponse(
                status_code=503,
                content={
                    "error": (
                        f"Too many active sessions for '{destination}' "
                        f"(max {_MAX_CONNECTIONS_PER_DEST})"
                    )
                },
            )
        session_id = str(uuid.uuid4())
        bridge.sessions.add(session_id)
        new_session = True
    elif not _UUID4_RE.match(session_id_header):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid Mcp-Session-Id format"},
        )
    elif session_id_header not in bridge.sessions:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session not found: {session_id_header}"},
        )
    else:
        session_id = session_id_header

    # Client notification (no id field) — fire-and-forget, return 202
    if original_id is None:
        data = json.dumps(payload).encode() + b"\n"
        try:
            async with bridge.stdin_lock:
                bridge.process.stdin.write(data)
                await bridge.process.stdin.drain()
        except OSError:
            pass
        log_request(
            user="stdio",
            source_ip=source_ip,
            destination=destination,
            mcp_method=mcp_method,
            status_code=202,
            latency_ms=(time.monotonic() - start) * 1000,
            request_body=request_body_str,
        )
        return Response(status_code=202)

    # Assign a monotonic internal id to avoid cross-client id collisions
    internal_id = bridge._counter
    bridge._counter += 1
    payload["id"] = internal_id

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    bridge.pending[internal_id] = (future, original_id)

    # Write to subprocess stdin
    data = json.dumps(payload).encode() + b"\n"
    try:
        async with bridge.stdin_lock:
            bridge.process.stdin.write(data)
            await bridge.process.stdin.drain()
    except OSError as exc:
        entry = bridge.pending.pop(internal_id, None)
        if entry is not None:
            entry[0].cancel()
        if new_session:
            bridge.sessions.discard(session_id)
        log_request(
            user="stdio",
            source_ip=source_ip,
            destination=destination,
            mcp_method=mcp_method,
            status_code=503,
            latency_ms=(time.monotonic() - start) * 1000,
            error=str(exc),
            request_body=request_body_str,
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Subprocess stdin unavailable"},
        )

    # Wait for the matching response from stdout
    try:
        result = await asyncio.wait_for(future, timeout=STDIO_RESPONSE_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        bridge.pending.pop(internal_id, None)
        if new_session:
            bridge.sessions.discard(session_id)
        log_request(
            user="stdio",
            source_ip=source_ip,
            destination=destination,
            mcp_method=mcp_method,
            status_code=504,
            latency_ms=(time.monotonic() - start) * 1000,
            rpc_id=original_id,
            request_body=request_body_str,
        )
        return JSONResponse(
            status_code=504,
            content={"error": "Subprocess response timeout"},
        )
    except Exception as exc:
        if new_session:
            bridge.sessions.discard(session_id)
        log_request(
            user="stdio",
            source_ip=source_ip,
            destination=destination,
            mcp_method=mcp_method,
            status_code=503,
            latency_ms=(time.monotonic() - start) * 1000,
            rpc_id=original_id,
            request_body=request_body_str,
            error=str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Subprocess unavailable"},
        )

    response_body_str = json.dumps(result)
    response_headers: dict[str, str] = {}
    if new_session:
        response_headers["mcp-session-id"] = session_id

    log_request(
        user="stdio",
        source_ip=source_ip,
        destination=destination,
        mcp_method=mcp_method,
        status_code=200,
        latency_ms=(time.monotonic() - start) * 1000,
        rpc_id=original_id,
        request_body=request_body_str,
        response_body=response_body_str,
    )
    return Response(
        content=response_body_str,
        media_type="application/json",
        status_code=200,
        headers=response_headers,
    )


async def handle_stdio_streamable_http_get(
    request: Request,
    destination: str,
    dest_config: "DestinationConfig",
    subprocess_env: dict[str, str],
) -> Response:
    """Handle ``GET /{destination}/mcp`` for stdio-backed destinations.

    Opens an SSE stream that drains the session's notification queue.
    Multiple concurrent GET streams per session are allowed (broadcast).
    """
    start = time.monotonic()

    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing Mcp-Session-Id header"},
        )
    if not _UUID4_RE.match(session_id):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid Mcp-Session-Id format"},
        )

    bridge = _stdio_bridges.get(destination)
    if bridge is None or session_id not in bridge.sessions:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session not found: {session_id}"},
        )

    stream_uuid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    bridge.notification_queues[stream_uuid] = q

    if session_id not in bridge.session_stream_uuids:
        bridge.session_stream_uuids[session_id] = set()
    bridge.session_stream_uuids[session_id].add(stream_uuid)

    status_code = 200

    async def event_generator() -> AsyncIterator[bytes]:
        nonlocal status_code
        try:
            while True:
                item = await q.get()
                if item is None:
                    # Subprocess died — inform client and close
                    status_code = 503
                    yield (
                        f"event: error\ndata: {json.dumps({'error': 'subprocess unavailable'})}\n\n"
                    ).encode()
                    return
                yield b"data: " + item.encode() + b"\n\n"
        finally:
            bridge.notification_queues.pop(stream_uuid, None)
            if session_id in bridge.session_stream_uuids:
                bridge.session_stream_uuids[session_id].discard(stream_uuid)
            log_request(
                user="stdio",
                source_ip=_source_ip(request),
                destination=destination,
                mcp_method=None,
                status_code=status_code,
                latency_ms=(time.monotonic() - start) * 1000,
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_stdio_streamable_http_delete(
    request: Request,
    destination: str,
    dest_config: "DestinationConfig",
    subprocess_env: dict[str, str],
) -> Response:
    """Handle ``DELETE /{destination}/mcp`` for stdio-backed destinations.

    Removes the session and closes any notification streams for it.
    The subprocess stays alive for other active sessions.
    """
    start = time.monotonic()

    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing Mcp-Session-Id header"},
        )
    if not _UUID4_RE.match(session_id):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid Mcp-Session-Id format"},
        )

    bridge = _stdio_bridges.get(destination)
    if bridge is None or session_id not in bridge.sessions:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session not found: {session_id}"},
        )

    bridge.sessions.discard(session_id)

    # Close all notification streams registered for this session
    stream_uuids = bridge.session_stream_uuids.pop(session_id, set())
    for stream_uuid in stream_uuids:
        q = bridge.notification_queues.pop(stream_uuid, None)
        if q is not None:
            # Drain to make room for the close sentinel so GET streams don't block.
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass  # Should not happen after drain

    log_request(
        user="stdio",
        source_ip=_source_ip(request),
        destination=destination,
        mcp_method=None,
        status_code=204,
        latency_ms=(time.monotonic() - start) * 1000,
    )
    return Response(status_code=204)
