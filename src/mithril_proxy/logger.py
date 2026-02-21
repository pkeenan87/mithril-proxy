"""JSON structured logger for mithril-proxy."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_LOG_FILE_ENV = "LOG_FILE"
_DEFAULT_LOG_FILE = Path("/var/log/mithril-proxy/proxy.log")

_AUDIT_LOG_BODIES_ENV = "AUDIT_LOG_BODIES"
_AUDIT_MAX_BYTES = 32_768  # 32 KB

# Thread lock so concurrent requests don't interleave JSON lines
_write_lock = threading.Lock()

_logger: Optional[logging.Logger] = None


class _JsonFormatter(logging.Formatter):
    """Serialize a LogRecord to a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        # Merge any extra fields attached to the record
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in (
                "args", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message",
                "module", "msecs", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread",
                "threadName",
            ):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def _audit_enabled() -> bool:
    return os.environ.get(_AUDIT_LOG_BODIES_ENV, "true").lower() not in ("false", "0", "no")


def _resolve_log_path() -> Path:
    env_val = os.environ.get(_LOG_FILE_ENV)
    return Path(env_val) if env_val else _DEFAULT_LOG_FILE


def setup_logging() -> None:
    """Configure the JSON file logger.  Call once at startup."""
    global _logger

    log_path = _resolve_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(_JsonFormatter())

    logger = logging.getLogger("mithril_proxy")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # Prevent propagation to root logger (avoids duplicate lines in uvicorn output)
    logger.propagate = False

    _logger = logger


def get_logger() -> logging.Logger:
    if _logger is None:
        raise RuntimeError("setup_logging() has not been called.")
    return _logger


def log_request(
    *,
    user: str,
    source_ip: str,
    destination: str,
    mcp_method: Optional[str],
    status_code: int,
    latency_ms: float,
    error: Optional[str] = None,
    rpc_id=None,
    request_body: Optional[str] = None,
    response_body: Optional[str] = None,
    detection_action: Optional[str] = None,
    detection_engine: Optional[str] = None,
    detection_detail: Optional[str] = None,
) -> None:
    """Write one structured JSON log line for a proxied request."""
    logger = get_logger()
    extra: dict[str, Any] = {
        "user": user,
        "source_ip": source_ip,
        "destination": destination,
        "mcp_method": mcp_method,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
    }
    if error is not None:
        extra["error"] = error

    if rpc_id is not None:
        extra["rpc_id"] = rpc_id

    if detection_action is not None:
        extra["detection_action"] = detection_action
    if detection_engine is not None:
        extra["detection_engine"] = detection_engine
    if detection_detail is not None:
        extra["detection_detail"] = detection_detail[:_AUDIT_MAX_BYTES]

    if _audit_enabled():
        for field_name, value in (("request_body", request_body), ("response_body", response_body)):
            if value is not None:
                if len(value) > _AUDIT_MAX_BYTES:
                    extra[field_name] = value[:_AUDIT_MAX_BYTES]
                    extra["truncated"] = True
                else:
                    extra[field_name] = value

    with _write_lock:
        logger.info("request", extra=extra)
