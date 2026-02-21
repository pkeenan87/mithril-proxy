# Spec for stdio Streamable HTTP Bridge

branch: claude/feature/stdio-streamable-http-bridge

## Summary

Replace the current stdio→SSE bridge with a stdio→Streamable HTTP bridge so that `stdio` destinations expose the modern MCP Streamable HTTP transport (`POST/GET/DELETE /{destination}/mcp`) instead of the legacy SSE transport (`GET /{destination}/sse` + `POST /{destination}/message`).

Currently, clients must use `type: sse` and connect via `/sse`. After this change, clients use `type: http` and connect via `/mcp`, matching the same protocol used by `streamable_http` remote destinations. The underlying subprocess communication (JSON-RPC over stdio) is unchanged; only the network-facing protocol changes.

---

## Functional Requirements

### Session lifecycle

- A `stdio` session begins when a client sends `POST /{destination}/mcp` without an `Mcp-Session-Id` header (or with an `initialize` method body).
- The bridge spawns a subprocess, assigns a UUID as the session ID, sends the JSON-RPC body to subprocess stdin, waits for the matching response on stdout, and returns it in the POST response body along with an `Mcp-Session-Id` response header.
- Subsequent `POST /{destination}/mcp` requests include `Mcp-Session-Id: <uuid>` and are routed to the existing subprocess for that session.
- A session ends when:
  - The client sends `DELETE /{destination}/mcp` with the session's `Mcp-Session-Id`.
  - The subprocess exits and all retries are exhausted.
  - The proxy shuts down.

### POST /{destination}/mcp (stdio dispatch)

- Validate `Mcp-Session-Id` format (UUID v4) if present; return 400 on invalid format.
- If no session exists for the given ID, return 404.
- Write the JSON-RPC body (newline-terminated) to the subprocess stdin.
- Wait for a response on stdout whose `id` field matches the request's `id`.
  - Lines with no `id` (notifications) are routed to the session's notification queue, not returned here.
- Return the matched response as `Content-Type: application/json`.
- If no matching response arrives within a configurable timeout (default 30 s), return 504.
- If the subprocess exits before responding, restart and return 503 if all retries fail.

### GET /{destination}/mcp (stdio notifications)

- Requires a valid `Mcp-Session-Id` header; return 400/404 if missing or unknown.
- Returns `Content-Type: text/event-stream`.
- Drains the session's notification queue, forwarding each notification as a `data:` SSE event.
- Keeps the stream open until the client disconnects or the session ends.
- Only one active GET stream per session is expected; a second concurrent GET may be rejected with 409 or silently coexist (open question).

### DELETE /{destination}/mcp (session termination)

- Requires a valid `Mcp-Session-Id` header; return 400/404 if missing or unknown.
- Sends SIGTERM to the subprocess, drains cleanup, removes the session.
- Returns 204 No Content on success.

### stdout dispatch

The stdout reader must distinguish two kinds of subprocess output:

- **Responses** — JSON objects with an `id` field matching a pending request. Resolve the waiting POST handler's future.
- **Notifications** — JSON objects with no `id` (or `id: null`). Enqueue onto the session's notification queue for the GET SSE stream.

Lines that fail JSON parsing are logged and discarded (same as current behavior).

### Subprocess lifecycle and retries

- Retry policy unchanged: up to 3 restarts with delays `[0.5s, 1.0s, 2.0s]`.
- On restart, pending futures for in-flight requests should be failed immediately (return 503 to those POSTs), as the restarted subprocess has no memory of prior state.
- The per-destination connection cap (`MAX_STDIO_CONNECTIONS`, default 10) continues to apply.

### Backward compatibility

- `GET /{destination}/sse` for a `stdio`-type destination returns 410 Gone (or 404) with a message directing clients to use `/mcp`.
- `POST /{destination}/message` for a `stdio`-type destination returns 410 Gone.
- `sse`-type destinations are unaffected; they continue to use the SSE proxy path.

### Logging

- POST requests: log `mcp_method`, `rpc_id`, `status_code`, `latency_ms`, `request_body`, `response_body` (subject to `AUDIT_LOG_BODIES`).
- GET streams: one log entry at stream close with `latency_ms` and final `status_code`.
- DELETE: one log entry with `status_code` and `latency_ms`.
- Subprocess stderr: unchanged — logged at WARNING, never forwarded.

---

## Possible Edge Cases

- **No `id` in request** — JSON-RPC notifications from client (fire-and-forget). Write to stdin but do not wait for a response; return 202 Accepted.
- **Batch requests** — JSON arrays are valid JSON-RPC. Decide whether to support or reject with 400 for now.
- **Concurrent POSTs with same `id`** — two requests with the same `id` on the same session could collide in the pending map. Later request should return 409 or the map should key on `(session_id, rpc_id)`.
- **GET stream opened before any POST** — client may open GET before sending initialize. The notification queue should buffer until the subprocess is ready (or return 404 if no session yet).
- **Subprocess writes multiple lines before responding** — stdout reader must continue dispatching all lines; the response for a specific `id` may not be the next line written.
- **Very large responses** — impose the same 1 MB chunk guard that the `streamable_http` proxy uses; return 502 if exceeded.
- **Session ID collision** — UUID v4 space is large enough; no special handling needed, but validate format.
- **Subprocess sends malformed JSON** — log and skip; do not crash the session.
- **Client disconnects during POST wait** — the waiting coroutine is cancelled; remove the pending future from the map and do not terminate the subprocess (other sessions may still be active, or it is a per-session subprocess that can be reused).

---

## Acceptance Criteria

- [ ] `stdio` destinations are reachable via `POST /{destination}/mcp` with `type: http` client config.
- [ ] `initialize` request spawns subprocess, returns response with `Mcp-Session-Id` header.
- [ ] Subsequent tool calls via `POST /mcp` with session header are routed correctly.
- [ ] Server notifications are delivered over `GET /mcp` SSE stream.
- [ ] `DELETE /mcp` terminates the subprocess and returns 204.
- [ ] `GET /{destination}/sse` for a stdio destination returns 410.
- [ ] `POST /{destination}/message` for a stdio destination returns 410.
- [ ] All existing tests (SSE proxy, streamable_http proxy, bridge) continue to pass.
- [ ] New test file covers the scenarios in Testing Guidelines below.
- [ ] `CLAUDE.md` and `SETUP.md` updated to reflect the new transport.

---

## Open Questions

- **Per-session or per-destination subprocess?** Current model is one subprocess per SSE connection (per-session). For Streamable HTTP, a per-session model is simpler (no cross-session notification routing), but a per-destination model is more efficient for tools that support concurrent calls. Spec assumes per-session for simplicity; revisit if memory or startup latency is a concern. Lets do per destination.
- **Single or multiple GET streams per session?** MCP spec is silent on this. Reject the second with 409 or allow both to receive the same notifications? allow both.
- **Batch JSON-RPC support?** Out of scope for now; return 400 with a clear message. out of scope.
- **Timeout configurability?** Should the 30 s POST response timeout be a per-destination config value or a global env var? global
- **Should `/sse` return 410 or 404?** 410 Gone is semantically correct (the route existed and was intentionally removed); 404 is simpler. Lean toward 410 with a descriptive body. 410.

---

## Testing Guidelines

Create `tests/test_stdio_streamable_http.py`. Tests should cover the following without being exhaustive:

- `POST /mcp` without `Mcp-Session-Id` spawns subprocess and returns response with `Mcp-Session-Id` header.
- `POST /mcp` with valid session ID routes to existing subprocess.
- `POST /mcp` with unknown session ID returns 404.
- `POST /mcp` with invalid session ID format returns 400.
- Subprocess notification (no `id`) is routed to notification queue, not returned in POST response.
- `GET /mcp` with valid session ID receives notification from notification queue as SSE event.
- `GET /mcp` without session ID returns 400.
- `DELETE /mcp` with valid session ID terminates subprocess and returns 204.
- `GET /sse` for a stdio destination returns 410.
- `POST /message` for a stdio destination returns 410.
- Subprocess exit during pending POST returns 503 after retries exhausted.
- Connection cap enforced: 11th session on a destination returns 503.
