"""Tests for streamable HTTP transport (POST/GET /{destination}/mcp)."""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def tmp_log(tmp_path):
    return tmp_path / "proxy.log"


@pytest.fixture()
def app(tmp_log, tmp_path):
    """Return a configured FastAPI app with a streamable_http destination."""
    destinations_yml = tmp_path / "destinations.yml"
    destinations_yml.write_text(
        "destinations:\n"
        "  mcpdest:\n"
        "    type: streamable_http\n"
        "    url: http://upstream.example.com/mcp\n"
        "  ssedest:\n"
        "    type: sse\n"
        "    url: http://upstream.example.com/sse\n"
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


def _read_log_lines(tmp_log) -> list[dict]:
    text = tmp_log.read_text()
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


def _make_mock_json_upstream(response_data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock upstream response for a JSON content-type reply."""
    mock_upstream = MagicMock()
    mock_upstream.status_code = status_code
    mock_upstream.headers = httpx.Headers({"content-type": "application/json"})
    mock_upstream.aread = AsyncMock(return_value=json.dumps(response_data).encode())
    mock_upstream.aclose = AsyncMock()
    return mock_upstream


def _make_mock_client(mock_upstream: MagicMock) -> MagicMock:
    """Build a mock httpx.AsyncClient (non-context-manager) for POST handler."""
    mock_client = MagicMock()
    mock_client.build_request = MagicMock(return_value=MagicMock())
    mock_client.send = AsyncMock(return_value=mock_upstream)
    mock_client.aclose = AsyncMock()
    return mock_client


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #

class TestConfigValidation:
    def test_streamable_http_with_url_is_valid(self, tmp_path):
        from mithril_proxy.config import load_config, get_destination
        d = tmp_path / "d.yml"
        d.write_text("destinations:\n  gh:\n    type: streamable_http\n    url: https://api.example.com/mcp\n")
        load_config(path=d)
        dest = get_destination("gh")
        assert dest is not None
        assert dest.type == "streamable_http"
        assert dest.url == "https://api.example.com/mcp"

    def test_streamable_http_missing_url_raises(self, tmp_path):
        from mithril_proxy.config import load_config
        d = tmp_path / "d.yml"
        d.write_text("destinations:\n  gh:\n    type: streamable_http\n")
        with pytest.raises(ValueError, match="requires a non-empty 'url'"):
            load_config(path=d)

    def test_unknown_type_error_mentions_streamable_http(self, tmp_path):
        from mithril_proxy.config import load_config
        d = tmp_path / "d.yml"
        d.write_text("destinations:\n  gh:\n    type: unknown\n    url: https://example.com\n")
        with pytest.raises(ValueError, match="streamable_http"):
            load_config(path=d)

    def test_non_http_scheme_raises(self, tmp_path):
        from mithril_proxy.config import load_config
        d = tmp_path / "d.yml"
        d.write_text("destinations:\n  gh:\n    type: streamable_http\n    url: file:///etc/passwd\n")
        with pytest.raises(ValueError, match="http or https"):
            load_config(path=d)

    def test_streamable_http_url_trailing_slash_stripped(self, tmp_path):
        from mithril_proxy.config import load_config, get_destination
        d = tmp_path / "d.yml"
        d.write_text("destinations:\n  gh:\n    type: streamable_http\n    url: https://api.example.com/mcp/\n")
        load_config(path=d)
        dest = get_destination("gh")
        assert dest.url == "https://api.example.com/mcp"


# --------------------------------------------------------------------------- #
# POST /mcp — routing
# --------------------------------------------------------------------------- #

class TestMcpPostRouting:
    def test_unknown_destination_returns_404(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/notexist/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert resp.status_code == 404
        assert "notexist" in resp.json()["error"]

    def test_sse_destination_returns_400(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/ssedest/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# POST /mcp — JSON upstream response
# --------------------------------------------------------------------------- #

class TestMcpPostJsonResponse:
    @pytest.mark.asyncio
    async def test_json_response_forwarded(self, app, tmp_log):
        response_data = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        mock_upstream = _make_mock_json_upstream(response_data)
        mock_client = _make_mock_client(mock_upstream)

        # Create test client BEFORE the patch so it isn't intercepted.
        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                resp = await test_client.post(
                    "/mcpdest/mcp",
                    json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
                    headers={"Authorization": "Bearer testtoken1"},
                )

        assert resp.status_code == 200
        assert resp.json() == response_data

    @pytest.mark.asyncio
    async def test_request_and_response_body_logged(self, app, tmp_log):
        response_data = {"jsonrpc": "2.0", "id": 42, "result": {}}
        mock_upstream = _make_mock_json_upstream(response_data)
        mock_client = _make_mock_client(mock_upstream)

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                await test_client.post(
                    "/mcpdest/mcp",
                    json={"jsonrpc": "2.0", "method": "tools/list", "id": 42},
                )

        lines = _read_log_lines(tmp_log)
        assert lines, "Expected at least one log line"
        entry = lines[-1]
        assert "request_body" in entry
        assert "response_body" in entry
        assert entry["rpc_id"] == 42
        assert entry["mcp_method"] == "tools/list"

    @pytest.mark.asyncio
    async def test_authorization_header_forwarded(self, app, tmp_log):
        captured_headers: dict = {}

        mock_upstream = _make_mock_json_upstream({"jsonrpc": "2.0", "id": 1, "result": {}})

        mock_client = MagicMock()
        mock_client.send = AsyncMock(return_value=mock_upstream)
        mock_client.aclose = AsyncMock()

        def build_request(method, url, headers, content):
            captured_headers.update(headers)
            return MagicMock()

        mock_client.build_request = build_request

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                await test_client.post(
                    "/mcpdest/mcp",
                    json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                    headers={"Authorization": "Bearer secrettoken"},
                )

        assert "authorization" in {k.lower() for k in captured_headers}

    @pytest.mark.asyncio
    async def test_rpc_id_from_request_logged(self, app, tmp_log):
        # Response has no id field — rpc_id should come from request
        mock_upstream = _make_mock_json_upstream({"jsonrpc": "2.0", "result": {}})
        mock_client = _make_mock_client(mock_upstream)

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                await test_client.post(
                    "/mcpdest/mcp",
                    json={"jsonrpc": "2.0", "method": "initialize", "id": 99},
                )

        lines = _read_log_lines(tmp_log)
        entry = lines[-1]
        assert entry["rpc_id"] == 99

    @pytest.mark.asyncio
    async def test_mcp_method_logged(self, app, tmp_log):
        mock_upstream = _make_mock_json_upstream({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_client = _make_mock_client(mock_upstream)

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                await test_client.post(
                    "/mcpdest/mcp",
                    json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
                )

        lines = _read_log_lines(tmp_log)
        assert lines[-1]["mcp_method"] == "tools/call"


# --------------------------------------------------------------------------- #
# POST /mcp — upstream unreachable
# --------------------------------------------------------------------------- #

class TestMcpPostUpstreamUnreachable:
    @pytest.mark.asyncio
    async def test_connect_error_returns_502(self, app, tmp_log):
        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client.aclose = AsyncMock()

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                resp = await test_client.post(
                    "/mcpdest/mcp",
                    json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                )

        assert resp.status_code == 502
        body = resp.json()
        assert "error" in body

        lines = _read_log_lines(tmp_log)
        entry = lines[-1]
        assert entry["status_code"] == 502
        assert "error" in entry


# --------------------------------------------------------------------------- #
# POST /mcp — AUDIT_LOG_BODIES=false
# --------------------------------------------------------------------------- #

class TestMcpPostAuditLogBodiesFalse:
    @pytest.mark.asyncio
    async def test_bodies_omitted_when_audit_disabled(self, app, tmp_log, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_BODIES", "false")

        mock_upstream = _make_mock_json_upstream({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_client = _make_mock_client(mock_upstream)

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                await test_client.post(
                    "/mcpdest/mcp",
                    json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
                )

        lines = _read_log_lines(tmp_log)
        assert lines, "Expected at least one log line"
        entry = lines[-1]
        assert "request_body" not in entry
        assert "response_body" not in entry
        # Other fields still present
        assert entry["mcp_method"] == "tools/list"
        assert entry["rpc_id"] == 1


# --------------------------------------------------------------------------- #
# GET /mcp — routing
# --------------------------------------------------------------------------- #

class TestMcpGetRouting:
    def test_unknown_destination_returns_404(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/notexist/mcp")
        assert resp.status_code == 404

    def test_sse_destination_returns_400(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ssedest/mcp")
        assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# GET /mcp — SSE streaming
# --------------------------------------------------------------------------- #

class TestMcpGetStreaming:
    @pytest.mark.asyncio
    async def test_sse_chunks_forwarded(self, app, tmp_log):
        sse_bytes = b"event: endpoint\ndata: /mcp\n\n"

        async def fake_aiter_bytes():
            yield sse_bytes

        mock_upstream = MagicMock()
        mock_upstream.status_code = 200
        mock_upstream.aiter_bytes = fake_aiter_bytes
        mock_upstream.aclose = AsyncMock()
        mock_upstream.aread = AsyncMock(return_value=b"")

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(return_value=mock_upstream)
        mock_client.aclose = AsyncMock()

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                resp = await test_client.get("/mcpdest/mcp")

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert sse_bytes in resp.content

    @pytest.mark.asyncio
    async def test_upstream_4xx_forwarded(self, app, tmp_log):
        mock_upstream = MagicMock()
        mock_upstream.status_code = 401
        mock_upstream.aread = AsyncMock(return_value=b'{"error":"unauthorized"}')
        mock_upstream.aclose = AsyncMock()

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(return_value=mock_upstream)
        mock_client.aclose = AsyncMock()

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                resp = await test_client.get("/mcpdest/mcp")

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_connect_error_returns_502(self, app, tmp_log):
        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.aclose = AsyncMock()

        transport = httpx.ASGITransport(app=app)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with patch("mithril_proxy.proxy.httpx.AsyncClient", return_value=mock_client):
            async with test_client:
                resp = await test_client.get("/mcpdest/mcp")

        assert resp.status_code == 502
        assert "error" in resp.json()
