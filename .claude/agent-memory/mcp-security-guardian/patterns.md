# MCP Security Patterns — Extended Notes

## SSE Proxy Security

### Line-level SSE validation
The SSE spec (RFC-like) defines exactly five field prefixes: `data:`, `event:`, `id:`,
`retry:`, and `:` (comment). Any line from upstream that does not match one of these MUST
be dropped, not forwarded. Forwarding arbitrary lines enables:
- Injecting secondary `event:` directives to confuse the client SSE parser
- Embedding prompt-injection text inside comment lines (`: SYSTEM: ignore previous...`)
- Injecting `retry:` to manipulate client reconnect timing (DoS vector)

Safe pattern:
```python
ALLOWED_SSE_PREFIXES = ("data:", "event:", "id:", "retry:", ":")
if not any(raw_line.startswith(p) for p in ALLOWED_SSE_PREFIXES) and raw_line != "":
    continue  # drop unknown fields silently
```

### Endpoint URL validation
When an SSE `endpoint` event carries an absolute URL, validate it against the expected
upstream base before storing:
```python
from urllib.parse import urlparse
def _is_same_origin(url: str, base: str) -> bool:
    p, b = urlparse(url), urlparse(base)
    return p.scheme == b.scheme and p.netloc == b.netloc
```
If the URL is not same-origin, reject and close the stream.

## Session Map Security

### Session ID format enforcement
Always validate session IDs extracted from upstream before storing:
```python
_SESSION_ID_PATTERN = re.compile(r'^[A-Za-z0-9_\-]{8,128}$')
if not _SESSION_ID_PATTERN.match(session_id):
    raise ValueError(f"Invalid session ID format: {session_id!r}")
```

### Map size cap
Unbounded in-process session maps are a DoS vector. Add a cap:
```python
MAX_SESSIONS = 1000
if len(_session_map) >= MAX_SESSIONS:
    raise RuntimeError("Session map at capacity")
```

## Header Forwarding

### Request direction — skip list
Minimum skip set going upstream:
`{"host", "content-length", "transfer-encoding", "connection", "keep-alive",
  "x-forwarded-for", "x-real-ip", "x-forwarded-host", "x-forwarded-proto"}`

Adding X-Forwarded-* to the skip list prevents a client from forging their apparent origin.
The proxy should append its own X-Forwarded-For if needed.

### Response direction — skip list
Minimum skip set coming back from upstream to client:
`{"transfer-encoding", "connection", "keep-alive", "set-cookie"}`

Forwarding `set-cookie` from the upstream to the client can allow upstream to set cookies
scoped to the proxy's domain — a session fixation / cross-site cookie injection vector.

## Error Responses

Never include raw exception text in API responses. Use a structured approach:
- Development: include `detail` field gated on `DEBUG=true` env var
- Production: return only a static "Upstream error" message; log the detail server-side

## systemd Hardening Template

Minimum recommended directives for a proxy service:
```ini
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/mithril-proxy
CapabilityBoundingSet=
AmbientCapabilities=
```

## install.sh Principle of Least Privilege

The service user should NOT own its own virtualenv or source tree. Recommended layout:
- Source + venv: owned by `root:mithril`, mode `750` (root writes, mithril reads/executes)
- Log dir: owned by `mithril:mithril`, mode `700`
- Config dir: owned by `root:mithril`, mode `750`; individual files mode `640`

This prevents a compromised `mithril` process from modifying its own Python packages
as a persistence mechanism.
