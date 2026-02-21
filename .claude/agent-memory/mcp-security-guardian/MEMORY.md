# MCP Security Guardian — Agent Memory

## Project: mithril-proxy

**Stack**: Python 3.11+, FastAPI, uvicorn, httpx, PyYAML. Runs as systemd service on Raspberry Pi.

**Key files**:
- `src/mithril_proxy/proxy.py` — SSE proxy, session map, header forwarding, URL construction
- `src/mithril_proxy/config.py` — YAML config loader (uses yaml.safe_load — CORRECT)
- `src/mithril_proxy/logger.py` — JSON structured logger; _JsonFormatter dumps all extra LogRecord fields
- `src/mithril_proxy/main.py` — FastAPI app wiring
- `install.sh` — bootstrap script (runs as root)
- `systemd/mithril-proxy.service` — no systemd hardening directives

## Confirmed Vulnerability Patterns (first review, 2026-02-19)

### SSRF / URL Construction
- `_build_upstream_message_url` (proxy.py:220-227): if upstream SSE sends `endpoint` event with
  an absolute URL (starts with "http"), that URL is used verbatim as the registered session URL.
  A malicious upstream can redirect all subsequent POST /message calls to any internal host.
  Pattern: check `startswith("http")` but never validate against allowed-hosts allowlist.

### Session ID — no validation, collision possible
- Session IDs sourced directly from upstream SSE data (proxy.py:166, regex group 1).
  No format enforcement, no length cap, no rate-limit on registrations.
  An adversarial upstream can emit arbitrary strings as session IDs, including ones that
  collide with legitimate sessions (map key overwrite).

### X-Forwarded-For blind trust
- `_source_ip` (proxy.py:53-58) trusts the first value in X-Forwarded-For unconditionally.
  No check that the direct peer is a trusted proxy.

### Header forwarding — missing response header strip
- `_upstream_headers` skip list (proxy.py:67) only removes `host`, `content-length`,
  `transfer-encoding` on the REQUEST side. Does not strip `cookie` or `set-cookie`.
  On the RESPONSE side (proxy.py:287-290) strips transfer-encoding/connection/keep-alive
  but forwards `set-cookie` from upstream to the client — session fixation risk.

### SSE injection — raw_line forwarded verbatim
- Non-endpoint data lines are yielded verbatim (proxy.py:179). A malicious upstream can
  inject crafted SSE fields (extra `event:` lines, `retry:`, comment lines with prompt
  injection text) directly into the client stream.

### Internal detail in 502 error response
- `handle_message` (proxy.py:308): `"detail": str(exc)` exposes exception text to callers.

### systemd — no hardening
- `mithril-proxy.service` lacks NoNewPrivileges, PrivateTmp, ProtectSystem, ProtectHome,
  ReadWritePaths. Service also binds 0.0.0.0 unconditionally.

### install.sh — INSTALL_DIR permissions
- `chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR` (line 32) gives the service user
  write access to its own virtualenv and source. A compromise of the process allows
  modifying installed Python packages for persistence.

## Confirmed Vulnerability Patterns (stdio bridge, 2026-02-19)

### Duplicate import in proxy.py
- `from .config import get_destination, get_destination_url` appears TWICE on consecutive
  lines (proxy.py:15-16). Cosmetic but introduced by the diff; should be deduplicated.

### No subprocess cap (DoS)
- `bridge.py`: every SSE connection spawns its own subprocess with no max-connections check.
  An attacker can open N connections to exhaust process table, file descriptors, or memory.
  Pattern: need a per-destination semaphore or global cap before `_spawn_process`.

### Full os.environ inheritance (secrets leakage)
- `_spawn_process` (bridge.py:117): `env = {**os.environ, **extra_env}` passes ALL parent
  env vars (including DATABASE_URL, AWS credentials, dotenv-loaded secrets) to child.
  Subprocess stdout/stderr is attached; any of those vars printed to stderr leak into logs.
  Pattern: build a clean minimal env (PATH, HOME, USER, LANG, TMPDIR) and merge only
  explicitly declared vars.

### shlex.split on YAML-controlled string (command injection surface)
- `shlex.split(command)` is called on a YAML-controlled string (bridge.py:116, 100).
  shlex itself does not execute a shell, but glob characters and path separators in the
  first token pass through to execv unchanged. validate_stdio_commands checks only that
  the executable exists on PATH; it does NOT prevent trailing shell metacharacters injected
  via config (e.g. `npx; rm -rf /`). Since YAML is operator-controlled this is lower
  severity than true user-controlled injection, but CI pipelines that auto-apply PR config
  changes are vulnerable.
  Pattern: allowlist executable basenames; reject any command containing shell metacharacters.

### Session ID not validated in handle_stdio_message (cross-session hijack)
- `handle_stdio_message` (bridge.py:448) receives session_id directly from
  `request.query_params` with no format check. Any client that knows or guesses a UUID
  can write to another client's subprocess stdin. UUIDs are not secret (they're sent in
  the SSE endpoint event). No ownership check ties the session to the originating connection.
  Pattern: bind session to source IP or an opaque bearer token at registration time.

### Duplicate import line in proxy.py
- proxy.py lines 15-16 import `get_destination, get_destination_url` twice.

## Confirmed Vulnerability Patterns (streamable_http transport, 2026-02-20)

### httpx.AsyncClient resource leak on unexpected exceptions
- `handle_streamable_http_post` (proxy.py:387): `client = httpx.AsyncClient(...)` created
  before try/except. Only 3 exception types release it; any other exception leaks the client
  and upstream TCP connection. Pattern: wrap the full client lifetime in try/finally.

### 502 detail re-introduced in streamable_http POST handler
- proxy.py:409: `"detail": str(exc)` leaks upstream hostname and OS socket error text.
  Same issue as handle_message; must be gated on a dev-mode env flag.

### set-cookie forwarded from upstream JSON response to client
- proxy.py:414-416: `_HOP_BY_HOP` frozenset omits `set-cookie`, `www-authenticate`.
  On the JSON (non-SSE) path, full upstream response headers including set-cookie are
  forwarded. SSE path accidentally avoids this (discards response_headers). Fix: add
  set-cookie, www-authenticate, proxy-authenticate to the strip set.

### No concurrency cap on streamable_http connections
- Each POST and GET /mcp call creates a new httpx.AsyncClient with no semaphore or counter.
  GET handler uses timeout=None — connections held indefinitely. DoS via N concurrent GETs.
  Pattern: per-destination asyncio.Semaphore with same _MAX_CONNECTIONS_PER_DEST constant.

### URL scheme not validated at config load for streamable_http
- config.py:107-117: only checks non-empty string. file://, http://localhost/admin, and
  RFC-1918 addresses accepted. Pattern: urlparse + scheme allowlist {"http","https"} at
  config load time; optionally a host allowlist.

### try/finally around aread() swallows error log
- proxy.py:445-449: if upstream.aread() raises, finally closes connections but log_request
  is never called — unhandled exception surfaces as 500 with no structured log record.
  Pattern: initialise response_body=b"" before try; move log_request into finally.

## Patterns To Reuse

- Always validate session IDs against a strict regex `^[A-Za-z0-9_-]{8,128}$` before
  storing in the session map.
- Use an allowlist of upstream base URLs when resolving absolute URLs from upstream data.
- Strip `cookie`, `set-cookie`, `x-forwarded-for`, `x-real-ip` from upstream headers before
  forwarding to prevent header injection and session fixation.
- For SSE proxying: validate each line prefix against the SSE spec set
  `{event:, data:, id:, retry:, :}` and discard or escape anything else.
- Always add `detail` field only in development mode; gate on an env flag.
- For subprocess spawning: use a clean minimal env (PATH, HOME, USER, LANG, TMPDIR) as
  base; never inherit full os.environ. Use a semaphore to cap concurrent subprocesses.
- session_id from query params must be validated (UUID regex) AND ownership-checked before
  being used to look up another connection's subprocess.

See `patterns.md` for extended notes.
