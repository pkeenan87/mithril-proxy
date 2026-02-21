# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
PYTHONPATH=src .venv/bin/pytest tests/ -v

# Run a single test file
PYTHONPATH=src .venv/bin/pytest tests/test_bridge.py -v

# Run a single test by name
PYTHONPATH=src .venv/bin/pytest tests/test_stdio_streamable_http.py::test_first_post_creates_session -v

# Start the server locally (logs to _logs/proxy.log via .env)
PYTHONPATH=src .venv/bin/uvicorn mithril_proxy.main:app --port 3000

# Install dependencies into existing venv
.venv/bin/pip install -r requirements.txt
```

`PYTHONPATH=src` is required because the package is under `src/` with no editable install.

The `.env` file sets `LOG_FILE=_logs/proxy.log` — that directory is gitignored and created at runtime.

## Architecture

**Runtime:** Python 3.9.6, FastAPI, asyncio, single process, single event loop.

**Request flow:** MCP clients send JSON-RPC via `POST /{destination}/mcp` (Streamable HTTP transport) or via legacy SSE (`GET /{destination}/sse` + `POST /{destination}/message`). stdio destinations only support the Streamable HTTP path — `GET /sse` and `POST /message` return 410 Gone for them. The destination name is the YAML key in `config/destinations.yml`.

### Three destination types

**SSE destinations** (`type: sse`) — proxy to a remote HTTP/SSE server. `proxy.py` opens a persistent upstream SSE stream, rewrites the `event: endpoint` data line to replace the upstream message URL with the proxy's own `/{destination}/message?session_id=...`, and registers a `_session_map` entry mapping that session ID to the real upstream message URL.

**Streamable HTTP destinations** (`type: streamable_http`) — proxy to a remote server using the modern MCP Streamable HTTP transport. Clients POST JSON-RPC to `POST /{destination}/mcp`; the proxy forwards to the upstream URL and streams back either a JSON response or an SSE stream depending on the upstream `Content-Type`. `GET /{destination}/mcp` provides optional SSE listen support. No session rewriting needed.

**stdio destinations** (`type: stdio`) — bridge to a local subprocess (e.g. `npx -y @upstash/context7-mcp`) using the Streamable HTTP transport (`POST/GET/DELETE /{destination}/mcp`). `bridge.py` spawns **one subprocess per destination** shared across all sessions. `StdioDestinationBridge` holds all state:
- `pending: dict[int, (Future, original_id)]` — in-flight POST requests awaiting stdout responses
- `notification_queues: dict[str, Queue]` — one bounded queue per active `GET /mcp` stream
- `sessions: set[str]` — active `Mcp-Session-Id` values
- `_counter` — monotonically-increasing internal ID to prevent cross-client collision

Each POST assigns an `internal_id` to the outgoing JSON-RPC request; the subprocess sees only internal IDs. When a stdout line's `id` matches a pending entry, the future is resolved with the original client `id` restored. Lines with no matching `id` (notifications) are broadcast to all active GET streams.

Two long-lived tasks per bridge: `_stdio_stdout_reader` (dispatches responses and notifications), `_stderr_reader` (logs warnings). On subprocess exit, `_stdio_stdout_reader` restarts up to 3 times (`_RETRY_DELAYS = [0.5, 1.0, 2.0]`), then fails all pending futures (503) and closes all GET streams.

### Module map

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app + lifespan (startup order: `load_config` → `load_secrets` → `setup_logging` → `init_bridge` → `validate_stdio_commands`) |
| `config.py` | Parses `destinations.yml` into `DestinationConfig` dataclasses; rejects shell metacharacters in stdio commands |
| `secrets.py` | Loads `config/secrets.yml` (gitignored); supplies per-destination env vars injected into subprocesses |
| `proxy.py` | SSE proxy + session map for SSE-type destinations; dispatches stdio destinations to `bridge.py`; `handle_streamable_http_post()`, `handle_streamable_http_get()`, and `handle_streamable_http_delete()` for Streamable HTTP destinations; returns 410 for `GET /sse` and `POST /message` on stdio destinations |
| `bridge.py` | stdio-to-Streamable-HTTP bridge: per-destination `StdioDestinationBridge` dataclass, subprocess lifecycle, internal ID rewriting, pending future dispatch, notification queue broadcast, session management, shutdown |
| `logger.py` | Newline-delimited JSON log writer; `log_request()` is the single call site for all request logging; supports `AUDIT_LOG_BODIES` flag, `rpc_id`, `request_body`, `response_body` fields, and 32 KB truncation |
| `utils.py` | Shared request helpers (`source_ip()`); X-Forwarded-For is intentionally ignored — no trusted upstream proxy in this deployment |

### Security constraints in bridge.py

- Subprocess env uses `_SAFE_ENV_KEYS` allowlist — only `PATH`, `HOME`, `USER`, `NPM_CONFIG_CACHE`, etc. are inherited from the parent process; secrets come exclusively from `secrets.yml` via `extra_env`.
- `_UUID4_RE` validates `Mcp-Session-Id` format before any session lookup.
- Per-destination connection cap: `_MAX_CONNECTIONS_PER_DEST` (default 10, override with `MAX_STDIO_CONNECTIONS` env var) — enforced on first POST (no session header).
- `notification_queues` values are bounded `asyncio.Queue(maxsize=256)` — full queues silently drop notifications.
- `source_ip()` uses only `request.client.host` — `X-Forwarded-For` is not trusted (no upstream reverse proxy in this deployment).

### Config files

| Path | Purpose |
|---|---|
| `config/destinations.yml` | Destination definitions (committed; contains no secrets) |
| `config/secrets.yml` | Per-destination env vars for stdio subprocesses (gitignored; missing file is OK) |
| `.env` | Local overrides loaded via `python-dotenv` at import time in `main.py` |

**`secrets.yml` format:**
```yaml
context7:
  CONTEXT7_API_KEY: sk-...
```

**`destinations.yml` format:**
```yaml
destinations:
  github:
    type: streamable_http
    url: https://api.githubcopilot.com/mcp
  legacy:
    type: sse
    url: https://mcp.example.com/legacy
  context7:
    type: stdio
    command: npx -y @upstash/context7-mcp
```

### Test conventions

- `pytest-asyncio` with `mode=strict` — every async test needs `@pytest.mark.asyncio`.
- `reset_bridge_state` is an `autouse` fixture in `test_bridge.py`, `test_stdio_streamable_http.py`, and `test_audit_logging.py` that terminates subprocesses, clears `_stdio_bridges`, and resets `_bridges_create_lock` between tests. It is synchronous (not async) because each pytest-asyncio test runs in its own event loop.
- Use `httpx.ASGITransport(app=app)` for async HTTP tests; `TestClient` for sync tests.
- Session IDs in tests must match `_UUID4_RE` (`00000000-0000-4000-8000-000000000001` is a valid test UUID).
- Fixture commands passed through `load_config()` are subject to the shell metacharacter check — write a script file to `tmp_path` instead of using Python `-c` one-liners.

### Deployment

Production target is Raspberry Pi OS. `install.sh` creates a `mithril` system user (no home directory), installs to `/opt/mithril-proxy`, and registers a systemd service (`systemd/mithril-proxy.service`). Config lives in `/etc/mithril-proxy/`, logs in `/var/log/mithril-proxy/proxy.log`, npm cache in `/var/cache/mithril-proxy/.npm` (set via `NPM_CONFIG_CACHE` in the env file so stdio subprocesses can write their npm cache without a home directory).
