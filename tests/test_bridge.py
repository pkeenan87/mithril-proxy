"""Tests for the stdio bridge and proxy layer."""
from __future__ import annotations

import asyncio
import logging

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
    bridge._stdio_bridges.clear()
    bridge._bridges_create_lock = None
    yield
    bridge._stdio_bridges.clear()
    bridge._bridges_create_lock = None


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
def app_with_stdio(tmp_log, tmp_path):
    """FastAPI app with one stdio and one SSE destination."""
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

        configs = {"mysse": DestinationConfig(type="sse", url="http://example.com")}
        validate_stdio_commands(configs)  # must not raise


# --------------------------------------------------------------------------- #
# TestShutdown
# --------------------------------------------------------------------------- #

class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_terminates_processes(self, setup_logger):
        """shutdown_all_stdio sends SIGTERM to all registered bridges."""
        from mithril_proxy.bridge import StdioDestinationBridge, _stdio_bridges, shutdown_all_stdio

        # Spawn a real long-running process
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.returncode is None

        bridge = StdioDestinationBridge(destination="testdest", process=proc)
        _stdio_bridges["testdest"] = bridge

        await shutdown_all_stdio()

        # Process should be dead
        assert proc.returncode is not None
        assert "testdest" not in _stdio_bridges


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

    def test_stdio_destination_get_sse_returns_410(self, app_with_stdio):
        """GET /{stdio_dest}/sse returns 410 Gone."""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_stdio, raise_server_exceptions=False)
        resp = client.get("/echostdio/sse")
        assert resp.status_code == 410
        assert "mcp" in resp.json()["error"].lower()

    def test_stdio_destination_post_message_returns_410(self, app_with_stdio):
        """POST /{stdio_dest}/message returns 410 Gone."""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_stdio, raise_server_exceptions=False)
        resp = client.post(
            f"/echostdio/message?session_id={_UUID_A}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        )
        assert resp.status_code == 410
        assert "mcp" in resp.json()["error"].lower()
