"""Secrets loader — reads config/secrets.yml for per-destination env vars.

Missing file is OK (returns empty mappings). Values are coerced to strings so
YAML-parsed ints/bools pass cleanly to subprocess env.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

_SECRETS_PATH_ENV = "SECRETS_CONFIG"
_DEFAULT_SECRETS_PATH = Path(__file__).parent.parent.parent / "config" / "secrets.yml"

_secrets: dict[str, dict[str, str]] = {}


def _resolve_secrets_path() -> Path:
    env_val = os.environ.get(_SECRETS_PATH_ENV)
    return Path(env_val) if env_val else _DEFAULT_SECRETS_PATH


def load_secrets(path: Optional[Path] = None) -> None:
    """Load config/secrets.yml.  Missing file is silently ignored."""
    global _secrets

    secrets_path = path or _resolve_secrets_path()

    if not secrets_path.exists():
        _secrets = {}
        return

    with secrets_path.open() as fh:
        try:
            raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse {secrets_path}: {exc}") from exc

    if raw is None:
        _secrets = {}
        return

    if not isinstance(raw, dict):
        raise ValueError(f"{secrets_path} must be a YAML mapping at the top level.")

    parsed: dict[str, dict[str, str]] = {}
    for dest_name, env_vars in raw.items():
        if not isinstance(env_vars, dict):
            raise ValueError(
                f"{secrets_path}: entry '{dest_name}' must be a mapping of env var names to values."
            )
        parsed[dest_name] = {k: str(v) for k, v in env_vars.items()}

    _secrets = parsed


def get_destination_env(name: str) -> dict[str, str]:
    """Return secrets-file env vars for the named destination, or empty dict."""
    return dict(_secrets.get(name, {}))
