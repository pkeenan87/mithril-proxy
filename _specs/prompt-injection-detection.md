# Spec for Prompt Injection Detection

branch: claude/feature/prompt-injection-detection

## Summary

Add a layered prompt injection detection system to mithril-proxy that inspects both incoming MCP requests and outgoing MCP responses. Two complementary detection engines are supported: regex pattern matching (fast, deterministic) and AI-based classification using `protectai/deberta-v3-base-prompt-injection-v2`. Each destination can independently configure detection behaviour per engine via `destinations.yml`. Regex patterns are hot-reloadable from `/etc/mithril-proxy/patterns.d/` without a full service restart.

## Functional Requirements

- Inspect the `params` / `result` fields of JSON-RPC request and response bodies at every proxied endpoint (POST `/mcp`, POST `/message`)
- Support two detection engines, configurable independently per destination:
  - **Regex engine** — match against patterns loaded from flat files in `/etc/mithril-proxy/patterns.d/`
  - **AI engine** — run inference via `protectai/deberta-v3-base-prompt-injection-v2` (HuggingFace transformers)
- Each engine supports four modes, set per destination in `destinations.yml`:
  - `off` — engine is disabled; no scanning
  - `monitor` — scan and log detections; request/response passes through unchanged
  - `redact` — replace matched content with a placeholder (e.g. `[REDACTED]`) before forwarding
  - `block` — return an error response immediately; do not forward the request or response
- Regex patterns hot-reload: send `SIGHUP` to the process (or hit a reload endpoint) to re-read all `*.txt` / `*.conf` files in `patterns.d/` without restarting uvicorn
- All detection actions (match found, mode applied, engine used, pattern or confidence score) are appended to the existing structured JSON log line for the request

## Figma Design Reference

N/A — backend feature, no UI.

## Possible Edge Cases

- JSON-RPC batch requests (array body): each item must be scanned individually
- Notifications (no `id` field) still carry user-supplied `params` and must be scanned
- Very large payloads: AI inference on multi-KB strings may be slow on Raspberry Pi; need a configurable character limit above which AI scanning is skipped (with a log warning)
- Pattern files with syntax errors: bad regex must be logged and skipped, not crash the loader
- Pattern directory missing at startup: should log a warning and continue (regex engine effectively has zero patterns)
- AI model not installed / import fails: server must still start; AI engine falls back to `off` with a startup warning
- Concurrent requests during a pattern reload: in-flight requests must complete with the old pattern set; new set is swapped atomically
- `redact` mode on a response: the MCP client receives the redacted body; the original must NOT be logged (audit log must also redact)
- `block` mode on a response (upstream already answered): proxy must discard the upstream response and synthesise a JSON-RPC error response

## Acceptance Criteria

- A request whose body matches a loaded regex pattern is handled according to the configured mode for the regex engine on that destination
- A request classified as injection by the AI model (above a configurable confidence threshold) is handled according to the configured mode for the AI engine on that destination
- `monitor` mode: request passes through; log line contains `detection_action: "monitor"`, engine name, and match detail
- `redact` mode: forwarded body has sensitive content replaced; log line contains `detection_action: "redact"`; audit log does not contain the original content
- `block` mode: upstream never receives the request (or client never receives the response); a JSON-RPC error is returned; log line contains `detection_action: "block"`
- `off` mode: no scanning occurs; no performance overhead beyond a mode check
- After `SIGHUP` (or reload trigger), newly added or modified pattern files are active within one request cycle; in-flight requests are unaffected
- Pattern files with invalid regex are skipped with a `WARNING` log entry; the server continues running
- If the AI model is unavailable, the server starts normally with the AI engine treated as `off`; a `WARNING` is emitted at startup
- All detection log fields are present alongside existing fields (`user`, `source_ip`, `destination`, `mcp_method`, `status_code`, `latency_ms`)

## Open Questions

- Should the AI engine run synchronously (blocking the request) or in a thread pool executor to avoid blocking the asyncio event loop? (Likely thread pool given model inference time on Pi.)
- What is the redaction placeholder string? Should it be configurable per destination? it should be **REDACTED**.
- Should there be a per-destination character-length cap for AI scanning, and what is the default? yes, I will take you suggestion for default.
- Should the reload mechanism be `SIGHUP` only, or also expose a `POST /admin/reload-patterns` HTTP endpoint (auth-gated)? lets do the post request.
- Is the confidence threshold for the AI model global or per-destination? global default with per destination override.
- Should detection apply to SSE-type destinations as well, or only Streamable HTTP and stdio? it should apply to all types.

## Testing Guidelines

Create test files in `./tests/` covering:

- Regex engine: a matching payload in `monitor` mode passes through with correct log fields
- Regex engine: a matching payload in `redact` mode has content replaced in the forwarded body
- Regex engine: a matching payload in `block` mode returns a JSON-RPC error; upstream not called
- Regex engine: `off` mode — no detection fields in log
- Pattern hot-reload: patterns updated in `patterns.d/`; reload triggered; new pattern is active
- Pattern file with invalid regex: server continues; warning logged; other patterns still active
- AI engine: mock model returns high-confidence injection; `block` mode returns error
- AI engine: model unavailable at startup; server starts; AI engine is `off`
- Response scanning: upstream response containing injection pattern is handled per mode
- Audit log: `redact` mode does not log original content
