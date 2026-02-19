"""Tests for JSON structured logging.

Verifies:
- Each request writes exactly one JSON log line with all required fields
- Concurrent requests do not interleave log lines
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _read_log_lines(path: Path) -> list[dict]:
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# --------------------------------------------------------------------------- #
# log_request writes a single JSON line with all required fields
# --------------------------------------------------------------------------- #

class TestLogRequestFields:
    def test_all_required_fields_present(self, tmp_path):
        log_file = tmp_path / "proxy.log"

        import mithril_proxy.logger as log_mod

        # Fresh logger for this test
        logger = logging.getLogger(f"mithril_proxy.test_{id(self)}")
        logger.handlers.clear()
        handler = logging.FileHandler(str(log_file), mode="a")
        handler.setFormatter(log_mod._JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        original_logger = log_mod._logger
        log_mod._logger = logger

        try:
            log_mod.log_request(
                user="abcd1234",
                source_ip="10.0.0.1",
                destination="github",
                mcp_method="tools/list",
                status_code=200,
                latency_ms=42.5,
            )
        finally:
            log_mod._logger = original_logger
            handler.close()

        lines = _read_log_lines(log_file)
        assert len(lines) == 1
        entry = lines[0]

        assert "timestamp" in entry
        assert entry["user"] == "abcd1234"
        assert entry["source_ip"] == "10.0.0.1"
        assert entry["destination"] == "github"
        assert entry["mcp_method"] == "tools/list"
        assert entry["status_code"] == 200
        assert entry["latency_ms"] == 42.5

    def test_error_field_present_when_provided(self, tmp_path):
        log_file = tmp_path / "proxy.log"

        import mithril_proxy.logger as log_mod

        logger = logging.getLogger(f"mithril_proxy.test_err_{id(self)}")
        logger.handlers.clear()
        handler = logging.FileHandler(str(log_file), mode="a")
        handler.setFormatter(log_mod._JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        original_logger = log_mod._logger
        log_mod._logger = logger

        try:
            log_mod.log_request(
                user="anonymous",
                source_ip="1.2.3.4",
                destination="testdest",
                mcp_method=None,
                status_code=502,
                latency_ms=1500.0,
                error="Connection refused",
            )
        finally:
            log_mod._logger = original_logger
            handler.close()

        lines = _read_log_lines(log_file)
        assert len(lines) == 1
        assert lines[0]["error"] == "Connection refused"
        assert lines[0]["user"] == "anonymous"
        assert lines[0]["status_code"] == 502

    def test_no_error_field_when_not_provided(self, tmp_path):
        log_file = tmp_path / "proxy.log"

        import mithril_proxy.logger as log_mod

        logger = logging.getLogger(f"mithril_proxy.test_noerr_{id(self)}")
        logger.handlers.clear()
        handler = logging.FileHandler(str(log_file), mode="a")
        handler.setFormatter(log_mod._JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        original_logger = log_mod._logger
        log_mod._logger = logger

        try:
            log_mod.log_request(
                user="abc",
                source_ip="1.2.3.4",
                destination="dest",
                mcp_method=None,
                status_code=200,
                latency_ms=10.0,
            )
        finally:
            log_mod._logger = original_logger
            handler.close()

        lines = _read_log_lines(log_file)
        assert "error" not in lines[0]


# --------------------------------------------------------------------------- #
# Concurrent writes do not corrupt or interleave lines
# --------------------------------------------------------------------------- #

class TestConcurrentLogging:
    def test_concurrent_writes_produce_valid_json_lines(self, tmp_path):
        log_file = tmp_path / "proxy.log"

        import mithril_proxy.logger as log_mod

        logger = logging.getLogger(f"mithril_proxy.test_concurrent_{id(self)}")
        logger.handlers.clear()
        handler = logging.FileHandler(str(log_file), mode="a")
        handler.setFormatter(log_mod._JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        original_logger = log_mod._logger
        log_mod._logger = logger

        N = 50
        errors: list[Exception] = []

        def write_log(i: int):
            try:
                log_mod.log_request(
                    user=f"user{i:04d}",
                    source_ip=f"10.0.0.{i % 256}",
                    destination="dest",
                    mcp_method="tools/list",
                    status_code=200,
                    latency_ms=float(i),
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_log, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        log_mod._logger = original_logger
        handler.close()

        assert not errors, f"Exceptions during concurrent writes: {errors}"

        lines = _read_log_lines(log_file)
        assert len(lines) == N, f"Expected {N} lines, got {len(lines)}"

        # Every line must be valid JSON with all required fields
        for entry in lines:
            for field in ("timestamp", "user", "source_ip", "destination", "status_code", "latency_ms"):
                assert field in entry, f"Missing field '{field}' in: {entry}"
