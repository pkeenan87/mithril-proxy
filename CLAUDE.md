# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
PYTHONPATH=src .venv/bin/pytest tests/ -v

# Run a single test file
PYTHONPATH=src .venv/bin/pytest tests/test_bridge.py -v

# Run a single test by name
PYTHONPATH=src .venv/bin/pytest tests/test_bridge.py::TestHandleStdioMessage::test_unknown_session_returns_404 -v

# Start the server locally (logs to _logs/proxy.log via .env)
PYTHONPATH=src .venv/bin/uvicorn mithril_proxy.main:app --port 3000

# Install dependencies into existing venv
.venv/bin/pip install -r requirements.txt
```

`PYTHONPATH=src` is required because the package is under `src/` with no editable install.

The `.env` file sets `LOG_FILE=_logs/proxy.log` — that directory is gitignored and created at runtime.

## Architecture

**Runtime:** Python 3.9.6, FastAPI, asyncio, single process, single event loop.

**Request flow:** MCP clients connect via SSE (`GET /{destination}/sse`) then POST JSON-RPC messages (`POST /{destination}/message?session_id=<uuid>`). The destination name is the YAML key in `config/destinations.yml`.

### Two destination types

**SSE destinations** (`type: sse`) — proxy to a remote HTTP/SSE server. `proxy.py` opens a persistent upstream SSE stream, rewrites the `event: endpoint` data line to replace the upstream message URL with the proxy's own `/{destination}/message?session_id=...`, and registers a `_session_map` entry mapping that session ID to the real upstream message URL.

**stdio destinations** (`type: stdio`) — bridge to a local subprocess (e.g. `npx -y @upstash/context7-mcp`). `bridge.py` spawns one subprocess per SSE connection. Three concurrent asyncio tasks handle I/O:
- `_stdout_reader` → `out_queue` → `event_stream` generator (yields `data:` SSE chunks)
- `_stdin_writer` ← `session.stdin_queue` ← `handle_stdio_message` (POST body enqueued here)
- `_stderr_reader` → `log.warning` (never forwarded to client)

The intermediate `asyncio.Queue` is required because the async generator (`event_stream`) cannot be fed directly by another coroutine. On subprocess exit, the bridge restarts up to 3 times (`_RETRY_DELAYS = [0.5, 1.0, 2.0]`), then emits an `event: error` and closes.

### Module map

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app + lifespan (startup order: `load_config` → `load_secrets` → `setup_logging` → `init_bridge` → `validate_stdio_commands`) |
| `config.py` | Parses `destinations.yml` into `DestinationConfig` dataclasses; rejects shell metacharacters in stdio commands |
| `secrets.py` | Loads `config/secrets.yml` (gitignored); supplies per-destination env vars injected into subprocesses |
| `proxy.py` | SSE proxy + session map for SSE-type destinations; dispatches stdio destinations to `bridge.py` |
| `bridge.py` | stdio-to-SSE bridge: subprocess lifecycle, I/O tasks, session registry, shutdown |
| `logger.py` | Newline-delimited JSON log writer; `log_request()` is the single call site for all request logging |

### Security constraints in bridge.py

- Subprocess env uses `_SAFE_ENV_KEYS` allowlist — only `PATH`, `HOME`, `USER`, etc. are inherited from the parent process; secrets come exclusively from `secrets.yml` via `extra_env`.
- `_UUID4_RE` validates `session_id` format before any session lookup.
- Per-destination connection cap: `_MAX_CONNECTIONS_PER_DEST` (default 10, override with `MAX_STDIO_CONNECTIONS` env var).
- Both `stdin_queue` and `out_queue` are bounded (`maxsize=256`).

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
    type: sse
    url: https://mcp.example.com/github
  context7:
    type: stdio
    command: npx -y @upstash/context7-mcp
```

### Test conventions

- `pytest-asyncio` with `mode=strict` — every async test needs `@pytest.mark.asyncio`.
- `reset_bridge_state` is an `autouse` fixture in `test_bridge.py` that clears `_stdio_sessions` and resets `_stdio_lock` between tests.
- Use `httpx.ASGITransport(app=app)` for async HTTP tests; `TestClient` for sync tests.
- Session IDs in tests must match `_UUID4_RE` (`00000000-0000-4000-8000-000000000001` is a valid test UUID).
- Fixture commands passed through `load_config()` are subject to the shell metacharacter check — write a script file to `tmp_path` instead of using Python `-c` one-liners.

### Deployment

Production target is Raspberry Pi OS. `install.sh` creates a `mithril` system user, installs to `/opt/mithril-proxy`, and registers a systemd service (`systemd/mithril-proxy.service`). Config lives in `/etc/mithril-proxy/`, logs in `/var/log/mithril-proxy/proxy.log`.
