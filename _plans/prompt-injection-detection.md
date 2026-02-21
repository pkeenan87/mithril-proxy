# Prompt Injection Detection

## Context

Add a two-engine prompt injection detection layer that inspects all inbound MCP request bodies and outbound MCP response bodies before forwarding. The regex engine is fast and deterministic; the AI engine (DeBERTa v3) adds semantic detection at the cost of inference latency. Each destination independently configures both engines via `destinations.yml` using four modes: `off`, `monitor`, `redact`, `block`. The regex engine supports hot-reload via `POST /admin/reload-patterns` without restarting uvicorn.

---

## Key Design Decisions

- **New module `detector.py`** — all detection logic (both engines, pattern loading, hot-reload) lives here; `proxy.py` and `bridge.py` call a single `scan()` function and act on the returned `DetectionResult`
- **AI engine runs in `asyncio.get_event_loop().run_in_executor(None, ...)`** — inference is CPU-bound and blocks for hundreds of milliseconds on Pi; running in the default thread pool executor keeps the asyncio event loop unblocked
- **Atomic pattern swap** — `detector.py` holds patterns in a module-level `list[re.Pattern]` protected by a `threading.Lock`; reload replaces the reference atomically so in-flight requests complete with the old list
- **Redaction placeholder is the literal string `**REDACTED**`** — not configurable; applied by `detector.py` before the body is forwarded or logged
- **AI character cap default is 4000 characters** — payloads longer than this skip AI scanning with a `WARNING` log; overridable per destination via `destinations.yml` as `ai_max_chars`
- **AI confidence threshold defaults to 0.85 globally** — set via `AI_INJECTION_THRESHOLD` env var; per-destination override via `ai_threshold` in `destinations.yml`
- **`POST /admin/reload-patterns`** — unauthenticated but bound to `127.0.0.1` via a separate `admin_app` FastAPI instance on port 3001 (configurable via `ADMIN_PORT` env var); avoids exposing the endpoint to external clients without adding a credential store
- **Detection applies to all destination types** — SSE (`handle_message`), Streamable HTTP (`handle_streamable_http_post`), and stdio (responses from `bridge.py`)
- **`block` on a response** — the upstream response is discarded; `proxy.py` synthesises a JSON-RPC error object with `code: -32603` and `message: "Response blocked by injection filter"`
- **Audit log `redact` compliance** — `log_request()` already receives the (possibly already redacted) body string; callers in `proxy.py` and `bridge.py` must pass the post-redaction body, never the original

---

## Files to Change

| File | Change |
|------|--------|
| `src/mithril_proxy/detector.py` | **New file.** Pattern loader, hot-reload, regex engine, AI engine wrapper, `scan()` entry point, `DetectionResult` dataclass |
| `src/mithril_proxy/config.py` | Add `regex_mode`, `ai_mode`, `ai_threshold`, `ai_max_chars` fields to `DestinationConfig`; parse from `destinations.yml` |
| `src/mithril_proxy/proxy.py` | Call `detector.scan()` on request body before forwarding and on response body before returning; act on `DetectionResult`; pass post-redaction body to `log_request()` |
| `src/mithril_proxy/bridge.py` | Call `detector.scan()` on the response body before resolving each pending future; return a JSON-RPC error future result on `block` |
| `src/mithril_proxy/logger.py` | Add `detection_action`, `detection_engine`, `detection_detail` optional parameters to `log_request()` |
| `src/mithril_proxy/main.py` | Add startup calls to `detector.init_detector()` and `detector.load_patterns()`; register `POST /admin/reload-patterns` route (or mount admin app); register `SIGHUP` handler as an alias for reload |
| `requirements.txt` | Add `transformers>=4.40.0` and `torch` (CPU-only) for the AI engine; mark both as optional with install instructions |
| `config/destinations.yml` | Add commented example showing `regex_mode`, `ai_mode`, `ai_threshold`, `ai_max_chars` fields |
| `tests/test_detection.py` | **New file.** Unit and integration tests for both engines and all modes |

---

## Implementation Steps

### 1. Add detection config fields to `DestinationConfig`

- In `config.py`, add four optional fields to `DestinationConfig`:
  - `regex_mode: str = "off"` — accepted values: `off`, `monitor`, `redact`, `block`
  - `ai_mode: str = "off"` — same accepted values
  - `ai_threshold: Optional[float] = None` — per-destination override; `None` means use global default
  - `ai_max_chars: int = 4000` — character cap for AI scanning
- In `load_config()`, parse these four fields from each destination entry dict
- Validate that `regex_mode` and `ai_mode` are one of the four accepted values; raise `ValueError` on unknown values
- All four fields are optional in the YAML; missing means the dataclass default applies

### 2. Create `detector.py` — pattern loader and hot-reload

- Define `_patterns: list[re.Pattern]` and `_patterns_lock: threading.Lock` at module level
- Define `PATTERNS_DIR` constant; resolve from `PATTERNS_DIR` env var, defaulting to `/etc/mithril-proxy/patterns.d/`
- Write `load_patterns(patterns_dir: Path | None = None) -> int`:
  - Glob all `*.txt` and `*.conf` files in the directory (sorted for determinism)
  - Read each file line by line; skip blank lines and lines starting with `#`
  - Compile each non-empty line as a `re.Pattern` with `re.IGNORECASE`; on `re.error`, log a `WARNING` with the offending line and file name, and skip it
  - If the directory does not exist, log a `WARNING` and return 0 without raising
  - Under `_patterns_lock`, atomically replace `_patterns` with the new compiled list
  - Return the count of successfully loaded patterns
- Write `reload_patterns() -> int` — thin wrapper over `load_patterns()` for the admin endpoint

### 3. Create `detector.py` — AI engine

- Define `_ai_pipeline` module-level variable, initially `None`
- Define `AI_INJECTION_THRESHOLD` constant; read from `AI_INJECTION_THRESHOLD` env var, defaulting to `0.85`
- Write `init_detector() -> None`:
  - Attempt `from transformers import pipeline` and instantiate `pipeline("text-classification", model="protectai/deberta-v3-base-prompt-injection-v2")`
  - Store the result in `_ai_pipeline`
  - On any `ImportError` or `OSError`, log a `WARNING` ("AI engine unavailable: {exc}") and leave `_ai_pipeline = None`
- Write `_run_ai(text: str) -> float`:
  - If `_ai_pipeline is None`, return `0.0`
  - Call the pipeline on `text`; parse the returned label and score
  - Return the confidence score if the label indicates injection, else `0.0`
  - Wrap in `try/except Exception` and return `0.0` on any error, logging a `WARNING`

### 4. Create `detector.py` — `DetectionResult` and `scan()`

- Define `DetectionResult` as a dataclass:
  - `action: str` — one of `"pass"`, `"monitor"`, `"redact"`, `"block"`
  - `engine: Optional[str]` — `"regex"`, `"ai"`, or `None`
  - `detail: Optional[str]` — matched pattern string or confidence score string
  - `body: str` — the (possibly redacted) body to forward
- Write `scan(body: str, dest_config: DestinationConfig, *, is_response: bool = False) -> DetectionResult`:
  - If `body` is empty or both modes are `off`, return `DetectionResult(action="pass", ...)`
  - **Regex pass**: if `dest_config.regex_mode != "off"`, iterate `_patterns` under a read of `_patterns_lock` (copy the list reference first so the lock is not held during matching); check `pattern.search(body)` for each; on first match, record `engine="regex"` and `detail=pattern.pattern`; apply the mode action
  - **AI pass**: if `dest_config.ai_mode != "off"` and no blocking result yet:
    - If `len(body) > dest_config.ai_max_chars`, log `WARNING` ("AI scan skipped: body exceeds {ai_max_chars} chars") and skip AI
    - Otherwise, run `_run_ai(body)` in `asyncio.get_event_loop().run_in_executor(None, _run_ai, body)` — caller must `await` this; `scan()` itself must be `async`
    - Resolve effective threshold: `dest_config.ai_threshold if dest_config.ai_threshold is not None else AI_INJECTION_THRESHOLD`
    - If score >= threshold, record `engine="ai"` and `detail=f"score={score:.3f}"`; apply the mode action
  - **Action application**:
    - `off` — never reached (guarded above)
    - `monitor` — `action="monitor"`, `body=body` unchanged
    - `redact` — replace the matched region (regex: replace `pattern.sub("**REDACTED**", body)`; AI: replace entire body with `"**REDACTED**"`) and set `action="redact"`
    - `block` — set `action="block"`, `body=body` (body is discarded by caller anyway)
  - If both engines trigger with different modes, the **stricter mode wins** (`block` > `redact` > `monitor`)
  - Return `DetectionResult`

### 5. Update `logger.py` — detection fields

- Add three optional keyword parameters to `log_request()`:
  - `detection_action: Optional[str] = None`
  - `detection_engine: Optional[str] = None`
  - `detection_detail: Optional[str] = None`
- When any of these are not `None`, include them in the `extra` dict passed to the logger

### 6. Update `proxy.py` — request scanning on all POST endpoints

- In `handle_message()` (SSE POST):
  - After reading `body`, call `result = await detector.scan(body_str, dest_config)`
  - If `result.action == "block"`: log with detection fields and return a `JSONResponse(400, {"jsonrpc":"2.0","error":{"code":-32600,"message":"Request blocked by injection filter"},"id": rpc_id})`
  - If `result.action in ("monitor", "redact")`: use `result.body` as the forwarded body; pass detection fields to `log_request()`
- Apply the same pattern in `handle_streamable_http_post()` immediately after the body is read and parsed

### 7. Update `proxy.py` — response scanning on all POST endpoints

- In `handle_message()` and `handle_streamable_http_post()` (non-SSE branch):
  - After reading `response_body`, call `result = await detector.scan(response_body_str, dest_config, is_response=True)`
  - If `result.action == "block"`: log with detection fields; return synthesised JSON-RPC error: `{"jsonrpc":"2.0","error":{"code":-32603,"message":"Response blocked by injection filter"},"id": rpc_id}`
  - If `result.action in ("monitor", "redact")`: use `result.body` as the body passed to `log_request()` and returned to the client
- SSE streaming responses (`text/event-stream`) are not scanned — individual SSE frames cannot be easily reconstructed into JSON-RPC objects mid-stream; document this limitation in a code comment

### 8. Update `bridge.py` — response scanning on stdio destinations

- In `_stdio_stdout_reader`, after reading a stdout line and before resolving the pending future, call `result = await detector.scan(line_str, dest_config, is_response=True)` where `dest_config` is looked up from the bridge's destination name
- The bridge does not have direct access to `dest_config`; pass it into `StdioDestinationBridge` at construction time (it is already available at `init_bridge()`)
- On `block`: resolve the future with a synthesised JSON-RPC error body instead of the original line
- On `redact`: resolve the future with `result.body` (the redacted version)
- On `monitor`: resolve the future normally; log detection fields via `_log` (the bridge's logger)

### 9. Update `main.py` — startup and admin endpoint

- In `lifespan()`, after `setup_logging()`: call `detector.load_patterns()` then `detector.init_detector()` (in that order; pattern load is synchronous and fast)
- Add a `POST /admin/reload-patterns` route that calls `detector.reload_patterns()` and returns `{"loaded": n}` where `n` is the pattern count
- Bind the admin route to `127.0.0.1` only: create a second `uvicorn` server instance in the lifespan context on `ADMIN_PORT` (default 3001), or use FastAPI's `add_middleware` with an IP-check dependency on the route
- Register a `signal.signal(signal.SIGHUP, lambda *_: asyncio.create_task(asyncio.to_thread(detector.reload_patterns)))` in the lifespan startup block as a convenience alias

### 10. Update `requirements.txt` and `config/destinations.yml`

- In `requirements.txt`, add a comment block explaining that `transformers` and `torch` (CPU) are optional; add them commented out with the install command `pip install 'transformers>=4.40.0' torch --index-url https://download.pytorch.org/whl/cpu`
- In `config/destinations.yml`, add a commented example showing the four new fields under a stdio destination

---

## Verification

1. Start the server with a pattern file containing a known injection string; send a POST with a matching body; confirm `monitor` mode logs `detection_action: "monitor"` without altering the forwarded request
2. Switch to `redact` mode; repeat; confirm the forwarded body contains `**REDACTED**` and the audit log does not contain the original string
3. Switch to `block` mode; confirm a `400` JSON-RPC error is returned and the upstream never receives the request (check upstream access logs or mock)
4. Add a new pattern file while the server is running; `POST /admin/reload-patterns`; send a matching request; confirm the new pattern is active
5. Put a syntactically invalid regex in a pattern file; reload; confirm the server continues running and logs a `WARNING` for the bad pattern
6. If transformers is installed: send a known injection string; confirm AI engine detects it above threshold in `monitor` mode
7. If transformers is not installed: confirm server starts with a `WARNING` and AI mode is effectively `off`
8. Run the full test suite: `PYTHONPATH=src .venv/bin/pytest tests/ -v`
9. Check response scanning: configure a destination in `block` mode; mock the upstream to return a body containing a known pattern; confirm the client receives the synthesised error and not the upstream body
