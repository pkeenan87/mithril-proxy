"""Tests for prompt injection detection: regex engine, AI engine, and integration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mithril_proxy.config import DestinationConfig
from mithril_proxy.detector import (
    DetectionResult,
    _REDACTION_PLACEHOLDER,
    _run_ai,
    load_patterns,
    reload_patterns,
    scan,
)

_UUID_A = "00000000-0000-4000-8000-000000000001"


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _dest(regex_mode="off", ai_mode="off", ai_threshold=None, ai_max_chars=4000):
    return DestinationConfig(
        type="stdio",
        command="echo hello",
        regex_mode=regex_mode,
        ai_mode=ai_mode,
        ai_threshold=ai_threshold,
        ai_max_chars=ai_max_chars,
    )


def _read_log_lines(path: Path) -> list[dict]:
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# --------------------------------------------------------------------------- #
#  Fixtures                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def patterns_dir(tmp_path):
    d = tmp_path / "patterns.d"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def reset_detector_state():
    """Reset detector module state between tests."""
    import mithril_proxy.detector as det
    det._patterns = []
    det._ai_pipeline = None
    yield
    det._patterns = []
    det._ai_pipeline = None


@pytest.fixture()
def tmp_log(tmp_path):
    return tmp_path / "detection_test.log"


@pytest.fixture()
def setup_logger(tmp_log):
    """Wire up a real JSON logger so log_request() writes to tmp_log."""
    import mithril_proxy.logger as log_mod

    logger = logging.getLogger(f"mithril_proxy_detection_{id(tmp_log)}")
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


# =========================================================================== #
#  Pattern loader tests                                                        #
# =========================================================================== #

class TestPatternLoader:

    def test_load_from_directory(self, patterns_dir):
        (patterns_dir / "common.txt").write_text(
            "# comment\n"
            "ignore.*previous.*instructions\n"
            "\n"
            "system\\s*prompt\n"
        )
        count = load_patterns(patterns_dir)
        assert count == 2

    def test_invalid_regex_skipped(self, patterns_dir, caplog):
        (patterns_dir / "bad.txt").write_text(
            "[invalid regex\n"
            "valid_pattern\n"
        )
        with caplog.at_level(logging.WARNING, logger="mithril_proxy"):
            count = load_patterns(patterns_dir)
        assert count == 1
        assert "Invalid regex" in caplog.text

    def test_missing_directory_warns(self, tmp_path, caplog):
        missing = tmp_path / "nonexistent"
        with caplog.at_level(logging.WARNING, logger="mithril_proxy"):
            count = load_patterns(missing)
        assert count == 0
        assert "does not exist" in caplog.text

    def test_reload_replaces_patterns(self, patterns_dir):
        (patterns_dir / "v1.txt").write_text("first_pattern\n")
        load_patterns(patterns_dir)
        (patterns_dir / "v1.txt").write_text("second_pattern\nthird_pattern\n")
        count = reload_patterns()
        # reload_patterns() uses default dir; call load_patterns directly for test
        count = load_patterns(patterns_dir)
        assert count == 2

    def test_conf_files_loaded(self, patterns_dir):
        (patterns_dir / "rules.conf").write_text("some_rule\n")
        count = load_patterns(patterns_dir)
        assert count == 1

    def test_non_txt_conf_files_ignored(self, patterns_dir):
        (patterns_dir / "rules.json").write_text("not_loaded\n")
        count = load_patterns(patterns_dir)
        assert count == 0


# =========================================================================== #
#  Regex engine tests                                                          #
# =========================================================================== #

class TestRegexEngine:

    @pytest.mark.asyncio
    async def test_off_mode_no_scan(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        result = await scan("ignore previous injection instructions", _dest(regex_mode="off"))
        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_monitor_passes_body_unchanged(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        body = "try injection here"
        result = await scan(body, _dest(regex_mode="monitor"))
        assert result.action == "monitor"
        assert result.engine == "regex"
        assert result.body == body  # unchanged

    @pytest.mark.asyncio
    async def test_redact_replaces_match(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        body = "try injection here"
        result = await scan(body, _dest(regex_mode="redact"))
        assert result.action == "redact"
        assert result.engine == "regex"
        assert "injection" not in result.body
        assert _REDACTION_PLACEHOLDER in result.body

    @pytest.mark.asyncio
    async def test_block_returns_block_action(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        result = await scan("try injection here", _dest(regex_mode="block"))
        assert result.action == "block"
        assert result.engine == "regex"

    @pytest.mark.asyncio
    async def test_no_match_passes(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        result = await scan("completely safe content", _dest(regex_mode="block"))
        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_empty_body_passes(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        result = await scan("", _dest(regex_mode="block"))
        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_case_insensitive(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        result = await scan("INJECTION ATTACK", _dest(regex_mode="monitor"))
        assert result.action == "monitor"


# =========================================================================== #
#  AI engine tests                                                             #
# =========================================================================== #

class TestAIEngine:

    @pytest.mark.asyncio
    async def test_ai_block_on_high_score(self):
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.95}]
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            result = await scan("hack the system", _dest(ai_mode="block"))
        assert result.action == "block"
        assert result.engine == "ai"
        assert "0.950" in result.detail

    @pytest.mark.asyncio
    async def test_ai_below_threshold_passes(self):
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.3}]
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            result = await scan("normal text", _dest(ai_mode="block"))
        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_ai_unavailable_skips(self):
        # _ai_pipeline is None by default (reset_detector_state fixture)
        result = await scan("hack the system", _dest(ai_mode="block"))
        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_ai_max_chars_skips(self):
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.95}]
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            result = await scan("x" * 5000, _dest(ai_mode="block", ai_max_chars=100))
        assert result.action == "pass"
        mock_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_ai_per_destination_threshold(self):
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.7}]
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            # Default threshold 0.85 — should pass
            result = await scan("suspicious text", _dest(ai_mode="block"))
            assert result.action == "pass"
            # Per-destination threshold 0.5 — should block
            result = await scan("suspicious text", _dest(ai_mode="block", ai_threshold=0.5))
            assert result.action == "block"

    @pytest.mark.asyncio
    async def test_ai_redact_replaces_entire_body(self):
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.95}]
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            result = await scan("hack the system", _dest(ai_mode="redact"))
        assert result.action == "redact"
        assert result.body == _REDACTION_PLACEHOLDER

    @pytest.mark.asyncio
    async def test_ai_monitor_passes_body_unchanged(self):
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.95}]
        body = "hack the system"
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            result = await scan(body, _dest(ai_mode="monitor"))
        assert result.action == "monitor"
        assert result.body == body


# =========================================================================== #
#  Strictest-mode-wins tests                                                   #
# =========================================================================== #

class TestStrictestModeWins:

    @pytest.mark.asyncio
    async def test_regex_block_trumps_ai_monitor(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.95}]
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            result = await scan(
                "injection attack",
                _dest(regex_mode="block", ai_mode="monitor"),
            )
        assert result.action == "block"

    @pytest.mark.asyncio
    async def test_ai_block_trumps_regex_monitor(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("injection\n")
        load_patterns(patterns_dir)
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [{"label": "INJECTION", "score": 0.95}]
        with patch("mithril_proxy.detector._ai_pipeline", mock_pipeline):
            result = await scan(
                "injection attack",
                _dest(regex_mode="monitor", ai_mode="block"),
            )
        assert result.action == "block"
        assert result.engine == "ai"


# =========================================================================== #
#  Config validation tests                                                     #
# =========================================================================== #

class TestConfigValidation:

    def test_valid_detection_modes(self, tmp_path):
        from mithril_proxy.config import load_config
        config_file = tmp_path / "destinations.yml"
        config_file.write_text(
            "destinations:\n"
            "  test:\n"
            "    type: streamable_http\n"
            "    url: https://example.com/mcp\n"
            "    regex_mode: monitor\n"
            "    ai_mode: block\n"
            "    ai_threshold: 0.9\n"
            "    ai_max_chars: 2000\n"
        )
        load_config(config_file)
        from mithril_proxy.config import get_destination
        dest = get_destination("test")
        assert dest.regex_mode == "monitor"
        assert dest.ai_mode == "block"
        assert dest.ai_threshold == 0.9
        assert dest.ai_max_chars == 2000

    def test_invalid_detection_mode_raises(self, tmp_path):
        from mithril_proxy.config import load_config
        config_file = tmp_path / "destinations.yml"
        config_file.write_text(
            "destinations:\n"
            "  test:\n"
            "    type: streamable_http\n"
            "    url: https://example.com/mcp\n"
            "    regex_mode: invalid\n"
        )
        with pytest.raises(ValueError, match="invalid regex_mode"):
            load_config(config_file)

    def test_defaults_when_omitted(self, tmp_path):
        from mithril_proxy.config import load_config
        config_file = tmp_path / "destinations.yml"
        config_file.write_text(
            "destinations:\n"
            "  test:\n"
            "    type: streamable_http\n"
            "    url: https://example.com/mcp\n"
        )
        load_config(config_file)
        from mithril_proxy.config import get_destination
        dest = get_destination("test")
        assert dest.regex_mode == "off"
        assert dest.ai_mode == "off"
        assert dest.ai_threshold is None
        assert dest.ai_max_chars == 4000


# =========================================================================== #
#  Logger detection fields                                                     #
# =========================================================================== #

class TestLoggerDetectionFields:

    def test_detection_fields_in_log(self, setup_logger, tmp_log):
        from mithril_proxy.logger import log_request
        log_request(
            user="test",
            source_ip="127.0.0.1",
            destination="test",
            mcp_method="test/method",
            status_code=200,
            latency_ms=1.0,
            detection_action="block",
            detection_engine="regex",
            detection_detail="injection",
        )
        lines = _read_log_lines(tmp_log)
        assert len(lines) == 1
        assert lines[0]["detection_action"] == "block"
        assert lines[0]["detection_engine"] == "regex"
        assert lines[0]["detection_detail"] == "injection"

    def test_no_detection_fields_when_none(self, setup_logger, tmp_log):
        from mithril_proxy.logger import log_request
        log_request(
            user="test",
            source_ip="127.0.0.1",
            destination="test",
            mcp_method="test/method",
            status_code=200,
            latency_ms=1.0,
        )
        lines = _read_log_lines(tmp_log)
        assert len(lines) == 1
        assert "detection_action" not in lines[0]


# =========================================================================== #
#  Admin reload endpoint                                                       #
# =========================================================================== #

class TestAdminReloadEndpoint:

    def test_reload_from_localhost(self, tmp_path, patterns_dir):
        """Test the admin endpoint returns loaded count."""
        from starlette.testclient import TestClient
        from mithril_proxy.main import app

        (patterns_dir / "rules.txt").write_text("injection\n")

        with patch("mithril_proxy.main.reload_patterns") as mock_reload, \
             patch("mithril_proxy.main._source_ip", return_value="127.0.0.1"):
            mock_reload.return_value = 1

            config_file = tmp_path / "destinations.yml"
            config_file.write_text("destinations: {}\n")
            log_file = tmp_path / "test.log"

            with patch.dict("os.environ", {
                "DESTINATIONS_CONFIG": str(config_file),
                "LOG_FILE": str(log_file),
                "PATTERNS_DIR": str(patterns_dir),
            }):
                client = TestClient(app)
                response = client.post("/admin/reload-patterns")
                assert response.status_code == 200
                assert response.json() == {"loaded": 1}

    def test_reload_blocked_from_remote(self, tmp_path):
        """Non-localhost requests to admin endpoint are rejected."""
        from starlette.testclient import TestClient
        from mithril_proxy.main import app

        config_file = tmp_path / "destinations.yml"
        config_file.write_text("destinations: {}\n")
        log_file = tmp_path / "test.log"

        with patch.dict("os.environ", {
            "DESTINATIONS_CONFIG": str(config_file),
            "LOG_FILE": str(log_file),
        }):
            with patch("mithril_proxy.main._source_ip", return_value="192.168.1.100"):
                client = TestClient(app)
                response = client.post("/admin/reload-patterns")
                assert response.status_code == 403


# =========================================================================== #
#  Response scanning                                                           #
# =========================================================================== #

class TestResponseScanning:

    @pytest.mark.asyncio
    async def test_response_scan_block(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("secret_data\n")
        load_patterns(patterns_dir)
        result = await scan(
            '{"result": "here is secret_data from db"}',
            _dest(regex_mode="block"),
            is_response=True,
        )
        assert result.action == "block"

    @pytest.mark.asyncio
    async def test_response_scan_redact(self, patterns_dir):
        (patterns_dir / "rules.txt").write_text("secret_data\n")
        load_patterns(patterns_dir)
        result = await scan(
            '{"result": "here is secret_data from db"}',
            _dest(regex_mode="redact"),
            is_response=True,
        )
        assert result.action == "redact"
        assert "secret_data" not in result.body
        assert _REDACTION_PLACEHOLDER in result.body


# =========================================================================== #
#  Hot-reload integration                                                      #
# =========================================================================== #

class TestHotReloadIntegration:

    @pytest.mark.asyncio
    async def test_new_pattern_active_after_reload(self, patterns_dir):
        (patterns_dir / "v1.txt").write_text("old_pattern\n")
        load_patterns(patterns_dir)

        # Old pattern blocks
        result = await scan("old_pattern here", _dest(regex_mode="block"))
        assert result.action == "block"

        # New pattern not yet loaded
        result = await scan("new_pattern here", _dest(regex_mode="block"))
        assert result.action == "pass"

        # Reload with new pattern
        (patterns_dir / "v2.txt").write_text("new_pattern\n")
        load_patterns(patterns_dir)

        result = await scan("new_pattern here", _dest(regex_mode="block"))
        assert result.action == "block"
