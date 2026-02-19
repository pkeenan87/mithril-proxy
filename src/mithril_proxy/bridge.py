"""stdio-to-SSE bridge for mithril-proxy.

Each incoming SSE connection spawns its own subprocess. Three concurrent asyncio
tasks per connection handle I/O:

  stdout_reader  → out_queue → event_stream (async generator yields SSE chunks)
  stdin_writer   ← in_queue ← handle_stdio_message (POST endpoint enqueues body)
  stderr_reader  → log.warning (never forwarded to the SSE client)

The asyncio.Queue decouples the concurrent stdout-reader task from the async
generator that cannot be fed directly by another coroutine.

Restart policy: on subprocess exit, restart up to 3 times (delays: 0.5s, 1.0s,
2.0s). After all retries are exhausted, emit an SSE error event and close.
Each SSE client gets its own independent subprocess.
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

if TYPE_CHECKING:
    from .config import DestinationConfig

# --------------------------------------------------------------------------- #
#  Constants                                                                   #
# --------------------------------------------------------------------------- #

_RETRY_DELAYS = [0.5, 1.0, 2.0]
_SHUTDOWN_GRACE = 5.0
_MAX_CONNECTIONS_PER_DEST = int(os.environ.get("MAX_STDIO_CONNECTIONS", "10"))
_MAX_QUEUE_SIZE = 256

# Allowlisted parent-process env keys passed to subprocesses.
# Intentionally minimal — secrets are supplied exclusively via extra_env
# (populated from config/secrets.yml), never from the parent environment.
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TEMP", "TMP", "TERM", "SHELL",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
})

# UUID v4 format — used to validate session_id before lookup
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Sentinel that signals the stdin-writer task to stop cleanly.
_SENTINEL = object()


# --------------------------------------------------------------------------- #
#  Session registry                                                            #
# --------------------------------------------------------------------------- #

@dataclass
class StdioSession:
    session_id: str
    destination: str
    process: asyncio.subprocess.Process
    stdin_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    )


_stdio_sessions: dict[str, StdioSession] = {}
_stdio_lock: Optional[asyncio.Lock] = None


def init_bridge() -> None:
    """Initialize bridge primitives. Must be called inside a running event loop."""
    global _stdio_lock
    _stdio_lock = asyncio.Lock()


def _get_lock() -> asyncio.Lock:
    """Return the session lock, initializing lazily if init_bridge() was not called."""
    global _stdio_lock
    if _stdio_lock is None:
        _stdio_lock = asyncio.Lock()
    return _stdio_lock


async def _register_stdio_session(session: StdioSession) -> None:
    async with _get_lock():
        _stdio_sessions[session.session_id] = session


async def _remove_stdio_session(session_id: str) -> None:
    async with _get_lock():
        _stdio_sessions.pop(session_id, None)


def get_stdio_session(session_id: str) -> Optional[StdioSession]:
    """Return the active StdioSession for *session_id*, or None."""
    return _stdio_sessions.get(session_id)


# --------------------------------------------------------------------------- #
#  Command validation                                                          #
# --------------------------------------------------------------------------- #

def validate_stdio_commands(configs: dict) -> None:
    """Fail-fast check that every stdio destination's executable exists on PATH.

    SSE-type destinations are skipped.  Raises ValueError on first bad entry.
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


async def _stdout_reader(
    process: asyncio.subprocess.Process,
    out_queue: asyncio.Queue,
) -> None:
    """Read stdout lines from process, enqueue formatted SSE data chunks.

    Puts ``None`` on the queue to signal EOF.
    """
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            await out_queue.put(b"data: " + line.rstrip(b"\n") + b"\n\n")
    finally:
        await out_queue.put(None)


async def _stdin_writer(
    process: asyncio.subprocess.Process,
    in_queue: asyncio.Queue,
) -> None:
    """Drain *in_queue* and write each item to process stdin.

    Stops when ``_SENTINEL`` is dequeued or when the pipe breaks.
    """
    try:
        while True:
            item = await in_queue.get()
            if item is _SENTINEL:
                break
            try:
                process.stdin.write(item)
                await process.stdin.drain()
            except OSError:
                # Covers BrokenPipeError, ConnectionResetError, and any other
                # pipe-related errors that arise when the subprocess exits.
                break
    finally:
        try:
            process.stdin.close()
        except Exception:
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


async def _drain_stdout_task(
    out_queue: asyncio.Queue,
) -> AsyncIterator[bytes]:
    """Async generator that yields chunks from *out_queue* until the None sentinel."""
    while True:
        chunk = await out_queue.get()
        if chunk is None:
            return
        yield chunk


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Send SIGTERM to *process* if it is still running."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass


# --------------------------------------------------------------------------- #
#  Shutdown                                                                    #
# --------------------------------------------------------------------------- #

async def shutdown_all_stdio() -> None:
    """Gracefully terminate all active stdio sessions. Called at proxy shutdown."""
    logger = get_logger()
    sessions = list(_stdio_sessions.values())
    if not sessions:
        return

    logger.info(
        "shutdown: terminating stdio sessions",
        extra={"count": len(sessions)},
    )

    # Send SIGTERM to all running processes
    for session in sessions:
        _terminate_process(session.process)

    # Wait up to grace period, then SIGKILL survivors
    async def _wait_one(proc: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_GRACE)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    await asyncio.gather(*(_wait_one(s.process) for s in sessions), return_exceptions=True)
    _stdio_sessions.clear()


# --------------------------------------------------------------------------- #
#  SSE handler                                                                 #
# --------------------------------------------------------------------------- #

async def handle_stdio_sse(
    request: Request,
    destination: str,
    dest_config: "DestinationConfig",
    subprocess_env: dict[str, str],
) -> Response:
    """Handle ``GET /{destination}/sse`` for a stdio-backed destination.

    Spawns a subprocess, registers a session, and returns a StreamingResponse
    that relays stdout as SSE ``data:`` events.
    """
    logger = get_logger()

    # Enforce per-destination connection cap before spawning anything
    active_count = sum(1 for s in _stdio_sessions.values() if s.destination == destination)
    if active_count >= _MAX_CONNECTIONS_PER_DEST:
        return JSONResponse(
            status_code=503,
            content={
                "error": (
                    f"Too many active connections for '{destination}' "
                    f"(max {_MAX_CONNECTIONS_PER_DEST})"
                )
            },
        )

    session_id = str(uuid.uuid4())
    start = time.monotonic()
    status_code = 200
    error_msg: Optional[str] = None
    executable = shlex.split(dest_config.command)[0]

    # Attempt initial process spawn (up to 3 attempts for the first spawn only)
    process: Optional[asyncio.subprocess.Process] = None
    for attempt in range(len(_RETRY_DELAYS)):
        try:
            process = await _spawn_process(dest_config.command, subprocess_env)
            logger.info(
                "subprocess started",
                extra={
                    "destination": destination,
                    "session_id": session_id,
                    "pid": process.pid,
                    "executable": executable,   # basename only — no inline secrets
                },
            )
            break
        except Exception as exc:
            logger.warning(
                "subprocess start failed",
                extra={
                    "destination": destination,
                    "session_id": session_id,
                    "attempt": attempt + 1,
                    "error": str(exc),
                },
            )
            if attempt < len(_RETRY_DELAYS) - 1:
                await asyncio.sleep(_RETRY_DELAYS[attempt])

    if process is None:
        return JSONResponse(
            status_code=503,
            content={"error": f"Failed to start subprocess for destination '{destination}'"},
        )

    session = StdioSession(
        session_id=session_id,
        destination=destination,
        process=process,
    )
    await _register_stdio_session(session)

    async def event_stream():
        nonlocal status_code, error_msg
        current_process = process
        active_tasks: list[asyncio.Task] = []

        try:
            # Emit the endpoint event so the client knows where to POST messages
            yield (
                f"event: endpoint\n"
                f"data: /{destination}/message?session_id={session_id}\n\n"
            ).encode()

            # Run up to len(_RETRY_DELAYS)+1 process lifetimes (initial + 3 restarts)
            for attempt in range(len(_RETRY_DELAYS) + 1):
                out_queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)

                stdout_task = asyncio.create_task(
                    _stdout_reader(current_process, out_queue)
                )
                stdin_task = asyncio.create_task(
                    _stdin_writer(current_process, session.stdin_queue)
                )
                stderr_task = asyncio.create_task(
                    _stderr_reader(current_process, destination, session_id)
                )
                active_tasks = [stdout_task, stdin_task, stderr_task]

                # Relay stdout to the SSE client
                async for chunk in _drain_stdout_task(out_queue):
                    yield chunk

                # stdout EOF — signal stdin writer to stop, then wait for workers
                await session.stdin_queue.put(_SENTINEL)
                await asyncio.gather(stdin_task, stderr_task, return_exceptions=True)
                # Tasks are now done; clear list before the next await point so
                # the finally block doesn't double-cancel already-completed tasks.
                active_tasks = []

                exit_code = await current_process.wait()
                logger.warning(
                    "subprocess exited",
                    extra={
                        "destination": destination,
                        "session_id": session_id,
                        "exit_code": exit_code,
                        "attempt": attempt + 1,
                    },
                )

                if attempt >= len(_RETRY_DELAYS):
                    # All restart attempts exhausted
                    break

                # Restart with exponential backoff
                retry_delay = _RETRY_DELAYS[attempt]
                logger.info(
                    "subprocess restarting",
                    extra={
                        "destination": destination,
                        "session_id": session_id,
                        "retry_in_seconds": retry_delay,
                        "restart_attempt": attempt + 1,
                    },
                )
                await asyncio.sleep(retry_delay)

                try:
                    current_process = await _spawn_process(dest_config.command, subprocess_env)
                    session.process = current_process
                    session.stdin_queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
                    logger.info(
                        "subprocess restarted",
                        extra={
                            "destination": destination,
                            "session_id": session_id,
                            "pid": current_process.pid,
                            "restart_attempt": attempt + 1,
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "subprocess restart failed",
                        extra={
                            "destination": destination,
                            "session_id": session_id,
                            "restart_attempt": attempt + 1,
                            "error": str(exc),
                        },
                    )
                    break

            # All attempts exhausted — inform the client
            status_code = 503
            error_msg = "subprocess unavailable after max retries"
            yield (
                f"event: error\n"
                f"data: {json.dumps({'error': 'subprocess unavailable'})}\n\n"
            ).encode()

        finally:
            # Cancel any tasks still running (e.g. client disconnected mid-stream)
            for task in active_tasks:
                if not task.done():
                    task.cancel()
            # Await cancelled tasks so they are properly cleaned up and do not
            # generate "Task was destroyed but it is pending" warnings.
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)

            _terminate_process(current_process)
            await _remove_stdio_session(session_id)

            latency_ms = (time.monotonic() - start) * 1000
            log_request(
                user="stdio",
                source_ip="localhost",
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


# --------------------------------------------------------------------------- #
#  Message handler                                                             #
# --------------------------------------------------------------------------- #

async def handle_stdio_message(
    request: Request,
    destination: str,
    session_id: str,
) -> Response:
    """Handle ``POST /{destination}/message`` for a stdio-backed destination.

    Enqueues the request body onto the session's stdin queue.  Returns 202 on
    success, 400 on malformed session_id, or 404 if the session is not active.
    """
    if not _UUID4_RE.match(session_id):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid session_id format"},
        )

    session = get_stdio_session(session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session not found: {session_id}"},
        )

    body = await request.body()

    # JSON-RPC over stdio requires newline-terminated frames
    if not body.endswith(b"\n"):
        body = body + b"\n"

    await session.stdin_queue.put(body)
    return Response(status_code=202)
