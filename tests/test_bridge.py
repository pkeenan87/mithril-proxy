"""Tests for the stdio-to-SSE bridge."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Valid UUID4-format strings used across tests (must match _UUID4_RE in bridge.py)
_UUID_A = "00000000-0000-4000-8000-000000000001"
_UUID_B = "00000000-0000-4000-8000-000000000002"

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def reset_bridge_state():
    """Clear module-level bridge state between tests to prevent leakage."""
    import mithril_proxy.bridge as bridge
    bridge._stdio_sessions.clear()
    bridge._stdio_lock = None
    yield
    bridge._stdio_sessions.clear()
    bridge._stdio_lock = None


@pytest.fixture()
def tmp_log(tmp_path):
    return tmp_path / "bridge.log"


@pytest.fixture()
def setup_logger(tmp_log):
    """Wire up a real logger so bridge code can call get_logger()."""
    import mithril_proxy.logger as log_mod

    logger = logging.getLogger("mithril_proxy_test_bridge")
    logger.handlers.clear()
    handler = logging.FileHandler(str(tmp_log), mode="a")
    handler.setFormatter(log_mod._JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    log_mod._logger = logger
    yield logger
    log_mod._logger = None


@pytest.fixture()
def sse_dest_config():
    """A minimal SSE-type DestinationConfig."""
    from mithril_proxy.config import DestinationConfig
    return DestinationConfig(type="sse", url="http://upstream.example.com")


@pytest.fixture()
def stdio_dest_config():
    """A stdio DestinationConfig using python3 (always available)."""
    from mithril_proxy.config import DestinationConfig
    return DestinationConfig(
        type="stdio",
        command="python3 -c \"import sys; [print(l.rstrip(), flush=True) for l in sys.stdin]\"",
    )


@pytest.fixture()
def instant_exit_config():
    """A stdio DestinationConfig whose process exits immediately."""
    from mithril_proxy.config import DestinationConfig
    return DestinationConfig(type="stdio", command="python3 -c \"\"")


@pytest.fixture()
def app_with_stdio(tmp_log, tmp_path):
    """FastAPI app with one stdio and one SSE destination."""
    # Write a script file — avoids shell metacharacters that load_config rejects.
    echo_script = tmp_path / "echo_server.py"
    echo_script.write_text(
        "import sys\n"
        "for line in sys.stdin:\n"
        "    print(line.rstrip(), flush=True)\n"
    )

    destinations_yml = tmp_path / "destinations.yml"
    destinations_yml.write_text(
        "destinations:\n"
        "  echostdio:\n"
        "    type: stdio\n"
        f"    command: python3 {echo_script}\n"
        "  ssedest:\n"
        "    url: http://upstream.example.com\n"
    )

    import mithril_proxy.config as cfg
    import mithril_proxy.logger as log_mod

    cfg.load_config(path=destinations_yml)

    logger = logging.getLogger("mithril_proxy")
    logger.handlers.clear()
    handler = logging.FileHandler(str(tmp_log), mode="a")
    handler.setFormatter(log_mod._JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    log_mod._logger = logger

    from mithril_proxy.main import app as fastapi_app
    return fastapi_app


# --------------------------------------------------------------------------- #
# TestValidateStdioCommands
# --------------------------------------------------------------------------- #

class TestValidateStdioCommands:
    def test_valid_python3_command_passes(self):
        from mithril_proxy.bridge import validate_stdio_commands
        from mithril_proxy.config import DestinationConfig

        configs = {"myserver": DestinationConfig(type="stdio", command="python3 --version")}
        validate_stdio_commands(configs)  # must not raise

    def test_missing_executable_raises(self):
        from mithril_proxy.bridge import validate_stdio_commands
        from mithril_proxy.config import DestinationConfig

        configs = {
            "bad": DestinationConfig(type="stdio", command="this-binary-definitely-does-not-exist --flag")
        }
        with pytest.raises(ValueError, match="not found on PATH"):
            validate_stdio_commands(configs)

    def test_sse_destinations_are_skipped(self):
        from mithril_proxy.bridge import validate_stdio_commands
        from mithril_proxy.config import DestinationConfig

        # SSE entry has no command — validation must skip it entirely
        configs = {"mysse": DestinationConfig(type="sse", url="http://example.com")}
        validate_stdio_commands(configs)  # must not raise


# --------------------------------------------------------------------------- #
# TestHandleStdioMessage
# --------------------------------------------------------------------------- #

class TestHandleStdioMessage:
    @pytest.mark.asyncio
    async def test_unknown_session_returns_404(self, setup_logger):
        from mithril_proxy.bridge import handle_stdio_message

        request = MagicMock()
        request.body = AsyncMock(return_value=b'{"jsonrpc":"2.0","method":"ping","id":1}')

        # _UUID_A is a valid UUID4 format but no session is registered for it
        resp = await handle_stdio_message(request, "testdest", _UUID_A)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_session_id_returns_400(self, setup_logger):
        from mithril_proxy.bridge import handle_stdio_message

        request = MagicMock()
        request.body = AsyncMock(return_value=b'{"jsonrpc":"2.0","method":"ping","id":1}')

        resp = await handle_stdio_message(request, "testdest", "not-a-uuid")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_message_enqueued_and_returns_202(self, setup_logger):
        from mithril_proxy.bridge import StdioSession, _stdio_sessions, handle_stdio_message

        fake_queue: asyncio.Queue = asyncio.Queue()
        proc = MagicMock()
        proc.returncode = None

        session = StdioSession(
            session_id=_UUID_A,
            destination="testdest",
            process=proc,
            stdin_queue=fake_queue,
        )
        _stdio_sessions[_UUID_A] = session

        request = MagicMock()
        body = b'{"jsonrpc":"2.0","method":"ping","id":1}'
        request.body = AsyncMock(return_value=body)

        resp = await handle_stdio_message(request, "testdest", _UUID_A)
        assert resp.status_code == 202

        enqueued = await fake_queue.get()
        assert enqueued == body + b"\n"

    @pytest.mark.asyncio
    async def test_newline_appended_if_missing(self, setup_logger):
        from mithril_proxy.bridge import StdioSession, _stdio_sessions, handle_stdio_message

        fake_queue: asyncio.Queue = asyncio.Queue()
        proc = MagicMock()
        proc.returncode = None

        session = StdioSession(
            session_id=_UUID_B,
            destination="testdest",
            process=proc,
            stdin_queue=fake_queue,
        )
        _stdio_sessions[_UUID_B] = session

        request = MagicMock()
        # Body already has newline — should not double-add
        request.body = AsyncMock(return_value=b'{"method":"ping"}\n')

        resp = await handle_stdio_message(request, "testdest", _UUID_B)
        assert resp.status_code == 202

        enqueued = await fake_queue.get()
        assert enqueued == b'{"method":"ping"}\n'
        assert enqueued.count(b"\n") == 1


# --------------------------------------------------------------------------- #
# TestHandleStdioSseEndpointEvent
# --------------------------------------------------------------------------- #

class TestHandleStdioSseEndpointEvent:
    @pytest.mark.asyncio
    async def test_first_event_is_endpoint(self, setup_logger, stdio_dest_config):
        """The first SSE event emitted must be 'event: endpoint'."""
        from mithril_proxy.bridge import handle_stdio_sse

        request = MagicMock()
        response = await handle_stdio_sse(request, "testdest", stdio_dest_config, {})

        chunks: list[bytes] = []

        async def collect():
            async for chunk in response.body_iterator:
                chunks.append(chunk)
                if b"event: endpoint" in chunk:
                    break

        await asyncio.wait_for(collect(), timeout=10.0)

        combined = b"".join(chunks)
        assert b"event: endpoint" in combined
        assert b"testdest/message" in combined

    @pytest.mark.asyncio
    async def test_stdout_line_becomes_data_event(self, setup_logger, stdio_dest_config):
        """A line written to subprocess stdout appears as a 'data:' SSE event."""
        from mithril_proxy.bridge import (
            _stdio_sessions,
            get_stdio_session,
            handle_stdio_sse,
        )

        request = MagicMock()
        response = await handle_stdio_sse(request, "testdest", stdio_dest_config, {})

        endpoint_seen = asyncio.Event()
        data_chunks: list[bytes] = []
        session_id_holder: list[str] = []

        async def collect():
            async for chunk in response.body_iterator:
                data_chunks.append(chunk)
                if b"event: endpoint" in chunk:
                    # Parse session_id from the data line
                    for piece in chunk.split(b"\n"):
                        if piece.startswith(b"data: ") and b"session_id=" in piece:
                            sid = piece.split(b"session_id=")[1].strip().decode()
                            session_id_holder.append(sid)
                            endpoint_seen.set()
                if b"data: " in chunk and b"event:" not in chunk and session_id_holder:
                    break

        # Start collection, send a message once endpoint is known, then wait
        collect_task = asyncio.create_task(collect())

        try:
            await asyncio.wait_for(endpoint_seen.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            collect_task.cancel()
            pytest.fail("Timed out waiting for endpoint event")

        if session_id_holder:
            session = get_stdio_session(session_id_holder[0])
            if session:
                await session.stdin_queue.put(b"hello from test\n")

        try:
            await asyncio.wait_for(collect_task, timeout=5.0)
        except asyncio.TimeoutError:
            collect_task.cancel()

        combined = b"".join(data_chunks)
        assert b"data: " in combined


# --------------------------------------------------------------------------- #
# TestSubprocessRetry
# --------------------------------------------------------------------------- #

class TestSubprocessRetry:
    @pytest.mark.asyncio
    async def test_subprocess_exit_triggers_restarts(self, setup_logger, instant_exit_config):
        """A subprocess that exits immediately is restarted and eventually emits error event."""
        from mithril_proxy.bridge import handle_stdio_sse

        request = MagicMock()

        # Speed up the test by patching asyncio.sleep
        sleep_calls: list[float] = []

        async def fast_sleep(delay):
            sleep_calls.append(delay)

        with patch("mithril_proxy.bridge.asyncio.sleep", side_effect=fast_sleep):
            response = await handle_stdio_sse(request, "testdest", instant_exit_config, {})

            chunks: list[bytes] = []

            async def collect():
                async for chunk in response.body_iterator:
                    chunks.append(chunk)

            await asyncio.wait_for(collect(), timeout=15.0)

        combined = b"".join(chunks)
        assert b"event: error" in combined
        assert b"subprocess unavailable" in combined
        # Should have retried 3 times (delays list has 3 entries)
        assert len(sleep_calls) == len([0.5, 1.0, 2.0])

    @pytest.mark.asyncio
    async def test_after_max_retries_destination_returns_503_via_app(
        self, app_with_stdio, setup_logger
    ):
        """After all restarts are exhausted, the error event is 503-class content."""
        from mithril_proxy.bridge import handle_stdio_sse
        from mithril_proxy.config import DestinationConfig

        instant_exit = DestinationConfig(type="stdio", command="python3 -c \"\"")
        request = MagicMock()

        async def fast_sleep(delay):
            pass

        with patch("mithril_proxy.bridge.asyncio.sleep", side_effect=fast_sleep):
            response = await handle_stdio_sse(request, "testdest", instant_exit, {})
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        combined = b"".join(chunks)
        assert b"event: error" in combined


# --------------------------------------------------------------------------- #
# TestSubprocessStderr
# --------------------------------------------------------------------------- #

class TestSubprocessStderr:
    @pytest.mark.asyncio
    async def test_stderr_logged_not_forwarded(self, setup_logger, tmp_log):
        """Subprocess stderr must appear in the log file and NOT in the SSE stream."""
        from mithril_proxy.bridge import handle_stdio_sse
        from mithril_proxy.config import DestinationConfig

        # This process writes to stderr then exits immediately
        stderr_config = DestinationConfig(
            type="stdio",
            command='python3 -c "import sys; sys.stderr.write(\'ERR_MARKER\\n\'); sys.stderr.flush()"',
        )
        request = MagicMock()

        async def fast_sleep(delay):
            pass

        with patch("mithril_proxy.bridge.asyncio.sleep", side_effect=fast_sleep):
            response = await handle_stdio_sse(request, "testdest", stderr_config, {})
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        sse_output = b"".join(chunks)

        # Stderr must NOT appear in the SSE stream
        assert b"ERR_MARKER" not in sse_output

        # Stderr MUST appear in the log file
        log_content = tmp_log.read_text()
        assert "ERR_MARKER" in log_content


# --------------------------------------------------------------------------- #
# TestShutdown
# --------------------------------------------------------------------------- #

class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_terminates_processes(self, setup_logger):
        """shutdown_all_stdio sends SIGTERM to all registered sessions."""
        from mithril_proxy.bridge import StdioSession, _stdio_sessions, shutdown_all_stdio

        # Spawn a real long-running process
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.returncode is None

        session = StdioSession(
            session_id="shutdown-test",
            destination="testdest",
            process=proc,
        )
        _stdio_sessions["shutdown-test"] = session

        await shutdown_all_stdio()

        # Process should be dead
        assert proc.returncode is not None
        assert "shutdown-test" not in _stdio_sessions


# --------------------------------------------------------------------------- #
# TestSseDestinationUnchanged
# --------------------------------------------------------------------------- #

class TestSseDestinationUnchanged:
    def test_sse_destination_still_returns_404_for_unknown(self, app_with_stdio):
        """SSE-type destinations continue to return 404 for unknown destinations."""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_stdio, raise_server_exceptions=False)
        resp = client.get("/nonexistent/sse")
        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["error"]

    def test_sse_destination_message_missing_session_returns_400(self, app_with_stdio):
        """POST to SSE-type destination without session_id returns 400."""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_stdio, raise_server_exceptions=False)
        resp = client.post("/ssedest/message", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_stdio_destination_message_unknown_session_returns_404(
        self, app_with_stdio
    ):
        """POST to a stdio destination with an unknown session_id returns 404."""
        transport = httpx.ASGITransport(app=app_with_stdio)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/echostdio/message?session_id={_UUID_A}",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
        assert resp.status_code == 404
