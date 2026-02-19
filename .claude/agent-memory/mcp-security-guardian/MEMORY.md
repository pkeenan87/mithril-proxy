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

## Patterns To Reuse

- Always validate session IDs against a strict regex `^[A-Za-z0-9_-]{8,128}$` before
  storing in the session map.
- Use an allowlist of upstream base URLs when resolving absolute URLs from upstream data.
- Strip `cookie`, `set-cookie`, `x-forwarded-for`, `x-real-ip` from upstream headers before
  forwarding to prevent header injection and session fixation.
- For SSE proxying: validate each line prefix against the SSE spec set
  `{event:, data:, id:, retry:, :}` and discard or escape anything else.
- Always add `detail` field only in development mode; gate on an env flag.

See `patterns.md` for extended notes.
