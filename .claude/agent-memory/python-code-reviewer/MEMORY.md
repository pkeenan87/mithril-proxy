# Python Code Reviewer — Project Memory

## Project: mithril-proxy
SSE/MCP proxy server. Python 3.11+, FastAPI, uvicorn, httpx, PyYAML.
Runs as a systemd service on a Raspberry Pi (single-process, single event loop).

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
- Module-level mutable globals (`_destinations`, `_session_map`, `_logger`) create test
  isolation problems when tests share the same process. Tests in `test_logging.py` work
  around this manually by saving/restoring `_logger`; `_session_map` has no such guard.
- Bare `except Exception: pass` in `handle_message` (proxy.py line 272) silently drops
  JSON decode errors and type errors from `payload.get("method")`.

## See Also
- `patterns.md` — detailed fix snippets for the retry loop
