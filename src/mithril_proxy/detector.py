"""Prompt injection detection for mithril-proxy.

Two detection engines:
  - Regex: deterministic pattern matching loaded from flat files in patterns.d/
  - AI: semantic classification via protectai/deberta-v3-base-prompt-injection-v2

Patterns are hot-reloadable via ``reload_patterns()`` (called from the admin
endpoint or SIGHUP handler).  The AI engine runs inference in a thread pool
executor so the asyncio event loop is never blocked.

Each destination independently configures both engines using four modes:
  off, monitor, redact, block.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import DestinationConfig

_log = logging.getLogger("mithril_proxy")

# --------------------------------------------------------------------------- #
#  Constants                                                                   #
# --------------------------------------------------------------------------- #

_REDACTION_PLACEHOLDER = "**REDACTED**"

_DEFAULT_PATTERNS_DIR = Path("/etc/mithril-proxy/patterns.d/")
_PATTERNS_DIR_ENV = "PATTERNS_DIR"

AI_INJECTION_THRESHOLD = float(
    os.environ.get("AI_INJECTION_THRESHOLD", "0.85")
)

# Mode severity ordering for "strictest wins" logic.
_MODE_SEVERITY = {"off": 0, "monitor": 1, "redact": 2, "block": 3}


# --------------------------------------------------------------------------- #
#  Pattern loader + hot-reload                                                 #
# --------------------------------------------------------------------------- #

_patterns: list[re.Pattern[str]] = []
_patterns_lock = threading.Lock()


def _resolve_patterns_dir() -> Path:
    env_val = os.environ.get(_PATTERNS_DIR_ENV)
    return Path(env_val) if env_val else _DEFAULT_PATTERNS_DIR


def load_patterns(patterns_dir: Optional[Path] = None) -> int:
    """Load regex patterns from flat files in *patterns_dir*.

    Each file (``*.txt`` or ``*.conf``) is read line-by-line.  Blank lines and
    lines starting with ``#`` are skipped.  Invalid regexes log a WARNING and
    are skipped.  If the directory does not exist, a WARNING is logged and 0 is
    returned.

    Returns the number of successfully compiled patterns.
    """
    target = patterns_dir or _resolve_patterns_dir()

    if not target.is_dir():
        _log.warning("Patterns directory does not exist: %s — regex engine has 0 patterns", target)
        with _patterns_lock:
            global _patterns
            _patterns = []
        return 0

    compiled: list[re.Pattern[str]] = []
    for filepath in sorted(target.iterdir()):
        if filepath.suffix not in (".txt", ".conf"):
            continue
        try:
            lines = filepath.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            _log.warning("Cannot read pattern file %s: %s", filepath, exc)
            continue

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                compiled.append(re.compile(stripped, re.IGNORECASE))
            except re.error as exc:
                _log.warning(
                    "Invalid regex in %s line %d: %r — %s",
                    filepath.name, lineno, stripped, exc,
                )

    with _patterns_lock:
        _patterns = compiled

    _log.info("Loaded %d regex patterns from %s", len(compiled), target)
    return len(compiled)


def reload_patterns() -> int:
    """Reload patterns from the configured directory.  Intended for the admin endpoint."""
    return load_patterns()


# --------------------------------------------------------------------------- #
#  AI engine                                                                    #
# --------------------------------------------------------------------------- #

_ai_pipeline = None  # type: ignore[assignment]
_ai_executor: ThreadPoolExecutor | None = None


_AI_MAX_WORKERS = int(os.environ.get("AI_MAX_WORKERS", "1"))


def init_detector() -> None:
    """Attempt to load the AI classification pipeline.

    On ``ImportError`` or ``OSError`` the AI engine is disabled with a WARNING.
    A dedicated thread pool with ``AI_MAX_WORKERS`` (default 1) workers is
    created for AI inference so it doesn't saturate the default executor.
    """
    global _ai_pipeline, _ai_executor
    try:
        from transformers import pipeline as hf_pipeline  # type: ignore[import-untyped]

        _ai_pipeline = hf_pipeline(
            "text-classification",
            model="protectai/deberta-v3-base-prompt-injection-v2",
        )
        _ai_executor = ThreadPoolExecutor(max_workers=_AI_MAX_WORKERS)
        _log.info("AI injection detector loaded successfully")
    except (ImportError, OSError, Exception) as exc:
        _log.warning("AI engine unavailable: %s", exc)
        _ai_pipeline = None


def _run_ai(text: str) -> float:
    """Run AI inference synchronously.  Returns the injection confidence score."""
    if _ai_pipeline is None:
        return 0.0
    try:
        results = _ai_pipeline(text)
        if results and isinstance(results, list):
            result = results[0]
            label = result.get("label", "").upper()
            score = float(result.get("score", 0.0))
            # The model returns INJECTION or SAFE labels
            if "INJECTION" in label:
                return score
            # If the label is SAFE, the injection score is 1 - safe_score
            return 1.0 - score
        return 0.0
    except Exception as exc:
        _log.warning("AI inference error: %s", exc)
        return 0.0


# --------------------------------------------------------------------------- #
#  DetectionResult + scan()                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class DetectionResult:
    """Result of scanning a body through the detection engines."""
    action: str  # "pass", "monitor", "redact", "block"
    engine: Optional[str] = None  # "regex", "ai", or None
    detail: Optional[str] = None  # matched pattern or confidence score
    body: str = ""  # the (possibly redacted) body to forward


async def scan(
    body: str,
    dest_config: DestinationConfig,
    *,
    is_response: bool = False,
) -> DetectionResult:
    """Scan *body* through regex and AI engines per *dest_config* modes.

    Returns a :class:`DetectionResult` describing the action to take and the
    (possibly redacted) body.  When both engines trigger, the **stricter mode
    wins** (block > redact > monitor).
    """
    if not body:
        return DetectionResult(action="pass", body=body)

    regex_mode = dest_config.regex_mode
    ai_mode = dest_config.ai_mode

    if regex_mode == "off" and ai_mode == "off":
        return DetectionResult(action="pass", body=body)

    best_action = "pass"
    best_engine: Optional[str] = None
    best_detail: Optional[str] = None
    result_body = body

    # --- Regex pass ---
    if regex_mode != "off":
        # Copy the list reference so we don't hold the lock during matching.
        with _patterns_lock:
            current_patterns = _patterns

        for pattern in current_patterns:
            if pattern.search(body):
                if _MODE_SEVERITY.get(regex_mode, 0) > _MODE_SEVERITY.get(best_action, 0):
                    best_action = regex_mode
                    best_engine = "regex"
                    best_detail = pattern.pattern
                    if regex_mode == "redact":
                        result_body = pattern.sub(_REDACTION_PLACEHOLDER, body)
                break  # stop on first match

    # --- AI pass ---
    if ai_mode != "off" and best_action != "block":
        if _ai_pipeline is None:
            pass  # AI unavailable; skip silently
        elif len(body) > dest_config.ai_max_chars:
            _log.warning(
                "AI scan skipped: body exceeds %d chars (%d)",
                dest_config.ai_max_chars, len(body),
            )
        else:
            loop = asyncio.get_running_loop()
            score = await loop.run_in_executor(_ai_executor, _run_ai, body)
            threshold = (
                dest_config.ai_threshold
                if dest_config.ai_threshold is not None
                else AI_INJECTION_THRESHOLD
            )
            if score >= threshold:
                if _MODE_SEVERITY.get(ai_mode, 0) > _MODE_SEVERITY.get(best_action, 0):
                    best_action = ai_mode
                    best_engine = "ai"
                    best_detail = f"score={score:.3f}"
                    if ai_mode == "redact":
                        result_body = _REDACTION_PLACEHOLDER

    if best_action == "block":
        return DetectionResult(
            action="block", engine=best_engine, detail=best_detail, body=body,
        )

    if best_action == "pass":
        return DetectionResult(action="pass", body=body)

    return DetectionResult(
        action=best_action, engine=best_engine, detail=best_detail, body=result_body,
    )
