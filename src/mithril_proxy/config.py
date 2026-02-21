"""Destination config loader — reads config/destinations.yml on startup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml

_CONFIG_PATH_ENV = "DESTINATIONS_CONFIG"
_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "destinations.yml"

_destinations: dict[str, DestinationConfig] = {}


@dataclass
class DestinationConfig:
    type: str = "sse"
    url: Optional[str] = None
    command: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)


def _resolve_config_path() -> Path:
    env_val = os.environ.get(_CONFIG_PATH_ENV)
    if env_val:
        return Path(env_val)
    return _DEFAULT_CONFIG_PATH


def load_config(path: Optional[Path] = None) -> None:
    """Load and validate destinations.yml.  Call once at application startup."""
    global _destinations

    config_path = path or _resolve_config_path()

    if not config_path.exists():
        raise FileNotFoundError(
            f"Destinations config not found: {config_path}. "
            "Create config/destinations.yml or set the DESTINATIONS_CONFIG env var."
        )

    with config_path.open() as fh:
        try:
            raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse {config_path}: {exc}") from exc

    if raw is None:
        # Empty file is valid — no destinations configured yet
        _destinations = {}
        return

    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must be a YAML mapping at the top level.")

    # Support both flat `name: url` and nested `destinations: {name: {url: ...}}`
    destinations_raw = raw.get("destinations", raw)

    if not isinstance(destinations_raw, dict):
        raise ValueError(
            f"{config_path}: 'destinations' must be a mapping of name → {{url: ...}}."
        )

    parsed: dict[str, DestinationConfig] = {}
    for name, entry in destinations_raw.items():
        if isinstance(entry, str):
            # Flat string URL — treat as SSE destination
            url = entry.strip()
            if not url:
                raise ValueError(
                    f"{config_path}: destination '{name}' has an empty URL."
                )
            parsed[name] = DestinationConfig(type="sse", url=url.rstrip("/"))

        elif isinstance(entry, dict):
            dest_type = entry.get("type", "sse")

            if dest_type not in ("sse", "stdio", "streamable_http"):
                raise ValueError(
                    f"{config_path}: destination '{name}' has unknown type '{dest_type}'. "
                    "Accepted values: 'sse', 'stdio', 'streamable_http'."
                )

            env_block = entry.get("env", {})
            if not isinstance(env_block, dict):
                raise ValueError(
                    f"{config_path}: destination '{name}' env must be a mapping."
                )
            # Coerce YAML-parsed values (ints, bools, etc.) to strings
            env_dict = {k: str(v) for k, v in env_block.items()}

            if dest_type == "sse":
                url = entry.get("url", "")
                if not isinstance(url, str) or not url.strip():
                    raise ValueError(
                        f"{config_path}: destination '{name}' (type: sse) requires a non-empty 'url'."
                    )
                parsed[name] = DestinationConfig(
                    type="sse",
                    url=url.strip().rstrip("/"),
                    env=env_dict,
                )

            elif dest_type == "streamable_http":
                url = entry.get("url", "")
                if not isinstance(url, str) or not url.strip():
                    raise ValueError(
                        f"{config_path}: destination '{name}' (type: streamable_http) requires a non-empty 'url'."
                    )
                parsed_scheme = urlparse(url.strip()).scheme
                if parsed_scheme not in ("http", "https"):
                    raise ValueError(
                        f"{config_path}: destination '{name}' (type: streamable_http) url must use "
                        f"http or https scheme, got '{parsed_scheme}'."
                    )
                parsed[name] = DestinationConfig(
                    type="streamable_http",
                    url=url.strip().rstrip("/"),
                    env=env_dict,
                )

            else:  # stdio
                command = entry.get("command", "")
                if not isinstance(command, str) or not command.strip():
                    raise ValueError(
                        f"{config_path}: destination '{name}' (type: stdio) requires a non-empty 'command'."
                    )
                command = command.strip()
                # Reject shell metacharacters that could enable injection.
                # shlex.split is used (not a shell), but semicolons, pipes, etc.
                # in the config still indicate a misconfigured or malicious entry.
                _SHELL_METACHARS = set(";&|$<>()`\n\r")
                bad = _SHELL_METACHARS.intersection(command)
                if bad:
                    raise ValueError(
                        f"{config_path}: destination '{name}' command contains "
                        f"disallowed characters: {sorted(bad)}"
                    )
                parsed[name] = DestinationConfig(
                    type="stdio",
                    command=command,
                    env=env_dict,
                )

        else:
            raise ValueError(
                f"{config_path}: destination '{name}' must be a string URL or a mapping."
            )

    _destinations = parsed


def get_destination(name: str) -> Optional[DestinationConfig]:
    """Return the DestinationConfig for *name*, or None if unknown."""
    return _destinations.get(name)


def get_destination_url(name: str) -> Optional[str]:
    """Return the upstream URL for *name*, or None if unknown or stdio type."""
    dest = _destinations.get(name)
    if dest is None or dest.type != "sse":
        return None
    return dest.url


def get_stdio_destinations() -> dict[str, DestinationConfig]:
    """Return all stdio-type destinations."""
    return {n: d for n, d in _destinations.items() if d.type == "stdio"}


def destination_names() -> list[str]:
    """Return the list of configured destination names."""
    return list(_destinations.keys())
