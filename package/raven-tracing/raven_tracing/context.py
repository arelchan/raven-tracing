"""Per-turn trace context, propagated via contextvars.

contextvars survive ``await`` and are snapshotted when ``asyncio.create_task``
forks a child task — so a subagent spawned mid-turn (P1) inherits the turn's
span as parent automatically, and nested LLM/tool calls hang off the right node.
"""

from __future__ import annotations

import contextlib
import contextvars
import secrets
import time
from dataclasses import dataclass, replace
from typing import Iterator


@dataclass(frozen=True)
class TraceCtx:
    trace_id: str
    session_key: str | None = None
    channel: str | None = None
    chat_id: str | None = None
    parent_span_id: str | None = None


_CTX: contextvars.ContextVar[TraceCtx | None] = contextvars.ContextVar(
    "raven_tracing_ctx", default=None
)


def current() -> TraceCtx | None:
    return _CTX.get()


def new_trace_id() -> str:
    return f"trace-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def new_span_id() -> str:
    return f"span-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"


@contextlib.contextmanager
def turn_scope(
    *,
    session_key: str | None,
    channel: str | None,
    chat_id: str | None,
    root_span_id: str,
) -> Iterator[TraceCtx]:
    """Open a fresh trace for one turn; child spans parent onto ``root_span_id``."""
    ctx = TraceCtx(
        trace_id=new_trace_id(),
        session_key=session_key,
        channel=channel,
        chat_id=chat_id,
        parent_span_id=root_span_id,
    )
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)


@contextlib.contextmanager
def child_scope(span_id: str) -> Iterator[TraceCtx]:
    """Re-parent descendants onto ``span_id`` (used by the subagent probe, P1)."""
    cur = _CTX.get() or TraceCtx(trace_id=new_trace_id())
    token = _CTX.set(replace(cur, parent_span_id=span_id))
    try:
        yield _CTX.get()  # type: ignore[misc]
    finally:
        _CTX.reset(token)
