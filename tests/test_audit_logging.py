"""Tests for MCP message audit logging.

Verifies request_body, response_body, rpc_id, truncation at 32 KB, and the
AUDIT_LOG_BODIES toggle across SSE proxy and stdio bridge paths.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_UUID_A = "00000000-0000-4000-8000-000000000001"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _read_log_lines(path: Path) -> list[dict]:
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def tmp_log(tmp_path):
    return tmp_path / "audit.log"


@pytest.fixture()
def setup_logger(tmp_log):
    """Wire up a real JSON logger so log_request() writes to tmp_log."""
    import mithril_proxy.logger as log_mod

    logger = logging.getLogger(f"mithril_proxy_audit_{id(tmp_log)}")
    logger.handlers.clear()
    handler = logging.FileHandler(str(tmp_log), mode="a")
    handler.setFormatter(log_mod._JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    original = log_mod._logger
    log_mod._logger = logger
    yield logger
    log_mod._logger = original
    handler.close()


@pytest.fixture(autouse=True)
def reset_bridge_state():
    """Prevent _stdio_sessions leaking between tests."""
    import mithril_proxy.bridge as bridge
    bridge._stdio_sessions.clear()
    bridge._stdio_lock = None
    yield
    bridge._stdio_sessions.clear()
    bridge._stdio_lock = None


@pytest.fixture()
def app(tmp_log, tmp_path):
    """FastAPI app with one SSE destination wired to tmp_log."""
    destinations_yml = tmp_path / "destinations.yml"
    destinations_yml.write_text(
        "destinations:\n  testdest:\n    url: http://upstream.example.com\n"
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
# TestLogRequestAuditFields — direct log_request() unit tests
# --------------------------------------------------------------------------- #

class TestLogRequestAuditFields:
    def test_request_body_present_when_provided(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method="tools/list",
            status_code=200,
            latency_ms=1.0,
            request_body='{"jsonrpc":"2.0","method":"tools/list","id":1}',
        )
        lines = _read_log_lines(tmp_log)
        assert len(lines) == 1
        assert "request_body" in lines[0]
        assert '"method":"tools/list"' in lines[0]["request_body"]

    def test_response_body_present_when_provided(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method=None,
            status_code=200,
            latency_ms=1.0,
            response_body='{"jsonrpc":"2.0","result":{"tools":[]},"id":1}',
        )
        lines = _read_log_lines(tmp_log)
        assert "response_body" in lines[0]
        assert "tools" in lines[0]["response_body"]

    def test_rpc_id_present_when_provided(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method="tools/list",
            status_code=200,
            latency_ms=1.0,
            rpc_id=42,
        )
        lines = _read_log_lines(tmp_log)
        assert lines[0]["rpc_id"] == 42

    def test_rpc_id_absent_when_none(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method=None,
            status_code=200,
            latency_ms=1.0,
        )
        lines = _read_log_lines(tmp_log)
        assert "rpc_id" not in lines[0]

    def test_bodies_absent_when_not_provided(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method=None,
            status_code=200,
            latency_ms=1.0,
        )
        lines = _read_log_lines(tmp_log)
        assert "request_body" not in lines[0]
        assert "response_body" not in lines[0]


# --------------------------------------------------------------------------- #
# TestTruncation
# --------------------------------------------------------------------------- #

class TestTruncation:
    def test_large_request_body_is_truncated(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod

        large_body = "x" * 40_000  # 40 KB > 32 KB limit
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method=None,
            status_code=200,
            latency_ms=1.0,
            request_body=large_body,
        )
        lines = _read_log_lines(tmp_log)
        entry = lines[0]
        assert entry.get("truncated") is True
        assert len(entry["request_body"]) == 32_768

    def test_large_response_body_is_truncated(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod

        large_body = "y" * 40_000
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method=None,
            status_code=200,
            latency_ms=1.0,
            response_body=large_body,
        )
        lines = _read_log_lines(tmp_log)
        entry = lines[0]
        assert entry.get("truncated") is True
        assert len(entry["response_body"]) == 32_768

    def test_body_at_exact_limit_not_truncated(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod

        exact_body = "z" * 32_768
        log_mod.log_request(
            user="anon",
            source_ip="127.0.0.1",
            destination="testdest",
            mcp_method=None,
            status_code=200,
            latency_ms=1.0,
            request_body=exact_body,
        )
        lines = _read_log_lines(tmp_log)
        entry = lines[0]
        assert "truncated" not in entry
        assert len(entry["request_body"]) == 32_768


# --------------------------------------------------------------------------- #
# TestAuditToggle
# --------------------------------------------------------------------------- #

class TestAuditToggle:
    def test_audit_disabled_omits_body_fields(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod

        with patch.dict("os.environ", {"AUDIT_LOG_BODIES": "false"}):
            log_mod.log_request(
                user="anon",
                source_ip="127.0.0.1",
                destination="testdest",
                mcp_method="tools/list",
                status_code=200,
                latency_ms=1.0,
                request_body='{"jsonrpc":"2.0","method":"tools/list","id":1}',
                response_body='{"jsonrpc":"2.0","result":{},"id":1}',
                rpc_id=1,
            )
        lines = _read_log_lines(tmp_log)
        entry = lines[0]
        assert "request_body" not in entry
        assert "response_body" not in entry
        # rpc_id is always logged regardless of toggle
        assert entry["rpc_id"] == 1

    def test_audit_disabled_with_zero_value(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod

        with patch.dict("os.environ", {"AUDIT_LOG_BODIES": "0"}):
            log_mod.log_request(
                user="anon",
                source_ip="127.0.0.1",
                destination="testdest",
                mcp_method=None,
                status_code=200,
                latency_ms=1.0,
                request_body="some body",
            )
        lines = _read_log_lines(tmp_log)
        assert "request_body" not in lines[0]

    def test_audit_enabled_by_default(self, setup_logger, tmp_log):
        import mithril_proxy.logger as log_mod

        # Ensure no override is set
        env = {k: v for k, v in __import__("os").environ.items() if k != "AUDIT_LOG_BODIES"}
        with patch.dict("os.environ", env, clear=True):
            log_mod.log_request(
                user="anon",
                source_ip="127.0.0.1",
                destination="testdest",
                mcp_method=None,
                status_code=200,
                latency_ms=1.0,
                request_body="hello",
            )
        lines = _read_log_lines(tmp_log)
        assert "request_body" in lines[0]


# --------------------------------------------------------------------------- #
# TestSseProxyAuditLogging — integration via handle_message()
# --------------------------------------------------------------------------- #

class TestSseProxyAuditLogging:
    @pytest.mark.asyncio
    async def test_sse_post_logs_request_body(self, app, tmp_log):
        from mithril_proxy import proxy

        session_id = "sess-audit-1"
        await proxy._register_session(session_id, "http://upstream.example.com/msg?sessionId=1")

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 202
        mock_response.content = b'{"accepted":true}'
        mock_response.headers = {"content-type": "application/json"}

        with patch("mithril_proxy.proxy._connect_with_retries", new_callable=AsyncMock) as mock_conn:
            mock_conn.return_value = mock_response
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post(
                    f"/testdest/message?session_id={session_id}",
                    content=b'{"jsonrpc":"2.0","method":"tools/list","id":7}',
                    headers={"Content-Type": "application/json"},
                )

        await proxy._remove_session(session_id)

        lines = _read_log_lines(tmp_log)
        request_entries = [l for l in lines if l.get("mcp_method") == "tools/list"]
        assert request_entries, "Expected a log entry with mcp_method=tools/list"
        entry = request_entries[-1]
        assert "request_body" in entry
        assert "tools/list" in entry["request_body"]

    @pytest.mark.asyncio
    async def test_sse_post_logs_response_body(self, app, tmp_log):
        from mithril_proxy import proxy

        session_id = "sess-audit-2"
        await proxy._register_session(session_id, "http://upstream.example.com/msg?sessionId=2")

        upstream_content = b'{"jsonrpc":"2.0","result":{"tools":[]},"id":7}'
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.content = upstream_content
        mock_response.headers = {"content-type": "application/json"}

        with patch("mithril_proxy.proxy._connect_with_retries", new_callable=AsyncMock) as mock_conn:
            mock_conn.return_value = mock_response
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post(
                    f"/testdest/message?session_id={session_id}",
                    content=b'{"jsonrpc":"2.0","method":"tools/list","id":7}',
                    headers={"Content-Type": "application/json"},
                )

        await proxy._remove_session(session_id)

        lines = _read_log_lines(tmp_log)
        request_entries = [l for l in lines if "response_body" in l]
        assert request_entries, "Expected at least one entry with response_body"
        entry = request_entries[-1]
        assert "tools" in entry["response_body"]

    @pytest.mark.asyncio
    async def test_sse_post_logs_rpc_id(self, app, tmp_log):
        from mithril_proxy import proxy

        session_id = "sess-audit-3"
        await proxy._register_session(session_id, "http://upstream.example.com/msg?sessionId=3")

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 202
        mock_response.content = b""
        mock_response.headers = {}

        with patch("mithril_proxy.proxy._connect_with_retries", new_callable=AsyncMock) as mock_conn:
            mock_conn.return_value = mock_response
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post(
                    f"/testdest/message?session_id={session_id}",
                    content=b'{"jsonrpc":"2.0","method":"ping","id":99}',
                    headers={"Content-Type": "application/json"},
                )

        await proxy._remove_session(session_id)

        lines = _read_log_lines(tmp_log)
        matching = [l for l in lines if l.get("rpc_id") == 99]
        assert matching, "Expected a log entry with rpc_id=99"

    @pytest.mark.asyncio
    async def test_malformed_body_still_logged_no_exception(self, app, tmp_log):
        """Non-JSON body: no exception; raw request_body is logged; mcp_method absent."""
        from mithril_proxy import proxy

        session_id = "sess-audit-4"
        await proxy._register_session(session_id, "http://upstream.example.com/msg?sessionId=4")

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 202
        mock_response.content = b""
        mock_response.headers = {}

        with patch("mithril_proxy.proxy._connect_with_retries", new_callable=AsyncMock) as mock_conn:
            mock_conn.return_value = mock_response
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # Send raw binary that is not valid JSON
                resp = await client.post(
                    f"/testdest/message?session_id={session_id}",
                    content=b"not valid json at all",
                    headers={"Content-Type": "application/octet-stream"},
                )
            assert resp.status_code == 202

        await proxy._remove_session(session_id)

        # No exception was raised — just verify the log entry exists with the raw body
        lines = _read_log_lines(tmp_log)
        assert lines, "Expected at least one log entry"
        entry = lines[-1]
        assert entry["request_body"] == "not valid json at all"
        assert entry.get("mcp_method") is None


# --------------------------------------------------------------------------- #
# TestStdioAuditLogging — integration via bridge functions
# --------------------------------------------------------------------------- #

class TestStdioAuditLogging:
    @pytest.mark.asyncio
    async def test_handle_stdio_message_logs_request_body(self, setup_logger, tmp_log):
        """handle_stdio_message() must log request_body."""
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
        body = b'{"jsonrpc":"2.0","method":"tools/call","id":5}'
        request.body = AsyncMock(return_value=body)

        resp = await handle_stdio_message(request, "testdest", _UUID_A)
        assert resp.status_code == 202

        lines = _read_log_lines(tmp_log)
        matching = [l for l in lines if "request_body" in l]
        assert matching, "Expected a log entry with request_body"
        entry = matching[-1]
        assert "tools/call" in entry["request_body"]
        assert entry.get("mcp_method") == "tools/call"
        assert entry.get("rpc_id") == 5

    @pytest.mark.asyncio
    async def test_stdout_reader_logs_response_body(self, setup_logger, tmp_log):
        """_stdout_reader() must log each stdout line as response_body."""
        from mithril_proxy.bridge import _stdout_reader

        line_data = b'{"jsonrpc":"2.0","result":{"content":"ok"},"id":3}\n'
        call_count = 0

        async def mock_readline():
            nonlocal call_count
            call_count += 1
            return line_data if call_count == 1 else b""

        mock_stdout = MagicMock()
        mock_stdout.readline = mock_readline
        mock_process = MagicMock()
        mock_process.stdout = mock_stdout

        out_queue: asyncio.Queue = asyncio.Queue()
        await _stdout_reader(mock_process, out_queue, "testdest", _UUID_A)

        lines = _read_log_lines(tmp_log)
        response_entries = [l for l in lines if "response_body" in l]
        assert response_entries, "Expected a log entry with response_body"
        entry = response_entries[0]
        assert "content" in entry["response_body"]
        assert entry.get("rpc_id") == 3

    @pytest.mark.asyncio
    async def test_stdout_reader_separate_entry_per_line(self, setup_logger, tmp_log):
        """Each stdout line must produce exactly one separate log entry."""
        from mithril_proxy.bridge import _stdout_reader

        lines_to_emit = [
            b'{"jsonrpc":"2.0","result":{},"id":1}\n',
            b'{"jsonrpc":"2.0","result":{},"id":2}\n',
        ]
        call_count = 0

        async def mock_readline():
            nonlocal call_count
            if call_count < len(lines_to_emit):
                data = lines_to_emit[call_count]
                call_count += 1
                return data
            return b""

        mock_stdout = MagicMock()
        mock_stdout.readline = mock_readline
        mock_process = MagicMock()
        mock_process.stdout = mock_stdout

        out_queue: asyncio.Queue = asyncio.Queue()
        await _stdout_reader(mock_process, out_queue, "testdest", _UUID_A)

        log_lines = _read_log_lines(tmp_log)
        response_entries = [l for l in log_lines if "response_body" in l]
        assert len(response_entries) == 2
        rpc_ids = {e.get("rpc_id") for e in response_entries}
        assert rpc_ids == {1, 2}

    @pytest.mark.asyncio
    async def test_stdout_reader_non_json_line_no_rpc_id(self, setup_logger, tmp_log):
        """Non-JSON stdout lines are logged without rpc_id."""
        from mithril_proxy.bridge import _stdout_reader

        call_count = 0

        async def mock_readline():
            nonlocal call_count
            call_count += 1
            return b"plain text output\n" if call_count == 1 else b""

        mock_stdout = MagicMock()
        mock_stdout.readline = mock_readline
        mock_process = MagicMock()
        mock_process.stdout = mock_stdout

        out_queue: asyncio.Queue = asyncio.Queue()
        await _stdout_reader(mock_process, out_queue, "testdest", _UUID_A)

        log_lines = _read_log_lines(tmp_log)
        response_entries = [l for l in log_lines if "response_body" in l]
        assert response_entries
        entry = response_entries[0]
        assert entry["response_body"] == "plain text output"
        assert "rpc_id" not in entry

    @pytest.mark.asyncio
    async def test_stdio_audit_disabled_omits_bodies(self, setup_logger, tmp_log):
        """With AUDIT_LOG_BODIES=false, stdio log entries omit request_body."""
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
        body = b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
        request.body = AsyncMock(return_value=body)

        with patch.dict("os.environ", {"AUDIT_LOG_BODIES": "false"}):
            resp = await handle_stdio_message(request, "testdest", _UUID_A)

        assert resp.status_code == 202

        log_lines = _read_log_lines(tmp_log)
        assert log_lines, "Expected at least one log entry"
        entry = log_lines[-1]
        assert "request_body" not in entry
        assert "response_body" not in entry
        # rpc_id and mcp_method still logged
        assert entry.get("rpc_id") == 1
        assert entry.get("mcp_method") == "tools/list"
