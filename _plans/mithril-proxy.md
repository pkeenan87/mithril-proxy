│ Plan to implement                                                                │
│                                                                                  │
│ Plan: MCP Proxy Server (mithril-proxy)                                           │
│                                                                                  │
│ Context                                                                          │
│                                                                                  │
│ A minimal Python MCP proxy server to run on a Raspberry Pi as a systemd service. │
│  It sits between MCP clients (Claude Desktop, Cursor, etc.) and remote MCP       │
│ destination servers. It proxies MCP-over-SSE traffic, passes the client's Bearer │
│  token through unchanged to the destination, logs all activity as                │
│ newline-delimited JSON, and routes to destinations by URL path prefix. No        │
│ proxy-level auth gate — the destination server validates credentials. Designed   │
│ for low volume (10–15 req/min peak) with a path toward multi-instance            │
│ load-balanced deployment later.                                                  │
│                                                                                  │
│ ---                                                                              │
│ Key Design Decisions                                                             │
│                                                                                  │
│ - Transport: HTTP + SSE (MCP SSE protocol)                                       │
│ - Routing: GET /{destination}/sse and POST /{destination}/message                │
│ - Auth: Bearer token passed through unchanged; no proxy-level auth               │
│ - Config: config/destinations.yml (name → URL map)                               │
│ - Logging: Newline-delimited JSON, append to file, no rotation                   │
│ - Retries: 3 attempts on destination connection failure (exponential backoff)    │
│ - Port: 3000                                                                     │
│ - Runtime: Python 3.11+, FastAPI, uvicorn, httpx                                 │
│                                                                                  │
│ ---                                                                              │
│ File Structure                                                                   │
│                                                                                  │
│ mithril-proxy/                                                                   │
│ ├── src/                                                                         │
│ │   └── mithril_proxy/                                                           │
│ │       ├── __init__.py                                                          │
│ │       ├── main.py          # FastAPI app + route registration                  │
│ │       ├── proxy.py         # SSE forwarding + session management               │
│ │       ├── config.py        # YAML config loader + validation                   │
│ │       └── logger.py        # JSON log formatter + writer                       │
│ ├── tests/                                                                       │
│ │   ├── test_auth.py         # 401 on bad/missing token                          │
│ │   ├── test_proxy.py        # forwarding, retries, unknown destination          │
│ │   └── test_logging.py      # log entry fields + concurrency                    │
│ ├── config/                                                                      │
│ │   └── destinations.yml     # destination name → upstream URL                   │
│ ├── systemd/                                                                     │
│ │   └── mithril-proxy.service                                                    │
│ ├── install.sh                                                                   │
│ ├── SETUP.md                 # setup guide + client config examples              │
│ ├── requirements.txt                                                             │
│ └── README.md                                                                    │
│                                                                                  │
│ ---                                                                              │
│ Implementation Steps                                                             │
│                                                                                  │
│ 1. Project scaffold                                                              │
│                                                                                  │
│ - Create the directory structure above                                           │
│ - requirements.txt: fastapi, uvicorn[standard], httpx[http2], pyyaml,            │
│ python-dotenv                                                                    │
│ - config/destinations.yml with a commented example entry                         │
│                                                                                  │
│ 2. config.py — Destination config loader                                         │
│                                                                                  │
│ - Load and parse destinations.yml on startup                                     │
│ - Validate: each entry must have a non-empty string URL                          │
│ - Expose a get_destination_url(name: str) -> str | None function                 │
│ - Raise a clear startup error if the file is missing or malformed                │
│                                                                                  │
│ 3. logger.py — JSON structured logger                                            │
│                                                                                  │
│ - Custom logging.Formatter that serializes to a single JSON line                 │
│ - Fields logged per request:                                                     │
│   - timestamp (ISO 8601 UTC)                                                     │
│   - user (first 8 chars of Bearer token, for correlation; "anonymous" if         │
│ missing)                                                                         │
│   - source_ip                                                                    │
│   - destination (name from URL path)                                             │
│   - mcp_method (JSON-RPC method field from POST body, if available)              │
│   - status_code (upstream response status)                                       │
│   - latency_ms                                                                   │
│   - error (exception message, if any)                                            │
│ - File handler in append mode; path configurable via env var LOG_FILE (default:  │
│ /var/log/mithril-proxy/proxy.log)                                                │
│ - Log directory is created on startup if it doesn't exist                        │
│                                                                                  │
│ 4. proxy.py — SSE proxy + session management                                     │
│                                                                                  │
│ SSE connection flow:                                                             │
│ 1. GET /{destination}/sse → open long-lived httpx async SSE stream to            │
│ {upstream_url}/sse, passing the Authorization header and all other original      │
│ headers (minus Host)                                                             │
│ 2. Intercept the event: endpoint SSE message from upstream (contains the         │
│ upstream's message endpoint URL, e.g. /messages?sessionId=abc)                   │
│ 3. Extract the sessionId query param; store a session map: session_id →          │
│ upstream_message_url                                                             │
│ 4. Rewrite the event data URL to point back to the proxy:                        │
│ /{destination}/message?session_id={session_id}                                   │
│ 5. Stream all other SSE events from upstream to the client unchanged             │
│ 6. On disconnect (client or upstream), clean up session map entry                │
│                                                                                  │
│ Message forwarding:                                                              │
│ 1. POST /{destination}/message?session_id={id} → look up upstream message URL    │
│ from session map                                                                 │
│ 2. If session not found → 404                                                    │
│ 3. Parse JSON body to extract method field for logging                           │
│ 4. Forward POST to upstream URL with original headers (including Authorization)  │
│ 5. Return upstream response body + status code to client                         │
│                                                                                  │
│ Retries:                                                                         │
│ - Wrap upstream connection attempts in a retry loop: 3 attempts, exponential     │
│ backoff (0.5s, 1s, 2s)                                                           │
│ - Only retry on connection errors / 5xx; never retry on 4xx                      │
│                                                                                  │
│ 5. main.py — FastAPI app                                                         │
│                                                                                  │
│ - Load config on startup (fail fast if invalid)                                  │
│ - Register routes:                                                               │
│   - GET /{destination}/sse                                                       │
│   - POST /{destination}/message                                                  │
│ - Unknown destination name → JSON 404 response (logged)                          │
│ - Health check: GET /health → {"status": "ok"} (not logged)                      │
│ - Bind on 0.0.0.0:3000                                                           │
│                                                                                  │
│ 6. systemd/mithril-proxy.service                                                 │
│                                                                                  │
│ [Unit]                                                                           │
│ Description=Mithril MCP Proxy                                                    │
│ After=network.target                                                             │
│                                                                                  │
│ [Service]                                                                        │
│ Type=simple                                                                      │
│ User=mithril                                                                     │
│ WorkingDirectory=/opt/mithril-proxy                                              │
│ ExecStart=/opt/mithril-proxy/venv/bin/uvicorn mithril_proxy.main:app --host      │
│ 0.0.0.0 --port 3000                                                              │
│ Restart=on-failure                                                               │
│ RestartSec=5                                                                     │
│ EnvironmentFile=/etc/mithril-proxy/env                                           │
│                                                                                  │
│ [Install]                                                                        │
│ WantedBy=multi-user.target                                                       │
│ - EnvironmentFile holds LOG_FILE path and any future config                      │
│                                                                                  │
│ 7. install.sh                                                                    │
│                                                                                  │
│ Steps:                                                                           │
│ 1. Create system user mithril (no login shell)                                   │
│ 2. Create /opt/mithril-proxy, copy project files                                 │
│ 3. Create Python venv, install requirements.txt                                  │
│ 4. Create /var/log/mithril-proxy/ owned by mithril                               │
│ 5. Create /etc/mithril-proxy/env with defaults                                   │
│ 6. Copy destinations.yml to /etc/mithril-proxy/destinations.yml                  │
│ 7. Copy and enable systemd unit file (systemctl enable --now)                    │
│                                                                                  │
│ 8. SETUP.md — Setup guide                                                        │
│                                                                                  │
│ Sections:                                                                        │
│ - Prerequisites (Raspberry Pi OS, Python 3.11+)                                  │
│ - Clone and run install.sh                                                       │
│ - Edit /etc/mithril-proxy/destinations.yml with your destinations                │
│ - Start / restart the service                                                    │
│ - Client configuration examples (Claude Desktop, Cursor)                         │
│   - Show the claude_desktop_config.json snippet with url and Authorization       │
│ header                                                                           │
│   - Explain: client token goes in client config; proxy never stores it           │
│ - Viewing logs: tail -f /var/log/mithril-proxy/proxy.log | jq                    │
│ - Adding a new destination (one line in destinations.yml + restart)              │
│ - Troubleshooting common issues                                                  │
│                                                                                  │
│ 9. Tests (tests/)                                                                │
│                                                                                  │
│ - test_auth.py: missing token → logs "anonymous", request still forwarded (no    │
│ proxy gate)                                                                      │
│ - test_proxy.py:                                                                 │
│   - Valid destination → forwarded with correct upstream URL + auth header passed │
│  through                                                                         │
│   - Unknown destination → 404 JSON response                                      │
│   - Upstream unreachable → retried 3 times, 502 returned, error logged           │
│   - Session ID rewriting in endpoint event is correct                            │
│ - test_logging.py:                                                               │
│   - Each request writes exactly one JSON log line with all required fields       │
│   - Concurrent requests do not interleave log lines (file handler thread safety) │
│                                                                                  │
│ ---                                                                              │
│ Verification                                                                     │
│                                                                                  │
│ 1. Run uvicorn mithril_proxy.main:app --port 3000 locally                        │
│ 2. Configure a real MCP client to point to                                       │
│ http://localhost:3000/{destination}/sse                                          │
│ 3. Confirm requests are forwarded and responses flow back                        │
│ 4. Check proxy.log contains well-formed JSON entries                             │
│ 5. Kill the upstream → confirm 3 retries then 502, with error field in log       │
│ 6. Run pytest tests/                                                             │
│ 7. On Pi: run install.sh, check systemctl status mithril-proxy, reboot and       │
│ confirm auto-start   