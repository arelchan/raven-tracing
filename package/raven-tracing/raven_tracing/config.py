"""Import-time configuration for raven-tracing.

Deliberately dependency-free and side-effect-light: we must NOT import raven
internals here. This module is first imported during Raven plugin discovery,
before the agent config is loaded, so everything is driven by environment
variables with sane defaults — the plugin works the moment it is pip-installed.
"""

from __future__ import annotations

import os
from pathlib import Path

_OFF = {"0", "false", "off", "no"}


def enabled() -> bool:
    """Tracing is on unless ``RAVEN_TRACING`` is a falsy string."""
    return os.environ.get("RAVEN_TRACING", "1").strip().lower() not in _OFF


def state_dir() -> Path:
    """Trace state dir. Matches the viewer's raven convention (``~/.raven/traces``).

    Overridable with ``RAVEN_TRACING_DIR`` (absolute) or ``RAVEN_HOME``.
    Spans land at ``<state_dir>/logs/audit-spans.log``.
    """
    override = os.environ.get("RAVEN_TRACING_DIR")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("RAVEN_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".raven"
    return base / "traces"


def preview_len() -> int:
    """Max chars kept inline on a span; full payloads go to artifacts."""
    try:
        return max(0, int(os.environ.get("RAVEN_TRACING_PREVIEW", "500")))
    except ValueError:
        return 500
