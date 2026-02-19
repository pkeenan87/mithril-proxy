# Spec for stdio-to-sse-bridge

branch: claude/feature/stdio-to-sse-bridge

## Summary

The proxy currently only handles MCP-over-SSE transports. Many popular MCP servers (e.g. context7, GitHub, Playwright) are distributed as stdio processes launched via `npx` or a local binary. This feature adds a stdio-to-SSE bridge so that any stdio-based MCP server can be registered as a destination and used through the proxy exactly like a native SSE upstream.

The bridge spawns the stdio process on demand, wraps it with an SSE interface, and exposes it on a local port. The proxy then treats that local SSE endpoint as a normal destination.

## Functional requirements

- Add a new optional `type` field to each destination entry in `destinations.yml`. Accepted values: `sse` (default, existing behaviour) and `stdio`.
- For `stdio` destinations, add a `command` field (required) containing the shell command used to launch the MCP server process (e.g. `npx -y @upstash/context7-mcp`).
- On proxy startup, for each `stdio` destination: spawn the command as a subprocess and expose its stdin/stdout as a local MCP-over-SSE endpoint on a dynamically assigned localhost port.
- Register the spawned local SSE endpoint as the upstream URL for that destination, so all existing proxy routing logic works without changes.
- Relay JSON-RPC messages from the SSE client to the subprocess stdin and relay stdout responses back as SSE data events.
- If the subprocess exits unexpectedly, attempt to restart it up to 3 times with exponential backoff before marking the destination as unavailable and returning 503 to clients.
- On proxy shutdown, terminate all managed subprocess children gracefully (SIGTERM, then SIGKILL after 5 seconds).
- Log subprocess start, stop, restart attempts, and crashes as structured JSON entries using the existing logger.

## Possible Edge Cases

- Subprocess takes too long to start and the first SSE client connects before it is ready — proxy should queue or retry the connection briefly.
- Subprocess writes to stderr — should be captured and logged at WARNING level, not forwarded to the SSE client.
- Two concurrent SSE clients connect to the same `stdio` destination — determine whether each gets its own subprocess instance or they share one (likely one per destination for simplicity; document the decision).
- Subprocess command is missing or not executable on the host OS — fail fast at startup with a clear error message.
- `npx` requires network access on first run to download a package — timeout handling needed.
- The bridge local port conflicts with another process — retry with a different port.
- Long-running stdio servers that produce no output (idle) — ensure the subprocess is not killed by any internal timeout.

## Acceptance Criteria

- A `stdio` destination declared in `destinations.yml` with a valid `command` is spawned on startup and reachable at `GET /{destination}/sse`.
- A native SSE destination declared with `type: sse` (or no `type`) continues to work exactly as before.
- An MCP client (e.g. Claude Code) can connect to a stdio-backed destination through the proxy and successfully call tools exposed by the subprocess.
- Subprocess crashes trigger restart attempts; after 3 failures the destination returns 503 with an error logged.
- Proxy shutdown terminates all spawned subprocesses within 5 seconds.
- Subprocess stderr is written to the structured log file and not forwarded to clients.
- All existing tests continue to pass after this change.

## Open Questions

- Should each incoming SSE connection get its own subprocess instance, or should a single subprocess be shared across all clients connecting to the same destination? (One shared instance is simpler but may not support concurrent sessions depending on the MCP server implementation.) they should get their own subprocess.
- Should the bridge local SSE port be configurable, or always dynamically assigned? configurable.
- Should stdio destinations be started eagerly at proxy startup, or lazily on first client connection? eagerly.
- Is there a need to pass environment variables (e.g. API keys) to the subprocess, and if so, how should they be configured in `destinations.yml` without committing secrets? Is it possible to take it from the user request? If not, put the secrets in a yml file for now. this will suffice for testing. Long term we can migrate to an azure key vault.

## Testing Guidelines

Create test files in the `./tests` folder for the new feature. Focus on the following cases without going too heavy:

- A configured `stdio` destination spawns a subprocess on startup and the bridge is reachable.
- Messages sent to `POST /{destination}/message` are forwarded to subprocess stdin and responses are returned via SSE.
- If the subprocess exits, the bridge attempts to restart it up to 3 times.
- After 3 failed restart attempts the destination returns 503.
- A `sse`-type destination (or one with no `type`) continues to proxy normally alongside stdio destinations.
- Subprocess stderr output is written to the log and not included in the SSE stream.
