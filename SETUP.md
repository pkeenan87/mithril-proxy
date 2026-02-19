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

Edit `/etc/mithril-proxy/destinations.yml`:

```yaml
destinations:
  github:
    url: https://mcp.example.com/github

  my-local-server:
    url: http://192.168.1.50:8080
```

Each key becomes the URL path segment your clients connect to:
`GET http://<pi-ip>:3000/github/sse`

After editing:

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

The proxy passes the client's `Authorization: Bearer <token>` header through to the upstream MCP server unchanged. The client's token is **never stored** by the proxy — it is forwarded as-is.

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "github": {
      "url": "http://192.168.1.10:3000/github/sse",
      "headers": {
        "Authorization": "Bearer YOUR_UPSTREAM_TOKEN_HERE"
      }
    }
  }
}
```

Replace `192.168.1.10` with your Raspberry Pi's IP address, and `github` with the destination name you configured in `destinations.yml`.

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "github": {
      "url": "http://192.168.1.10:3000/github/sse",
      "headers": {
        "Authorization": "Bearer YOUR_UPSTREAM_TOKEN_HERE"
      }
    }
  }
}
```

---

## Adding a New Destination

1. Edit `/etc/mithril-proxy/destinations.yml` and add the new entry
2. Restart the service: `sudo systemctl restart mithril-proxy`
3. Update your MCP client config to point to `http://<pi-ip>:3000/<new-name>/sse`

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
