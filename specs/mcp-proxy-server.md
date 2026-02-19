# Spec for MCP Proxy Server

branch: claude/feature/mcp-proxy-server

## Summary

A minimal Python MCP proxy server that receives MCP requests, authenticates them via Bearer token, resolves the caller's identity and destination API credentials, forwards the request to the target MCP server, and logs all activity to a JSON log file. Designed to run as a systemd service, initially on a Raspberry Pi, with a path toward multi-instance deployment behind a load balancer.

## Functional requirements

- Accept incoming MCP requests over HTTP (or stdio, TBD)
- Authenticate each request using a Bearer token from the `Authorization` header
- Resolve the Bearer token to a user identity via a local lookup (e.g. config file or SQLite database)
- Retrieve the appropriate destination MCP server URL and API token for the authenticated user and requested destination
- Forward the request transparently to the target MCP server, injecting the resolved API token
- Return the target server's response to the caller without modification
- Log each request/response cycle to a newline-delimited JSON log file on the filesystem, including:
  - Timestamp (ISO 8601)
  - Authenticated user identity
  - Source IP address
  - Requested MCP destination/server name
  - MCP method or prompt content (sanitized if needed)
  - HTTP status code of the forwarded response
  - Latency (ms)
  - Any error or exception details
- Run as a systemd service with a provided unit file
- Provide an install script and setup guide to bootstrap the service on a fresh Raspberry Pi OS install

## Possible Edge Cases

- Bearer token is missing, malformed, or not found in the lookup store
- Destination MCP server name is unknown or not configured for the user
- Target MCP server is unreachable or returns an error
- Log file path does not exist or is not writable on startup
- Concurrent requests from the same or different users
- Token store is updated while the service is running (hot reload vs. restart)
- Request body is very large (streaming vs. buffered forwarding)
- Clock skew affecting log timestamps on the Raspberry Pi

## Acceptance Criteria

- A valid Bearer token resolves to a user and their destination API token, and the request is forwarded successfully
- An invalid or missing Bearer token returns HTTP 401 with a clear error message and is logged
- Each request produces exactly one structured JSON log entry in the configured log file
- The log file persists across service restarts (append mode)
- The systemd service starts automatically on boot and restarts on failure
- The install script sets up a Python virtualenv, installs dependencies, copies the unit file, and enables the service
- A setup guide walks through all manual steps needed for a fresh Raspberry Pi OS environment

## Open Questions

- Should the Bearer token lookup use a flat config file (e.g. TOML/JSON), SQLite, or a future-compatible store (e.g. Redis-ready)? to start lets use a yml file. Review my answer to question 3 to see if this is still needed though.
- Is the MCP transport HTTP/SSE or stdio? (affects how the proxy forwards requests) sse, I want this to run on a remote proxy and have all of my developers connect to it. This will not be a local proxy on the developers machine.
- Should the proxy strip or pass through the original `Authorization` header to the destination server? If it can pass it through that would be best, then I would not have to worry about maintaining my own bearer tokens and a lookup file of each users api keys matched to their mcp servers.
- What is the log rotation strategy? (logrotate, Python's RotatingFileHandler, or external). dont worry about log rotation for now, this is just for testing and I will eventually migrate to sending logs to an azure event hub
- Should the service bind to localhost only (behind nginx/HAProxy) or directly on a port? directly on a port for now, preferably port 3000
- What is the expected scale for the Raspberry Pi phase (requests/minute)? the testing scale is very low for now, just me testing. But the application should be built to handle 10-15 requests per minute to account for peak times in the future
- Should failed destination requests be retried, and if so how many times? three times.

## Testing Guidelines

Create test file(s) in the `./tests` folder for the new feature, and create meaningful tests for the following cases, without going too heavy:

- Valid Bearer token authenticates and request is forwarded to the correct destination
- Invalid Bearer token returns 401 and is not forwarded
- Unknown destination for a valid user returns an appropriate error
- JSON log entry is written for each request with all required fields present
- Service handles an unreachable destination gracefully (timeout, error logged)
- Concurrent requests do not corrupt the log file or mix up user credentials

## Additional Guidance

- write this in python