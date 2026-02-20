"""Shared request helpers for mithril-proxy."""

from __future__ import annotations

from fastapi import Request


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
