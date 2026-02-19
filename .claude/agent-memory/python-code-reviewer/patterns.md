# Patterns and Fix Snippets

## Retry Loop Fix (proxy.py _connect_with_retries)

### Current (buggy) code
```python
_RETRY_DELAYS = [0.5, 1.0, 2.0]

for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
    try:
        response = await client.request(method, url, **kwargs)
        if response.status_code >= 500:
            if attempt < len(_RETRY_DELAYS):   # Bug 1: silently returns 500 on final attempt
                await asyncio.sleep(delay)
                continue
        return response
    except (...) as exc:
        last_exc = exc
        if attempt <= len(_RETRY_DELAYS):      # Bug 2: always True, wastes sleep after last attempt
            await asyncio.sleep(delay)
raise last_exc
```

### Corrected pattern
```python
_MAX_RETRIES = 3
_RETRY_DELAYS = [0.5, 1.0, 2.0]  # len must equal _MAX_RETRIES - 1 (no sleep after last)

for attempt in range(_MAX_RETRIES):
    try:
        response = await client.request(method, url, **kwargs)
        if response.status_code < 500:
            return response
        last_exc = RuntimeError(f"Upstream returned {response.status_code}")
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
        last_exc = exc

    if attempt < _MAX_RETRIES - 1:
        await asyncio.sleep(_RETRY_DELAYS[attempt])

raise last_exc
```
Key points:
- `range(_MAX_RETRIES)` gives 0-indexed attempts so `attempt < _MAX_RETRIES - 1` is a
  clean "not the last attempt" guard without len() confusion.
- 500 responses now raise instead of returning, so callers get a consistent exception.
- Sleep only happens when another attempt will follow.
