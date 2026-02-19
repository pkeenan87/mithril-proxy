"""Destination config loader — reads config/destinations.yml on startup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

_CONFIG_PATH_ENV = "DESTINATIONS_CONFIG"
_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "destinations.yml"

_destinations: dict[str, str] = {}


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

    parsed: dict[str, str] = {}
    for name, entry in destinations_raw.items():
        if isinstance(entry, str):
            url = entry
        elif isinstance(entry, dict):
            url = entry.get("url", "")
        else:
            raise ValueError(
                f"{config_path}: destination '{name}' must be a string URL or a mapping with a 'url' key."
            )

        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"{config_path}: destination '{name}' has an empty or invalid URL."
            )

        parsed[name] = url.rstrip("/")

    _destinations = parsed


def get_destination_url(name: str) -> Optional[str]:
    """Return the upstream URL for *name*, or None if unknown."""
    return _destinations.get(name)


def destination_names() -> list[str]:
    """Return the list of configured destination names."""
    return list(_destinations.keys())
