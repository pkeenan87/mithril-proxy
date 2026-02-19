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

A minimal Python MCP-over-SSE proxy server. Sits between MCP clients (Claude Desktop, Cursor, etc.) and remote MCP destination servers, routing requests by URL path prefix and forwarding Bearer tokens unchanged.

## Quick Start

```bash
# Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Add at least one destination
cat > config/destinations.yml <<'EOF'
destinations:
  my-server:
    url: https://mcp.example.com
EOF

# Run
uvicorn mithril_proxy.main:app --host 0.0.0.0 --port 3000
```

Clients connect to `http://localhost:3000/my-server/sse`.

## Features

- **SSE proxy** — streams MCP-over-SSE events between clients and upstreams
- **Session routing** — rewrites upstream `endpoint` events so clients post messages back through the proxy
- **Pass-through auth** — Bearer token forwarded to upstream unchanged; no proxy-level credential store
- **Structured JSON logs** — one line per request, written to a configurable file
- **Retries** — 3 attempts with exponential backoff on upstream connection failures
- **Health check** — `GET /health` → `{"status": "ok"}`

## Deployment

See [SETUP.md](SETUP.md) for full Raspberry Pi / systemd installation instructions.

## Running Tests

```bash
pip install pytest pytest-asyncio httpx
pytest tests/ -v
```

## Project Structure

```
src/mithril_proxy/
  main.py     FastAPI app + route registration
  proxy.py    SSE forwarding + session management
  config.py   YAML config loader + validation
  logger.py   JSON log formatter + writer
config/
  destinations.yml
systemd/
  mithril-proxy.service
tests/
install.sh
SETUP.md
```
