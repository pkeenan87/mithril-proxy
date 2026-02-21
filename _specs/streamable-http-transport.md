# Streamable HTTP Transport Support

## Overview

Add support for MCP servers that use the Streamable HTTP transport protocol. This includes a new `streamable_http` destination type in `destinations.yml` and new proxy endpoints that forward requests directly to an upstream MCP server URL.

**Problem being solved:** GitHub's remote MCP server (`https://api.githubcopilot.com/mcp`) uses the MCP Streamable HTTP transport — clients POST JSON-RPC requests directly to the endpoint. The proxy currently only exposes `GET /{destination}/sse`, so `POST /github/sse` returns a 405 Method Not Allowed.

---

## Background

The MCP spec defines two transports:

| Transport | How it works | Use case |
|---|---|---|
| SSE | Client `GET`s a stream; posts to a separate message endpoint | Older remote servers |
| Streamable HTTP | Client `POST`s directly to the endpoint; server replies with JSON or an SSE stream | Newer remote servers (GitHub Copilot, etc.) |

GitHub Copilot MCP and other modern MCP servers use Streamable HTTP. Without this feature, users cannot proxy those servers through Mithril.

---

## New Destination Type: `streamable_http`

### Config format (`destinations.yml`)

```yaml
destinations:
  github:
    type: streamable_http
    url: https://api.githubcopilot.com/mcp
```

- `type: streamable_http` — required to opt into the new behavior
- `url` — the full upstream MCP endpoint (no path suffix is appended)
- Auth headers (e.g. `Authorization: Bearer <token>`) are forwarded from the client unchanged, same as SSE destinations

### Validation rules

- `url` is required and must be non-empty
- Same shell-metacharacter checks do **not** apply (no subprocess command)
- `env` block is not meaningful (no subprocess); reject or ignore it

---

## New Routes

### `POST /{destination}/mcp`

**Purpose:** Forward a JSON-RPC request from the MCP client to the upstream Streamable HTTP server.

**Request:**
- Body: JSON-RPC payload from client
- Headers: all client headers forwarded except `Host`, `Content-Length`, `Transfer-Encoding`

**Response:**
- If upstream responds with `Content-Type: application/json` → proxy the JSON response directly
- If upstream responds with `Content-Type: text/event-stream` → stream the SSE events back to the client

**Error handling:**
- Upstream unreachable → 502 with JSON error body
- Unknown destination → 404
- Destination is not `streamable_http` type → 400

**Logging:** Same `log_request()` fields as other handlers — `user`, `source_ip`, `destination`, `mcp_method`, `rpc_id`, `status_code`, `latency_ms`, `request_body`, `response_body` (subject to `AUDIT_LOG_BODIES`).

---

### `GET /{destination}/mcp`

**Purpose:** Allow the MCP client to establish a long-lived SSE stream for server-initiated messages (optional per the MCP spec; some clients use it).

**Request:**
- No body
- Headers forwarded as above

**Response:**
- Proxy the upstream's SSE stream back to the client (`Content-Type: text/event-stream`)
- If upstream returns 405 or does not support server-initiated SSE, surface that to the client cleanly

**Logging:** One log entry at stream close, same as `handle_sse`.

---

## Client Configuration

Once the proxy supports this, the client config for Claude Desktop (`~/.claude.json`) changes from:

```json
"github": {
  "type": "sse",
  "url": "http://<pi-ip>:3000/github/sse"
}
```

to:

```json
"github": {
  "type": "http",
  "url": "http://<pi-ip>:3000/github/mcp",
  "headers": {
    "Authorization": "Bearer YOUR_GITHUB_TOKEN"
  }
}
```

The `type: "http"` tells Claude Desktop to use Streamable HTTP transport (POST-based). The URL points to the new `/mcp` endpoint on the proxy.

---

## Files to Change

| File | Change |
|---|---|
| `src/mithril_proxy/config.py` | Add `"streamable_http"` to the valid types list; add parsing branch for `streamable_http` (requires `url`, no `command`) |
| `src/mithril_proxy/proxy.py` | Add `handle_streamable_http_post()` and `handle_streamable_http_get()` handler functions |
| `src/mithril_proxy/main.py` | Register `POST /{destination}/mcp` and `GET /{destination}/mcp` routes |
| `tests/test_streamable_http.py` | New test file (see Test Cases below) |
| `config/destinations.yml` | Add `streamable_http` example in comments |
| `SETUP.md` | Document the new destination type and client config |
| `CLAUDE.md` | Update module map and architecture notes |

---

## Test Cases

- `streamable_http` destination type is accepted by `load_config()`
- Unknown type in config raises `ValueError`
- `POST /{destination}/mcp` with known `streamable_http` dest → forwards to upstream URL
- `POST /{destination}/mcp` with unknown dest → 404
- `POST /{destination}/mcp` with an `sse`-type dest → 400 (wrong type)
- Upstream returns JSON → proxy returns JSON with correct status and content-type
- Upstream returns SSE (`text/event-stream`) → proxy streams SSE back
- Upstream unreachable → 502
- Auth header is forwarded to upstream
- `request_body` and `response_body` appear in log (when `AUDIT_LOG_BODIES=true`)
- `AUDIT_LOG_BODIES=false` → body fields absent from log
- `rpc_id` extracted and logged from request and/or response
- `GET /{destination}/mcp` streams upstream SSE back to client
- `GET /{destination}/mcp` with unknown dest → 404

---

## Acceptance Criteria

- [ ] `type: streamable_http` is valid in `destinations.yml` with no startup error
- [ ] `POST /{destination}/mcp` proxies a GitHub Copilot `initialize` request end-to-end
- [ ] Claude Desktop with `type: "http"` config connects to GitHub MCP through the proxy
- [ ] All existing tests continue to pass
- [ ] New test file passes
- [ ] `SETUP.md` documents the new destination type with a working example
