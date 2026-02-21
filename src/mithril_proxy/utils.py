"""Shared request helpers for mithril-proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from .detector import DetectionResult


def detection_log_kwargs(result: DetectionResult) -> dict[str, str]:
    """Build log_request kwargs from a DetectionResult (only when non-pass)."""
    if result.action == "pass":
        return {}
    return {
        "detection_action": result.action,
        "detection_engine": result.engine,
        "detection_detail": result.detail,
    }


def source_ip(request: Request) -> str:
    """Return the client IP address from the request.

    Uses only ``request.client.host`` — X-Forwarded-For is intentionally
    ignored because the proxy is deployed without a trusted upstream proxy.
    If a reverse proxy is added in future, revisit this with a trusted-proxy
    allowlist before re-enabling header-based IP extraction.
    """
    if request.client:
        return request.client.host
    return "unknown"
