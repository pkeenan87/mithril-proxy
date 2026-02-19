"""Test auth pass-through behaviour.

The proxy never gates on auth — it passes Bearer tokens straight through and
logs the first 8 chars for correlation. A missing token logs 'anonymous'.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_request(auth_header: str | None = None, client_ip: str = "127.0.0.1"):
    req = MagicMock()
    headers = {}
    if auth_header is not None:
        headers["authorization"] = auth_header
    req.headers = headers
    req.client = MagicMock()
    req.client.host = client_ip
    req.query_params = {}
    return req


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

class TestUserCorrelation:
    def test_valid_token_uses_first_8_chars(self):
        from mithril_proxy.proxy import _user_from_request

        req = _make_request("Bearer abcdefghijklmno")
        assert _user_from_request(req) == "abcdefgh"

    def test_missing_auth_returns_anonymous(self):
        from mithril_proxy.proxy import _user_from_request

        req = _make_request(None)
        assert _user_from_request(req) == "anonymous"

    def test_malformed_auth_returns_anonymous(self):
        from mithril_proxy.proxy import _user_from_request

        req = _make_request("Basic dXNlcjpwYXNz")
        assert _user_from_request(req) == "anonymous"

    def test_bearer_prefix_case_insensitive(self):
        from mithril_proxy.proxy import _user_from_request

        req = _make_request("BEARER mytoken123")
        assert _user_from_request(req) == "mytoken1"

    def test_short_token_uses_full_token(self):
        from mithril_proxy.proxy import _user_from_request

        req = _make_request("Bearer abc")
        assert _user_from_request(req) == "abc"


class TestHeaderPassthrough:
    def test_auth_header_included_in_upstream_headers(self):
        from mithril_proxy.proxy import _upstream_headers

        req = MagicMock()
        req.headers = {
            "authorization": "Bearer mysecrettoken",
            "content-type": "application/json",
            "host": "localhost:3000",
        }
        result = _upstream_headers(req)

        assert "authorization" in result
        assert result["authorization"] == "Bearer mysecrettoken"
        # Host must be stripped
        assert "host" not in result

    def test_host_header_is_stripped(self):
        from mithril_proxy.proxy import _upstream_headers

        req = MagicMock()
        req.headers = {"host": "example.com", "x-custom": "value"}
        result = _upstream_headers(req)
        assert "host" not in result
        assert result["x-custom"] == "value"

    def test_no_auth_header_not_injected(self):
        from mithril_proxy.proxy import _upstream_headers

        req = MagicMock()
        req.headers = {"content-type": "application/json"}
        result = _upstream_headers(req)
        assert "authorization" not in result
