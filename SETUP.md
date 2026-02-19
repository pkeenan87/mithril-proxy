# Mithril Proxy — Setup Guide

A minimal MCP-over-SSE proxy that sits between your MCP clients (Claude Desktop, Cursor, etc.) and remote MCP servers. It routes requests by URL path, passes Bearer tokens straight through to the upstream, and writes structured JSON logs.

---

## Prerequisites

- Raspberry Pi OS (Bookworm or later recommended)
- Python 3.11 or newer (`python3 --version`)
- `git` and `rsync` installed
- Root access (or `sudo`)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip rsync git
```

If you plan to use **stdio MCP servers** that are distributed via npm (e.g. context7, GitHub MCP), Node.js is also required:

```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
node --version && npx --version
```

---

## Installation

```bash
git clone https://github.com/YOUR_ORG/mithril-proxy.git
cd mithril-proxy
sudo bash install.sh
```

The script:
1. Creates a `mithril` system user
2. Copies the project to `/opt/mithril-proxy`
3. Creates a Python venv and installs dependencies
4. Creates `/var/log/mithril-proxy/` owned by `mithril`
5. Writes default config to `/etc/mithril-proxy/env`
6. Copies `destinations.yml` to `/etc/mithril-proxy/destinations.yml`
7. Installs and starts the `mithril-proxy` systemd service

---

## Configure Destinations

Edit `/etc/mithril-proxy/destinations.yml`. There are two destination types.

### SSE destinations (remote HTTP server)

The proxy forwards SSE traffic to an upstream HTTP server:

```yaml
destinations:
  github:
    type: sse          # optional — 'sse' is the default
    url: https://mcp.example.com/github
```

### stdio destinations (local process)

The proxy spawns a local subprocess and bridges its stdin/stdout as SSE. Each client connection gets its own subprocess instance.

```yaml
destinations:
  context7:
    type: stdio
    command: npx -y @upstash/context7-mcp
```

> **First connection note:** `npx` downloads the package on first run, which can take 15–30 seconds. Subsequent connections use the npm cache and start immediately.

Each destination key becomes the URL path segment clients connect to:
`GET http://<pi-ip>:3000/context7/sse`

After editing:

```bash
sudo systemctl restart mithril-proxy
```

---

## API Keys for stdio destinations

API keys and other secrets for stdio subprocesses go in `/etc/mithril-proxy/secrets.yml`. This file is separate from `destinations.yml` so secrets stay out of version control.

Create or edit `/etc/mithril-proxy/secrets.yml`:

```yaml
context7:
  CONTEXT7_API_KEY: your-api-key-here
```

Each top-level key matches a destination name. The values are injected as environment variables into that destination's subprocess — they are never written to logs or forwarded to clients.

Make the file readable only by the `mithril` user:

```bash
sudo chmod 600 /etc/mithril-proxy/secrets.yml
sudo chown mithril:mithril /etc/mithril-proxy/secrets.yml
```

Then tell the proxy where to find it by adding to `/etc/mithril-proxy/env`:

```
SECRETS_CONFIG=/etc/mithril-proxy/secrets.yml
```

Restart to apply:

```bash
sudo systemctl restart mithril-proxy
```

---

## Service Management

```bash
# Check status
sudo systemctl status mithril-proxy

# Start / stop / restart
sudo systemctl start mithril-proxy
sudo systemctl stop mithril-proxy
sudo systemctl restart mithril-proxy

# Follow service logs (systemd journal)
sudo journalctl -u mithril-proxy -f

# Follow JSON request logs
sudo tail -f /var/log/mithril-proxy/proxy.log | jq
```

---

## Client Configuration

Replace `192.168.1.10` with your Raspberry Pi's IP address in all examples below.

For **SSE destinations**, the proxy forwards the client's `Authorization: Bearer <token>` header to the upstream unchanged. The token is never stored by the proxy.

For **stdio destinations**, the API key is injected by the proxy from `secrets.yml` — no `Authorization` header is needed in the client config.

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "github": {
      "url": "http://192.168.1.10:3000/github/sse",
      "headers": {
        "Authorization": "Bearer YOUR_UPSTREAM_TOKEN_HERE"
      }
    },
    "context7": {
      "type": "sse",
      "url": "http://192.168.1.10:3000/context7/sse"
    }
  }
}
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "github": {
      "url": "http://192.168.1.10:3000/github/sse",
      "headers": {
        "Authorization": "Bearer YOUR_UPSTREAM_TOKEN_HERE"
      }
    },
    "context7": {
      "url": "http://192.168.1.10:3000/context7/sse"
    }
  }
}
```

---

## Adding a New Destination

**SSE destination:**
1. Add the entry to `/etc/mithril-proxy/destinations.yml`
2. Restart: `sudo systemctl restart mithril-proxy`
3. Point your client to `http://<pi-ip>:3000/<name>/sse` with an `Authorization` header

**stdio destination (e.g. an npm MCP server):**
1. Add the entry to `/etc/mithril-proxy/destinations.yml` with `type: stdio` and `command:`
2. If the server requires an API key, add it to `/etc/mithril-proxy/secrets.yml`
3. Restart: `sudo systemctl restart mithril-proxy`
4. Point your client to `http://<pi-ip>:3000/<name>/sse` — no `Authorization` header needed

---

## Viewing Logs

Request logs are written to `/var/log/mithril-proxy/proxy.log` as newline-delimited JSON.

```bash
# Pretty-print recent entries
sudo tail -20 /var/log/mithril-proxy/proxy.log | jq

# Filter by destination
sudo tail -f /var/log/mithril-proxy/proxy.log | jq 'select(.destination == "github")'

# Filter errors only
sudo tail -f /var/log/mithril-proxy/proxy.log | jq 'select(.error != null)'
```

Each log line contains:

| Field | Description |
|---|---|
| `timestamp` | ISO 8601 UTC |
| `user` | First 8 chars of Bearer token (`anonymous` if missing) |
| `source_ip` | Client IP address |
| `destination` | Destination name from the URL path |
| `mcp_method` | JSON-RPC method from POST body (null for SSE connections) |
| `status_code` | Upstream HTTP status code |
| `latency_ms` | Round-trip latency in milliseconds |
| `error` | Exception message (only present on errors) |
| `rpc_id` | JSON-RPC `id` field from the request or response (omitted when not present) |
| `request_body` | Full JSON-RPC request payload as a string (omitted when `AUDIT_LOG_BODIES=false`) |
| `response_body` | Full upstream response payload as a string (omitted when `AUDIT_LOG_BODIES=false`) |
| `truncated` | `true` when a body field was cut at the 32 KB limit (omitted otherwise) |

> **Security note:** Enabling audit body logging (`AUDIT_LOG_BODIES=true`, which is the default) persists full request and response payloads to disk. These may include sensitive tool arguments, API responses, or user data. Restrict log file permissions accordingly and rotate logs regularly.

---

## Troubleshooting

**Service fails to start — missing destinations config**
```
FileNotFoundError: Destinations config not found: /etc/mithril-proxy/destinations.yml
```
Create the file and add at least one destination, then restart.

**404 for a destination**
The destination name in the URL path doesn't match any key in `destinations.yml`. Check spelling and restart after editing.

**502 from the proxy**
The upstream MCP server is unreachable. The proxy retries 3 times with exponential backoff before returning 502. Check the upstream server and your network connectivity.

**Log file not being created**
Ensure `/var/log/mithril-proxy/` exists and is owned by the `mithril` user:
```bash
sudo mkdir -p /var/log/mithril-proxy
sudo chown mithril:mithril /var/log/mithril-proxy
```

**stdio destination fails to start — executable not found**
```
ValueError: stdio destination 'context7': command executable 'npx' not found on PATH
```
The proxy validates all stdio commands at startup. Install Node.js (see Prerequisites) and confirm `which npx` succeeds as the `mithril` user.

**stdio subprocess exits immediately / reconnects loop**
The subprocess crashed before producing any output. Check the proxy log for `subprocess stderr` entries:
```bash
sudo tail -f /var/log/mithril-proxy/proxy.log | jq 'select(.stderr_line != null)'
```
Common causes: missing API key in `secrets.yml`, or the npm package needs updating (`npx -y` will re-download on next connection).

**Too many connections error (503)**
```json
{"error": "Too many active connections for 'context7' (max 10)"}
```
Each SSE client connection spawns its own subprocess. The default cap is 10 per destination. Override with `MAX_STDIO_CONNECTIONS=<n>` in `/etc/mithril-proxy/env`.

**Disabling audit body logging**
By default the proxy writes the full request and response JSON-RPC payloads to the log file. To disable this (e.g. for privacy or disk-space reasons), set `AUDIT_LOG_BODIES=false` in `/etc/mithril-proxy/env`:
```
AUDIT_LOG_BODIES=false
```
With this flag set, `request_body` and `response_body` fields are omitted from every log line. The `rpc_id`, `mcp_method`, and all other fields are still logged.

**Port 3000 already in use**
Edit the `ExecStart` line in `/etc/systemd/system/mithril-proxy.service` to use a different port, then:
```bash
sudo systemctl daemon-reload && sudo systemctl restart mithril-proxy
```

---

## Auto-Start on Boot

The installer enables the service with `systemctl enable`. Verify:

```bash
sudo systemctl is-enabled mithril-proxy
# → enabled
```

Reboot and confirm: `sudo systemctl status mithril-proxy`
