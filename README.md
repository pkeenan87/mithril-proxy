# Mithril Proxy

              * . * . *
          .  ___________  .
        *  /      *      \  *
       .  / ╔═══════════╗ \  .
       * /  ║ ╔═══════╗ ║  \ *
       .|   ║ ║       ║ ║   |.
       *|   ║ ║MITHRIL║ ║   |*
       .|   ║ ║       ║ ║   |.
       * \  ║ ╚═══════╝ ║  / *
       .  \ ╚═══════════╝ /  .
        *  \      *      /  *
            \     *     /
             \    *    /
              \   *   /
               \  *  /
                \ * /
                 \ /
                  V

   "light and yet harder than tempered steel"

A minimal Python MCP proxy server. Sits between MCP clients (Claude Desktop, Cursor, etc.) and remote or local MCP servers, routing requests by URL path prefix and forwarding Bearer tokens unchanged.

## Quick Start

```bash
# Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Add at least one destination
cat > config/destinations.yml <<'EOF'
destinations:
  my-server:
    type: streamable_http
    url: https://mcp.example.com/mcp
EOF

# Run
PYTHONPATH=src uvicorn mithril_proxy.main:app --host 0.0.0.0 --port 3000
```

Clients connect to `http://localhost:3000/my-server/mcp`.

## Features

- **Streamable HTTP proxy** — forwards MCP Streamable HTTP transport (POST/GET/DELETE `/mcp`) to remote upstreams; streams SSE responses transparently
- **SSE proxy** — legacy MCP-over-SSE support for older upstreams; rewrites `endpoint` events so clients post messages back through the proxy
- **stdio bridge** — spawns a local subprocess (e.g. `npx` MCP servers) per destination, shared across all sessions; bridges stdin/stdout to the MCP Streamable HTTP transport with automatic restart on exit
- **Pass-through auth** — Bearer token forwarded to upstream unchanged; no proxy-level credential store
- **Audit logging** — full JSON-RPC request/response bodies, `rpc_id`, and `mcp_method` in every log line; 32 KB truncation; toggle with `AUDIT_LOG_BODIES=false`
- **Structured JSON logs** — one line per request, written to a configurable file
- **Health check** — `GET /health` → `{"status": "ok"}`

## Deployment

See [SETUP.md](SETUP.md) for full Raspberry Pi / systemd installation instructions.

## Running Tests

```bash
pip install pytest pytest-asyncio httpx
PYTHONPATH=src pytest tests/ -v
```

## Project Structure

```
src/mithril_proxy/
  main.py     FastAPI app + lifespan + route registration
  proxy.py    SSE proxy + Streamable HTTP forwarding + session management
  bridge.py   stdio-to-Streamable-HTTP bridge + per-destination subprocess lifecycle
  config.py   YAML config loader + validation
  secrets.py  Per-destination env vars from secrets.yml
  logger.py   JSON log formatter + writer (audit logging)
  utils.py    Shared request helpers (source_ip)
config/
  destinations.yml
  secrets.yml        (gitignored)
systemd/
  mithril-proxy.service
tests/
install.sh
SETUP.md
```
