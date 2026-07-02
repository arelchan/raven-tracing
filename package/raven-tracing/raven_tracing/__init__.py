"""raven-tracing — pluggable, non-invasive tracing for Raven.

Raven's plugin discovery imports this package (to read the bundled
``raven-plugin.toml`` via importlib.resources). We use that import as the
hook to install auto-instrumentation — the same pattern OpenTelemetry uses.
This runs before any ``AgentLoop`` is constructed, and we patch *class* methods,
so every later instance is observed.

Turn off with ``RAVEN_TRACING=0``. Spans land at
``~/.raven/traces/logs/audit-spans.log`` (override ``RAVEN_TRACING_DIR``).
"""

from __future__ import annotations

import logging

from . import config

__version__ = "0.1.0"

if config.enabled():
    try:
        from . import instrument

        instrument.install()
    except Exception:  # noqa: BLE001 — instrumentation must never break the host
        logging.getLogger("raven.plugin.raven-tracing").warning(
            "raven-tracing: install failed; agent unaffected", exc_info=True
        )
