# Python Code Reviewer — Project Memory

## Project: mithril-proxy
SSE/MCP proxy server. Python **3.9.6** (venv confirmed 2026-02-19), FastAPI, uvicorn, httpx, PyYAML.
Runs as a systemd service on a Raspberry Pi (single-process, single event loop).
IMPORTANT: Python 3.9, not 3.11+. asyncio.Lock() binds to running loop on 3.9 — avoid module-level or lazy construction outside a loop.

**Key files:**
- `src/mithril_proxy/config.py` — YAML config loader, module-level `_destinations` dict
- `src/mithril_proxy/logger.py` — JSON structured logger, `threading.Lock` for file writes
- `src/mithril_proxy/proxy.py` — SSE streaming + session map, retry logic, message proxy
- `src/mithril_proxy/main.py` — FastAPI app, lifespan startup
- `tests/test_auth.py`, `tests/test_logging.py` — unit tests

## Confirmed Bug Patterns Found (first review, 2026-02-19)

### Retry loop — two distinct off-by-one issues (proxy.py lines 89–101)
1. **500-response branch** (`attempt < len(_RETRY_DELAYS)`): On the final attempt (attempt=3,
   len=3), condition is False so it falls through to `return response` — silently returns
   the 500 instead of raising. Correct guard is `attempt < len(_RETRY_DELAYS)` if you want
   to skip sleeping on the last attempt but still return; intent seems to be to retry all
   slots, which requires removing the guard or restructuring.
2. **Exception branch** (`attempt <= len(_RETRY_DELAYS)`): Always True (enumerate starts=1,
   max=3=len), so sleep is always called even after the last attempt, wasting 2 s before
   `raise last_exc`.

### asyncio.Lock at module level (proxy.py line 23)
`asyncio.Lock()` created at import time. Safe only when the module is imported inside a
running event loop or in Python 3.10+ (lock no longer binds to a loop). Target is 3.11+
so technically safe, but fragile — if tests import the module before starting an event loop
under Python < 3.10 in a venv, it breaks. Flag for documentation at minimum.

### threading.Lock mixed with async code (logger.py line 17)
`threading.Lock` in `log_request` (called from async context via `finally` in generator).
Holding a threading lock inside an async coroutine blocks the event loop for the duration.
For a single-threaded service on a Pi the latency impact is negligible, but it is
architecturally wrong. Should be `asyncio.Lock` if called only from async code, or
logging should be offloaded to a thread. The concurrent test uses threads and correctly
exercises the lock, but from the production async path the lock provides no benefit while
adding a small blocking hazard.

### data_lines never read (proxy.py lines 152, 184)
Accumulated but never consumed. Multi-line `data:` payloads (valid SSE) will silently
drop all lines except the last one parsed per event. The `data_lines` list is a dead stub.

### Unused imports (proxy.py line 9)
`parse_qs`, `urlencode`, `AsyncIterator` imported but never used.

### `import json` inside function body (proxy.py line 269)
Minor anti-pattern. `json` is stdlib; move to top-level imports.

## Recurring Anti-Patterns in This Codebase
- Module-level mutable globals (`_destinations`, `_session_map`, `_logger`, `_stdio_sessions`)
  create test isolation problems. Tests now have `reset_bridge_state` autouse fixture that
  clears `bridge._stdio_sessions` and resets `bridge._stdio_lock = None` between tests.
- Bare `except Exception: pass` in `handle_message` (proxy.py line 272) silently drops
  JSON decode errors and type errors from `payload.get("method")`.
- `active_tasks = []` reset before `asyncio.gather` completes — recurring pattern that
  creates a task-leak window if GeneratorExit fires during gather. Always keep the full
  task list available in the finally block.

## New Files Added (2026-02-19 review)
- `src/mithril_proxy/bridge.py` — stdio-to-SSE bridge: subprocess lifecycle, 3-task model,
  restart loop (up to 3 retries), session registry with lazy asyncio.Lock
- `src/mithril_proxy/secrets.py` — secrets loader for config/secrets.yml (gitignored)
- `tests/test_bridge.py` — 13 new tests; all pass on Python 3.9.6

## Key Open Issues After Second Review (2026-02-19)
1. Task leak: `active_tasks = []` before `gather` completes (bridge.py ~line 341)
2. `asyncio.Lock` lazy construction in `_get_lock()` — unsafe on Python 3.9 outside event loop
3. Dead import: `get_destination_url` in proxy.py line 15 — no longer used in proxy.py
4. `_drain_stdout_task` annotation `-> AsyncGenerator[bytes, None]` is misleading (should be `AsyncIterator[bytes]` or no return annotation)
5. `_stdin_writer` does not catch bare `OSError` — only `BrokenPipeError`, `ConnectionResetError`
6. Unbounded stdin/stdout queues — memory risk on Pi under high load

## Streamable HTTP Transport (added 2026-02-20, proxy.py lines 343–533)
Three recurring patterns found in this new code:
1. **try/finally splits variable binding from use** — `aread()` in a `try` block, then
   `response_body.decode()` after the `finally`, causes `UnboundLocalError` if `aread()`
   raises. Always keep post-read code inside the same `try` block.
2. **Manual AsyncClient lifecycle instead of `async with`** — `client = httpx.AsyncClient()`
   + explicit `aclose()` leaks on `CancelledError` and unexpected exceptions. Always use
   `async with httpx.AsyncClient(...) as client:`.
3. **`response_headers` computed but discarded in SSE branch** — headers like `Mcp-Session-Id`
   are dropped when returning a `StreamingResponse` with a hardcoded headers dict. Pass
   the full filtered `response_headers` to preserve upstream headers in both branches.

Additional findings:
- `_HOP_BY_HOP` frozenset is a good pattern but does not include `content-length`; the
  existing `_upstream_headers` skip set does. Keep these two sets in sync.
- GET SSE handler catches only `ConnectError`+`TimeoutException`; POST handler also catches
  `RemoteProtocolError`. Both sides should be consistent.
- `env` dict on `streamable_http` config is parsed and stored but never used — silent
  security footgun if operators expect env keys to become request headers.

## See Also
- `patterns.md` — detailed fix snippets for the retry loop
