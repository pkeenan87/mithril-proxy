# Spec for MCP Message Audit Logging

branch: claude/feature/mcp-message-audit-logging

## Summary

Extend the proxy's structured JSON log to capture the full JSON-RPC request body sent by MCP clients and the full JSON-RPC response body returned by upstream MCP servers. This gives operators a complete audit trail of all MCP tool calls, arguments, and results flowing through the proxy — useful for debugging, compliance, and usage analysis.

## Functional Requirements

- When a client POSTs a message to `/{destination}/message`, log the full parsed request body as a `request_body` field alongside the existing log fields.
- When the upstream (SSE or stdio) returns a response to that message, log the full parsed response body as a `response_body` field.
- Both fields must appear in the same log line as the existing `user`, `source_ip`, `destination`, `mcp_method`, `status_code`, and `latency_ms` fields.
- Logging must apply to both destination types: SSE upstreams (proxy.py) and stdio subprocesses (bridge.py).
- For stdio destinations, responses arrive asynchronously over the SSE stream rather than as a direct HTTP response body. Each `data:` line that is valid JSON should be logged as a `response_body` entry correlated to the session.
- If the request body cannot be parsed as JSON (malformed payload), log `request_body` as `null` and continue — do not reject the request.
- If the response body cannot be parsed as JSON, log `response_body` as `null`.
- Body logging must be controlled by a configuration flag so operators can disable it if payloads are too large or contain data they do not want persisted. Default: enabled.

## Edge Cases

- **Large payloads:** Tool responses (e.g. document retrieval) can be very large. The logger should truncate `request_body` and `response_body` at a configurable byte limit (default 32 KB) and add a `truncated: true` flag to the log entry when truncation occurs.
- **Binary or non-UTF-8 content:** Log `response_body: null` and add a `decode_error: true` flag rather than crashing.
- **Streaming / batched stdio responses:** A single client message may produce multiple `data:` lines from a stdio subprocess (e.g. progress notifications followed by the final result). Each line should be logged as a separate `response_body` entry with the same `session_id` for correlation.
- **SSE-type messages with no upstream response:** The upstream returns 202 Accepted with no body. Log `response_body: null`.
- **Secrets in payloads:** API keys passed as tool arguments will appear in `request_body`. Operators should be warned in documentation that enabling audit logging may persist sensitive data to disk.

## Acceptance Criteria

- A POST to `/{destination}/message` produces a log line containing `request_body` with the full parsed JSON-RPC object.
- The corresponding upstream response produces a log line containing `response_body` with the full parsed JSON-RPC result object.
- Both fields are absent (not `null`, fully absent) when body logging is disabled via the config flag.
- When a payload exceeds the byte limit, the log entry contains `"truncated": true` and `request_body` / `response_body` are omitted or replaced with a truncation marker.
- Existing log fields (`user`, `source_ip`, `destination`, `mcp_method`, `status_code`, `latency_ms`) are unaffected.
- stdio and SSE destinations both produce audit log entries.
- The proxy continues to function correctly if the log file is not writable (audit logging failure must not affect request handling).

## Open Questions

- Should `request_body` and `response_body` be nested objects in the JSON log line, or serialised as strings? Nested objects are easier to query with `jq` but may complicate log parsers that expect flat records. strings.
- Is 32 KB the right default truncation limit for the Pi's storage constraints? The SD card write rate may be a bottleneck under high load. yes.
- For stdio destinations, should each `data:` line be a separate log entry, or should responses be buffered and logged once when a JSON-RPC `id` is detected in the response (matching it to the originating request)? separate log entry.
- Do we need request/response correlation IDs (JSON-RPC `id` field) surfaced as a top-level log field to make it easier to join request and response log lines? yes.

## Testing Guidelines

Create tests in `tests/test_audit_logging.py` covering:

- Request body appears in the log line for a POST to an SSE destination.
- Response body appears in the log line for the upstream reply.
- Request body appears in the log line for a POST to a stdio destination.
- Response body appears in the log line when a stdio subprocess emits a JSON line.
- Malformed (non-JSON) request body logs `request_body: null` without raising.
- Payload exceeding the byte limit logs `truncated: true`.
- When audit logging is disabled via config flag, `request_body` and `response_body` are absent from all log entries.
