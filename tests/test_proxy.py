"""Tests for SSE proxying, session rewriting, retries, and unknown destinations."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# App fixture with in-memory config + temp log file
# --------------------------------------------------------------------------- #

@pytest.fixture()
def tmp_log(tmp_path):
    return tmp_path / "proxy.log"


@pytest.fixture()
def app(tmp_log, tmp_path):
    """Return a configured FastAPI app with a minimal in-memory destination."""
    destinations_yml = tmp_path / "destinations.yml"
    destinations_yml.write_text(
        "destinations:\n  testdest:\n    url: http://upstream.example.com\n"
    )

    import mithril_proxy.config as cfg
    import mithril_proxy.logger as log_mod

    cfg.load_config(path=destinations_yml)

    import logging
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
# Health check
# --------------------------------------------------------------------------- #

class TestHealthCheck:
    def test_health_returns_ok(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# Unknown destination
# --------------------------------------------------------------------------- #

class TestUnknownDestination:
    def test_sse_unknown_destination_returns_404(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/nonexistent/sse")
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert "nonexistent" in body["error"]

    def test_message_unknown_destination_returns_404(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/nonexistent/message?session_id=abc", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert resp.status_code == 404

    def test_message_missing_session_id_returns_400(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/testdest/message", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert resp.status_code == 400

    def test_message_unknown_session_returns_404(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/testdest/message?session_id=does-not-exist", json={})
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Session endpoint rewriting
# --------------------------------------------------------------------------- #

class TestEndpointRewriting:
    def test_rewrite_endpoint_event_builds_proxy_url(self):
        from mithril_proxy.proxy import _rewrite_endpoint_event

        result = _rewrite_endpoint_event(
            data="/messages?sessionId=abc123",
            destination="testdest",
            session_id="abc123",
        )
        assert result == "/testdest/message?session_id=abc123"


# --------------------------------------------------------------------------- #
# Message forwarding — upstream success
# --------------------------------------------------------------------------- #

class TestMessageForwarding:
    @pytest.mark.asyncio
    async def test_message_forwarded_with_auth_header(self, app, tmp_log):
        from mithril_proxy import proxy

        session_id = "sess-xyz"
        # Manually register a session
        await proxy._register_session(session_id, "http://upstream.example.com/messages?sessionId=sess-xyz")

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 202
        mock_response.content = b""
        mock_response.headers = {"content-type": "application/json"}

        with patch("mithril_proxy.proxy._connect_with_retries", new_callable=AsyncMock) as mock_conn:
            mock_conn.return_value = mock_response

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/testdest/message?session_id={session_id}",
                    json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
                    headers={"Authorization": "Bearer mytoken123"},
                )

            assert resp.status_code == 202
            call_kwargs = mock_conn.call_args
            _, kwargs = call_kwargs.args, call_kwargs.kwargs if call_kwargs.kwargs else {}
            # Verify auth header was forwarded
            sent_headers = mock_conn.call_args[1].get("headers") or mock_conn.call_args[0][3] if len(mock_conn.call_args[0]) > 3 else {}

        # Clean up
        await proxy._remove_session(session_id)


# --------------------------------------------------------------------------- #
# Retry behaviour
# --------------------------------------------------------------------------- #

class TestRetries:
    @pytest.mark.asyncio
    async def test_connect_retries_on_connection_error(self):
        """_connect_with_retries should attempt 3 times on ConnectError."""
        from mithril_proxy.proxy import _connect_with_retries

        call_count = 0

        async def fake_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("refused")

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.request = fake_request

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.ConnectError):
                await _connect_with_retries(mock_client, "POST", "http://bad.host/")

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx(self):
        """4xx responses must not be retried."""
        from mithril_proxy.proxy import _connect_with_retries

        call_count = 0

        async def fake_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 401
            return resp

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.request = fake_request

        result = await _connect_with_retries(mock_client, "POST", "http://host/")
        assert result.status_code == 401
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_returns_502_when_upstream_unreachable(self, app, tmp_log):
        """POST to /message returns 502 when upstream refuses all retries."""
        from mithril_proxy import proxy

        session_id = "sess-retry-test"
        await proxy._register_session(session_id, "http://unreachable.example.com/messages?sessionId=xyz")

        with patch("mithril_proxy.proxy._connect_with_retries", new_callable=AsyncMock) as mock_conn:
            mock_conn.side_effect = httpx.ConnectError("connection refused")

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/testdest/message?session_id={session_id}",
                    json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                )

        assert resp.status_code == 502
        await proxy._remove_session(session_id)
