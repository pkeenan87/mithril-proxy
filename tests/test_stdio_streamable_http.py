"""Tests for the stdio → Streamable HTTP bridge.

Each test that spawns a real subprocess writes a helper script to tmp_path
to avoid shell metacharacters that load_config() rejects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

import httpx
import pytest

_UUID_A = "00000000-0000-4000-8000-000000000001"

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def reset_bridge_state():
    """Clear bridge state and terminate orphaned subprocesses between tests.

    Must be synchronous: each async test runs in its own event loop, so tasks
    created in a previous test's loop are already dead. We only need to
    terminate subprocesses and clear the shared dicts.
    """
    import mithril_proxy.bridge as bridge_mod
    # Terminate any leftover subprocesses from previous test
    for b in list(bridge_mod._stdio_bridges.values()):
        if b.process and b.process.returncode is None:
            try:
                b.process.terminate()
            except ProcessLookupError:
                pass
    bridge_mod._stdio_bridges.clear()
    bridge_mod._bridges_create_lock = None
    yield
    for b in list(bridge_mod._stdio_bridges.values()):
        if b.process and b.process.returncode is None:
            try:
                b.process.terminate()
            except ProcessLookupError:
                pass
    bridge_mod._stdio_bridges.clear()
    bridge_mod._bridges_create_lock = None


@pytest.fixture()
def tmp_log(tmp_path):
    return tmp_path / "test.log"


@pytest.fixture()
def setup_logger(tmp_log):
    import mithril_proxy.logger as log_mod

    logger = logging.getLogger("mithril_proxy_test_sh")
    logger.handlers.clear()
    handler = logging.FileHandler(str(tmp_log), mode="a")
    handler.setFormatter(log_mod._JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    log_mod._logger = logger
    yield logger
    log_mod._logger = None


def _make_echo_script(tmp_path: Path) -> Path:
    """Subprocess that echoes each JSON-RPC request back as a response."""
    script = tmp_path / "echo_mcp.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    try:\n"
        "        req = json.loads(line)\n"
        "        resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {}}\n"
        "        print(json.dumps(resp), flush=True)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    return script


def _make_notification_script(tmp_path: Path) -> Path:
    """Subprocess that sends a notification before responding to the 2nd+ request."""
    script = tmp_path / "notif_mcp.py"
    script.write_text(
        "import sys, json\n"
        "count = 0\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    try:\n"
        "        req = json.loads(line)\n"
        "        count += 1\n"
        "        if count > 1:\n"
        "            notif = {'jsonrpc': '2.0', 'method': 'notifications/test', 'params': {}}\n"
        "            print(json.dumps(notif), flush=True)\n"
        "        resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {'count': count}}\n"
        "        print(json.dumps(resp), flush=True)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    return script


def _make_one_shot_script(tmp_path: Path) -> Path:
    """Subprocess that responds once, then exits without responding to further requests."""
    script = tmp_path / "oneshot_mcp.py"
    script.write_text(
        "import sys, json\n"
        "count = 0\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    try:\n"
        "        req = json.loads(line)\n"
        "        count += 1\n"
        "        if count == 1:\n"
        "            resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {}}\n"
        "            print(json.dumps(resp), flush=True)\n"
        "        else:\n"
        "            sys.exit(0)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    return script


@pytest.fixture()
def app_with_echo_stdio(tmp_log, tmp_path):
    """FastAPI app with one echo stdio destination and one SSE destination."""
    script = _make_echo_script(tmp_path)
    destinations_yml = tmp_path / "destinations.yml"
    destinations_yml.write_text(
        "destinations:\n"
        "  echo:\n"
        "    type: stdio\n"
        f"    command: python3 {script}\n"
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


@pytest.fixture()
def app_with_notif_stdio(tmp_log, tmp_path):
    """FastAPI app with the notification subprocess."""
    script = _make_notification_script(tmp_path)
    destinations_yml = tmp_path / "destinations.yml"
    destinations_yml.write_text(
        "destinations:\n"
        "  notif:\n"
        "    type: stdio\n"
        f"    command: python3 {script}\n"
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


@pytest.fixture()
def app_with_oneshot_stdio(tmp_log, tmp_path):
    """FastAPI app with the one-shot subprocess (responds once, then exits)."""
    script = _make_one_shot_script(tmp_path)
    destinations_yml = tmp_path / "destinations.yml"
    destinations_yml.write_text(
        "destinations:\n"
        "  oneshot:\n"
        "    type: stdio\n"
        f"    command: python3 {script}\n"
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
# Test 1: First POST spawns subprocess, returns 200 with Mcp-Session-Id header
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_first_post_creates_session(app_with_echo_stdio, setup_logger):
    """First POST /mcp (no session header) spawns subprocess and returns Mcp-Session-Id."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/echo/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert resp.status_code == 200
    assert "mcp-session-id" in resp.headers
    session_id = resp.headers["mcp-session-id"]
    # Must be a valid UUID v4
    import re
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        session_id,
    )
    body = resp.json()
    assert body["id"] == 1  # original id restored


# --------------------------------------------------------------------------- #
# Test 2: Second POST with valid session header routes to same subprocess
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_second_post_routes_to_same_subprocess(app_with_echo_stdio, setup_logger):
    """Second POST with valid Mcp-Session-Id routes to existing subprocess."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First POST — creates session
        resp1 = await client.post(
            "/echo/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert resp1.status_code == 200
        session_id = resp1.headers["mcp-session-id"]

        # Second POST — uses session
        resp2 = await client.post(
            "/echo/mcp",
            headers={"mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert resp2.status_code == 200
    # No new session header on subsequent requests
    assert "mcp-session-id" not in resp2.headers
    body = resp2.json()
    assert body["id"] == 2


# --------------------------------------------------------------------------- #
# Test 3: POST with unknown Mcp-Session-Id → 404
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_post_unknown_session_returns_404(app_with_echo_stdio, setup_logger):
    """POST /mcp with unknown Mcp-Session-Id returns 404."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/echo/mcp",
            headers={"mcp-session-id": _UUID_A},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Test 4: POST with invalid UUID format Mcp-Session-Id → 400
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_post_invalid_session_id_format_returns_400(app_with_echo_stdio, setup_logger):
    """POST /mcp with invalid Mcp-Session-Id format returns 400."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/echo/mcp",
            headers={"mcp-session-id": "not-a-uuid"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Test 5: POST with no id (client notification) → 202 immediately
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_post_client_notification_returns_202(app_with_echo_stdio, setup_logger):
    """POST /mcp with no 'id' in body (client notification) returns 202 without waiting."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create session first
        resp1 = await client.post(
            "/echo/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = resp1.headers["mcp-session-id"]

        # Client notification (no id field)
        resp2 = await client.post(
            "/echo/mcp",
            headers={"mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    assert resp2.status_code == 202


# --------------------------------------------------------------------------- #
# Test 6: POST with JSON array body → 400
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_post_batch_returns_400(app_with_echo_stdio, setup_logger):
    """POST /mcp with JSON array body (batch) returns 400."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/echo/mcp",
            content=json.dumps([
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "pong"},
            ]),
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Test 7: GET /mcp with valid session ID receives notification from subprocess
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_receives_subprocess_notification(app_with_notif_stdio, setup_logger):
    """Subprocess notifications are dispatched to registered notification queues.

    httpx.ASGITransport buffers the full response before returning, so it cannot
    test SSE streaming directly. Instead, we register a notification queue on the
    bridge ourselves (exactly what handle_stdio_streamable_http_get does) and
    verify the stdout reader delivers the notification there.
    """
    import mithril_proxy.bridge as bridge_mod

    transport = httpx.ASGITransport(app=app_with_notif_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First POST — creates session (count=1 in subprocess, no notification)
        resp1 = await client.post(
            "/notif/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert resp1.status_code == 200
        session_id = resp1.headers["mcp-session-id"]

        # Manually register a notification queue (what GET handler does internally)
        bridge = bridge_mod._stdio_bridges.get("notif")
        assert bridge is not None
        stream_uuid = str(uuid.uuid4())
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        bridge.notification_queues[stream_uuid] = q

        # Second POST — subprocess sends notification (count=2) then responds
        resp2 = await client.post(
            "/notif/mcp",
            headers={"mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert resp2.status_code == 200
        # Notification arrives before the response (stdout_reader is sequential),
        # so by the time POST returns the queue already has the notification.

    assert not q.empty(), "Notification queue should have received the subprocess notification"
    notification_str = q.get_nowait()
    notification = json.loads(notification_str)
    assert notification.get("method") == "notifications/test"


# --------------------------------------------------------------------------- #
# Test 8: GET /mcp without session ID → 400
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_without_session_id_returns_400(app_with_echo_stdio, setup_logger):
    """GET /mcp without Mcp-Session-Id header returns 400."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/echo/mcp")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Test 9: DELETE /mcp with valid session ID → 204
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_delete_valid_session_returns_204(app_with_echo_stdio, setup_logger):
    """DELETE /mcp with valid Mcp-Session-Id returns 204 and removes session."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create session
        resp1 = await client.post(
            "/echo/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = resp1.headers["mcp-session-id"]

        # Delete session
        resp2 = await client.delete(
            "/echo/mcp",
            headers={"mcp-session-id": session_id},
        )
        assert resp2.status_code == 204

        # Subsequent POST with same session ID should return 404
        resp3 = await client.post(
            "/echo/mcp",
            headers={"mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
    assert resp3.status_code == 404


# --------------------------------------------------------------------------- #
# Test 10: GET /sse for a stdio destination → 410
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_sse_for_stdio_returns_410(app_with_echo_stdio, setup_logger):
    """GET /{stdio_dest}/sse returns 410 Gone."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/echo/sse")
    assert resp.status_code == 410
    assert "mcp" in resp.json()["error"].lower()


# --------------------------------------------------------------------------- #
# Test 11: POST /message for a stdio destination → 410
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_post_message_for_stdio_returns_410(app_with_echo_stdio, setup_logger):
    """POST /{stdio_dest}/message returns 410 Gone."""
    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/echo/message?session_id={_UUID_A}",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert resp.status_code == 410
    assert "mcp" in resp.json()["error"].lower()


# --------------------------------------------------------------------------- #
# Test 12: Connection cap — 11th session returns 503
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_connection_cap_returns_503(app_with_echo_stdio, setup_logger):
    """After MAX_STDIO_CONNECTIONS sessions, next POST without session ID returns 503."""
    import mithril_proxy.bridge as bridge_mod

    transport = httpx.ASGITransport(app=app_with_echo_stdio)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create the first real session to ensure the bridge exists
        resp = await client.post(
            "/echo/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert resp.status_code == 200

        # Pre-fill bridge.sessions with fake UUIDs to hit the cap
        bridge = bridge_mod._stdio_bridges.get("echo")
        assert bridge is not None
        cap = bridge_mod._MAX_CONNECTIONS_PER_DEST
        while len(bridge.sessions) < cap:
            bridge.sessions.add(str(uuid.uuid4()))

        # Next new-session request must be rejected
        resp_cap = await client.post(
            "/echo/mcp",
            json={"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {}},
        )
    assert resp_cap.status_code == 503
    assert "Too many active sessions" in resp_cap.json()["error"]
