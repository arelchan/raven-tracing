"""Import-time auto-instrumentation of EverClaw (the OpenTelemetry pattern).

We monkeypatch a small set of stable choke points so a pip-installed plugin can
observe the agent with zero edits to everclaw source. Every patch is guarded:
a signature mismatch (an everclaw refactor) disables that *single* probe and
logs a warning — it never crashes the host, and other probes keep working.

P0 probes (full fidelity):
  - session.turn   ← AgentLoop._process_message
  - llm.call       ← LLMProvider.chat_with_retry
  - tool.call / skill.use / skill.read   ← ToolRegistry.execute

P1: memory.*, subagent.call, plugin.load, skill.inject.

Skill activity has two surfaces and we capture both:
  - skill.use / skill.read   ← the use_skill/read_skill tools, AND a read_file
    whose path is a SKILL.md (the model reading a skill body on its own).
  - skill.inject             ← the context engine auto-injecting skill BODIES
    into the system prompt, via ActiveSkillsSegmentBuilder (always-on skills)
    and SkillsSegmentBuilder (router + gate selected skills). This is the
    dominant path and happens with no tool call at all.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import time
from collections import Counter
from typing import Any, Callable

from . import config
from . import context as ctx
from . import spans
from . import usage as usage_mod
from .store import preview_text

logger = logging.getLogger("everclaw.plugin.everclaw-tracing")

# Skill tools surface as ordinary tool calls; we re-type them to `skills`.
_SKILL_TOOLS = {"use_skill", "read_skill"}
# A read_file whose path is a SKILL.md is the model reading a skill body itself
# (the "summary" injection mode literally tells it to read_file the SKILL.md);
# re-type that single-purpose read to skill.read so it shows as a skill node.
_FILE_READ_TOOLS = {"read_file"}

# key "Class.method" -> (target_cls, method_name, original_fn) for uninstall.
_originals: dict[str, tuple[type, str, Callable]] = {}
_installed: dict[str, bool] = {}
_done = False


# ── helpers ───────────────────────────────────────────────────────────────


def _preview(value: Any, n: int | None = None) -> str:
    return preview_text(value, n if n is not None else config.preview_len())


def _split_session_key(session_key: str | None) -> tuple[str | None, str | None]:
    if session_key and ":" in session_key:
        channel, _, chat_id = session_key.partition(":")
        return channel or None, chat_id or None
    return None, None


def _wrap(target_cls: type, method_name: str, factory: Callable[[Callable], Callable], *, expect_params: list[str]) -> bool:
    """Patch ``target_cls.method_name`` with ``factory(original)``.

    Guarded by an async-ness + signature check on ``expect_params`` so an
    everclaw refactor degrades to a disabled probe, never a crash.
    """
    key = f"{target_cls.__name__}.{method_name}"
    try:
        original = getattr(target_cls, method_name)
        if not inspect.iscoroutinefunction(original):
            logger.warning("everclaw-tracing: %s is not async; probe disabled", key)
            return False
        params = set(inspect.signature(original).parameters)
        missing = [p for p in expect_params if p not in params]
        if missing:
            logger.warning("everclaw-tracing: %s missing %s; probe disabled", key, missing)
            return False
        wrapper = factory(original)
        functools.update_wrapper(wrapper, original)
        setattr(target_cls, method_name, wrapper)
        _originals[key] = (target_cls, method_name, original)
        _installed[key] = True
        logger.debug("everclaw-tracing: patched %s", key)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("everclaw-tracing: failed to patch %s: %s", key, e)
        return False


# ── probe: session.turn ─────────────────────────────────────────────────────


def _turn_capabilities(loop: Any) -> dict[str, Any]:
    """Snapshot what this turn's agent has loaded: tools, plugin backend +
    plugin-contributed tools, and the available skills. Read off the AgentLoop
    (``self``) at the turn probe. Each piece is best-effort — a missing attr
    just omits that field, never breaks the turn span. This makes every trace
    self-describing (e.g. a TUI trace plainly shows backend=null / no plugins)."""
    caps: dict[str, Any] = {}
    try:
        names = loop.tools.tool_names()
        caps["turn.tools"] = list(names)
        caps["turn.tool_count"] = len(names)
    except Exception:  # noqa: BLE001
        pass
    try:
        backend = getattr(loop, "backend", None)
        caps["turn.plugin.backend"] = type(backend).__name__ if backend is not None else None
    except Exception:  # noqa: BLE001
        pass
    try:
        ptools = getattr(loop, "plugin_tools", None) or []
        caps["turn.plugin.tools"] = [getattr(t, "name", None) for t in ptools]
    except Exception:  # noqa: BLE001
        pass
    try:
        cat = getattr(getattr(loop, "context", None), "skills", None)
        reg = getattr(cat, "registry", None) or getattr(cat, "_registry", None)
        metas = list(reg.list_all()) if reg is not None else []
        caps["turn.skills"] = [getattr(m, "name", None) for m in metas][:50]
        caps["turn.skill_count"] = len(metas)
    except Exception:  # noqa: BLE001
        pass
    return caps


def _install_turn(AgentLoop: type) -> bool:
    def factory(original):
        async def wrapper(self, msg, session_key=None, *args, **kwargs):
            sk = session_key or getattr(msg, "session_key", None)
            channel = getattr(msg, "channel", None)
            chat_id = getattr(msg, "chat_id", None)
            if not channel or not chat_id:
                ch2, cid2 = _split_session_key(sk)
                channel, chat_id = channel or ch2, chat_id or cid2
            root_id = ctx.new_span_id()
            start = spans.now_iso()
            # everclaw renamed the turn payload: old `msg.content` became
            # `TurnRequest.text`. `msg` is the positional arg (a TurnRequest on
            # current everclaw). Accept either so the probe spans both versions.
            user_input = getattr(msg, "text", None)
            if user_input is None:
                user_input = getattr(msg, "content", None)
            with ctx.turn_scope(session_key=sk, channel=channel, chat_id=chat_id, root_span_id=root_id) as tc:
                meta = {"traceId": tc.trace_id, "sessionKey": sk}
                in_art = spans.persist_artifact(
                    "turn-input", meta,
                    {"content": user_input, "channel": channel, "chat_id": chat_id, "media": getattr(msg, "media", None)},
                    label="turn-input",
                )
                # Emit the root span up-front (in-progress) so child spans always
                # have a root to group under WHILE the turn is still open. It's
                # re-emitted on completion with end time / output; the viewer
                # dedups by spanId so the final version wins. Without this, mid-turn
                # children are orphaned (root not yet written) and scatter across
                # synthetic groups until the turn closes.
                open_attrs = {"turn.input_preview": _preview(user_input), "turn.in_progress": True}
                open_attrs.update(spans.artifact_attributes("turn.input", in_art))
                spans.emit(spans.build_span(
                    "session.turn", "session",
                    trace_id=tc.trace_id, span_id=root_id, parent_span_id=None,
                    session_key=sk, channel=channel, chat_id=chat_id,
                    start_time=start, end_time=start,
                    status_code="OK", status_message="",
                    attributes=open_attrs,
                    events=[{"time": start, "name": "turn.start"}],
                ))
                status_code, status_msg, out_content = "OK", "", None
                try:
                    result = await original(self, msg, session_key, *args, **kwargs)
                    out_content = getattr(result, "content", None) if result is not None else None
                    return result
                except Exception as e:
                    status_code, status_msg = "ERROR", repr(e)
                    raise
                finally:
                    out_art = spans.persist_artifact("turn-output", meta, {"content": out_content}, label="turn-output")
                    attrs = {
                        "turn.input_preview": _preview(user_input),
                        "turn.output_preview": _preview(out_content),
                        "turn.in_progress": False,
                    }
                    attrs.update(_turn_capabilities(self))  # tools / plugins / skills this turn
                    attrs.update(spans.artifact_attributes("turn.input", in_art))
                    attrs.update(spans.artifact_attributes("turn.output", out_art))
                    spans.emit(spans.build_span(
                        "session.turn", "session",
                        trace_id=tc.trace_id, span_id=root_id, parent_span_id=None,
                        session_key=sk, channel=channel, chat_id=chat_id,
                        start_time=start, end_time=spans.now_iso(),
                        status_code=status_code, status_message=status_msg,
                        attributes=attrs,
                        events=[{"time": start, "name": "turn.start"}, {"time": spans.now_iso(), "name": "turn.end"}],
                    ))
        return wrapper

    # Guard on `session_key` only: everclaw's first positional arg was renamed
    # (msg -> req) but binds positionally, so its name is irrelevant. Checking
    # for `msg` here used to disable the whole root-span probe after that
    # rename, orphaning every child span.
    return _wrap(AgentLoop, "_process_message", factory, expect_params=["session_key"])


# ── probe: llm.call ─────────────────────────────────────────────────────────


def _llm_attrs(resp: Any, provider: str, model: str | None) -> dict[str, Any]:
    attrs: dict[str, Any] = {"llm.provider": provider, "llm.model": model}
    if resp is None:
        return attrs
    attrs["llm.finish_reason"] = getattr(resp, "finish_reason", None)
    attrs["llm.output_preview"] = _preview(getattr(resp, "content", None))
    tool_calls = getattr(resp, "tool_calls", None) or []
    attrs["llm.tool_call_count"] = len(tool_calls)
    if tool_calls:
        attrs["llm.tool_names"] = [getattr(t, "name", None) for t in tool_calls]
    u = usage_mod.normalize(getattr(resp, "usage", None), model)
    attrs["llm.usage.input_tokens"] = u["input_tokens"]
    attrs["llm.usage.output_tokens"] = u["output_tokens"]
    attrs["llm.usage.cache_read_tokens"] = u["cache_read_tokens"]
    attrs["llm.usage.cache_write_tokens"] = u["cache_write_tokens"]
    attrs["llm.usage.total_tokens"] = u["total_tokens"]
    attrs["llm.usage.cost_total"] = u["cost_usd"]
    reasoning = getattr(resp, "reasoning_content", None)
    if reasoning:
        attrs["llm.reasoning_preview"] = _preview(reasoning)
    return attrs


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        import json as _json
        return _json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


def _llm_input_payload(provider: str, model: str | None, messages: Any, tools: Any) -> dict:
    """Artifact payload for the model-input card.

    everclaw passes ONE flat ``messages`` list (system + prior turns + current).
    We split it into three non-overlapping views for the viewer:
      - ``systemPrompt``: the system message,
      - ``prompt``: the latest user message (the current input to this call),
      - ``historyMessages``: the prior turns only — everything EXCEPT the system
        message and that latest user message (so it doesn't duplicate them).
    ``messages`` keeps the full raw list as the ground truth of what was sent.
    """
    msgs = messages if isinstance(messages, list) else []
    system_prompt = ""
    user_prompt = ""
    system_idxs: set[int] = set()
    last_user_idx: int | None = None
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "system":
            system_idxs.add(i)
            if not system_prompt:
                system_prompt = _coerce_text(m.get("content"))
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, dict) and m.get("role") == "user":
            user_prompt = _coerce_text(m.get("content"))
            last_user_idx = i
            break
    history = [m for i, m in enumerate(msgs) if i not in system_idxs and i != last_user_idx]
    return {
        "provider": provider,
        "model": model,
        "systemPrompt": system_prompt,
        "prompt": user_prompt,
        "historyMessages": history,
        "messages": messages,
        "tools": tools,
    }


def _llm_output_payload(resp: Any) -> Any:
    if resp is None:
        return None
    content = getattr(resp, "content", None)
    return {
        "content": content,
        "output": content,  # field the shared viewer's model-output card reads
        "finish_reason": getattr(resp, "finish_reason", None),
        "tool_calls": [
            {"id": getattr(t, "id", None), "name": getattr(t, "name", None), "arguments": getattr(t, "arguments", None)}
            for t in (getattr(resp, "tool_calls", None) or [])
        ],
        "reasoning_content": getattr(resp, "reasoning_content", None),
        "usage": getattr(resp, "usage", None),
    }


def _install_llm(LLMProvider: type) -> bool:
    def factory(original):
        async def wrapper(self, messages=None, tools=None, model=None, *args, **kwargs):
            tc = ctx.current()
            trace_id = tc.trace_id if tc else ctx.new_trace_id()
            parent = tc.parent_span_id if tc else None
            sk = tc.session_key if tc else None
            channel = tc.channel if tc else None
            chat_id = tc.chat_id if tc else None
            span_id = ctx.new_span_id()
            start = spans.now_iso()
            provider_name = type(self).__name__
            eff_model = model or getattr(self, "default_model", None)
            meta = {"traceId": trace_id, "sessionKey": sk}
            in_art = spans.persist_artifact(
                "llm-input", meta,
                _llm_input_payload(provider_name, eff_model, messages, tools),
                label="llm-input",
            )
            status_code, status_msg, resp = "OK", "", None
            try:
                resp = await original(self, messages, tools, model, *args, **kwargs)
                if getattr(resp, "finish_reason", None) == "error":
                    status_code, status_msg = "ERROR", _preview(getattr(resp, "content", ""), 200)
                return resp
            except Exception as e:
                status_code, status_msg = "ERROR", repr(e)
                raise
            finally:
                attrs = _llm_attrs(resp, provider_name, eff_model)
                out_art = spans.persist_artifact("llm-output", meta, _llm_output_payload(resp), label="llm-output")
                attrs.update(spans.artifact_attributes("llm.input", in_art))
                attrs.update(spans.artifact_attributes("llm.output", out_art))
                spans.emit(spans.build_span(
                    "llm.call", "model_call",
                    trace_id=trace_id, span_id=span_id, parent_span_id=parent,
                    session_key=sk, channel=channel, chat_id=chat_id,
                    start_time=start, end_time=spans.now_iso(),
                    status_code=status_code, status_message=status_msg,
                    attributes=attrs,
                    events=[{"time": start, "name": "llm.start"}, {"time": spans.now_iso(), "name": "llm.end"}],
                ))
        return wrapper

    return _wrap(LLMProvider, "chat_with_retry", factory, expect_params=["messages", "model"])


def _install_llm_stream(AgentLoop: type) -> bool:
    """Capture the STREAMING main-agent LLM call.

    When a turn wires ``on_token_delta`` (the TUI does), AgentLoop diverts to
    ``_llm_call_stream`` → ``provider.chat_stream`` instead of
    ``chat_with_retry`` — so the chat_with_retry probe misses the main model
    call entirely (only the SkillForge rewriter, which uses chat_with_retry,
    shows up). ``_llm_call_stream`` returns an accumulated ``LLMResponse``, so
    we wrap it with the same span shape. Here ``self`` is the AgentLoop, so the
    provider/model come from ``self.provider``.
    """
    def factory(original):
        async def wrapper(self, messages=None, tools=None, model=None, *args, **kwargs):
            tc = ctx.current()
            trace_id = tc.trace_id if tc else ctx.new_trace_id()
            parent = tc.parent_span_id if tc else None
            sk = tc.session_key if tc else None
            channel = tc.channel if tc else None
            chat_id = tc.chat_id if tc else None
            span_id = ctx.new_span_id()
            start = spans.now_iso()
            provider = getattr(self, "provider", None)
            provider_name = type(provider).__name__ if provider is not None else "stream"
            eff_model = model or getattr(provider, "default_model", None)
            meta = {"traceId": trace_id, "sessionKey": sk}
            in_art = spans.persist_artifact(
                "llm-input", meta, _llm_input_payload(provider_name, eff_model, messages, tools), label="llm-input",
            )
            status_code, status_msg, resp = "OK", "", None
            try:
                resp = await original(self, messages, tools, model, *args, **kwargs)
                if getattr(resp, "finish_reason", None) == "error":
                    status_code, status_msg = "ERROR", _preview(getattr(resp, "content", ""), 200)
                return resp
            except Exception as e:
                status_code, status_msg = "ERROR", repr(e)
                raise
            finally:
                attrs = _llm_attrs(resp, provider_name, eff_model)
                attrs["llm.stream"] = True
                out_art = spans.persist_artifact("llm-output", meta, _llm_output_payload(resp), label="llm-output")
                attrs.update(spans.artifact_attributes("llm.input", in_art))
                attrs.update(spans.artifact_attributes("llm.output", out_art))
                spans.emit(spans.build_span(
                    "llm.call", "model_call",
                    trace_id=trace_id, span_id=span_id, parent_span_id=parent,
                    session_key=sk, channel=channel, chat_id=chat_id,
                    start_time=start, end_time=spans.now_iso(),
                    status_code=status_code, status_message=status_msg,
                    attributes=attrs,
                    events=[{"time": start, "name": "llm.start"}, {"time": spans.now_iso(), "name": "llm.end"}],
                ))
        return wrapper

    return _wrap(AgentLoop, "_llm_call_stream", factory, expect_params=["messages", "model"])


# ── probe: tool.call / skill.use / skill.read ───────────────────────────────


def _parse_skill_name(result: Any) -> str | None:
    """Skill tools return ``"## {name}\\n..."`` (hub: ``"## {name} ({version})"``)."""
    if not isinstance(result, str) or not result.strip():
        return None
    first = result.lstrip().splitlines()[0]
    if not first.startswith("## "):
        return None
    name = first[3:].strip()
    if name.endswith(")") and " (" in name:
        name = name[: name.rindex(" (")].strip()
    return name or None


def _skill_attrs(tool_name: str, params: Any, result: Any) -> dict[str, Any]:
    skill_id = params.get("skill_id") if isinstance(params, dict) else None
    source = native = None
    if isinstance(skill_id, str) and "/" in skill_id:
        source, _, native = skill_id.partition("/")
    return {
        "skill.id": skill_id,
        "skill.source": source,
        "skill.native_id": native,
        "skill.name": _parse_skill_name(result),
        "skill.tool": tool_name,
        "skill.result_preview": _preview(result),
    }


def _skill_name_from_path(path: str | None) -> str | None:
    """``…/skills/weather/SKILL.md`` → ``weather`` (the skill dir name)."""
    if not path:
        return None
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if len(parts) >= 2 and parts[-1].lower() == "skill.md":
        return parts[-2]
    return parts[-1] if parts else None


def _skill_read_path(name: str, params: Any) -> str | None:
    """If a read_file targets a SKILL.md, return that path; else ``None``.

    This is the discovery→injection follow-through: everclaw's summary mode
    tells the agent to ``read_file`` a skill's SKILL.md, and subagents (which
    only get the skill *catalog*) do the same. Those reads carry the real body
    into context but look like a plain file read — re-type them to skill.read.
    """
    if name not in _FILE_READ_TOOLS:
        return None
    path = params.get("path") if isinstance(params, dict) else (params if isinstance(params, str) else None)
    if isinstance(path, str) and path.replace("\\", "/").lower().rstrip("/").endswith("skill.md"):
        return path
    return None


def _install_tool(ToolRegistry: type) -> bool:
    def factory(original):
        async def wrapper(self, name, params, *args, **kwargs):
            tc = ctx.current()
            trace_id = tc.trace_id if tc else ctx.new_trace_id()
            parent = tc.parent_span_id if tc else None
            sk = tc.session_key if tc else None
            channel = tc.channel if tc else None
            chat_id = tc.chat_id if tc else None
            span_id = ctx.new_span_id()
            start = spans.now_iso()
            t0 = time.monotonic()
            meta = {"traceId": trace_id, "sessionKey": sk}
            in_art = spans.persist_artifact("tool-input", meta, {"name": name, "params": params}, label=name or "tool-input")
            status_code, status_msg, result = "OK", "", None
            try:
                # child_scope so a tool that spawns a background task (e.g. the
                # `spawn` tool → SubagentManager) nests its child under this span.
                with ctx.child_scope(span_id):
                    result = await original(self, name, params, *args, **kwargs)
                if isinstance(result, str) and result.startswith("Error"):
                    status_code, status_msg = "ERROR", _preview(result, 200)
                return result
            except Exception as e:
                status_code, status_msg = "ERROR", repr(e)
                raise
            finally:
                duration_ms = int((time.monotonic() - t0) * 1000)
                out_art = spans.persist_artifact("tool-output", meta, {"result": result}, label=name or "tool-output")
                skill_read_path = _skill_read_path(name, params)
                if name in _SKILL_TOOLS:
                    span_name = "skill.use" if name == "use_skill" else "skill.read"
                    span_type = "skills"
                    attrs = _skill_attrs(name, params, result)
                elif skill_read_path:
                    span_name, span_type = "skill.read", "skills"
                    attrs = {
                        "skill.tool": name,
                        "skill.injected_via": "read_file",
                        "skill.path": skill_read_path,
                        "skill.name": _skill_name_from_path(skill_read_path),
                        "skill.result_preview": _preview(result),
                    }
                else:
                    span_name, span_type = "tool.call", "tool_call"
                    attrs = {
                        "tool.name": name,
                        "tool.args_preview": _preview(params, 300),
                        "tool.result_preview": _preview(result),
                        "tool.error": status_msg or None,
                    }
                attrs["tool.duration_ms"] = duration_ms
                attrs.update(spans.artifact_attributes("tool.input", in_art))
                attrs.update(spans.artifact_attributes("tool.output", out_art))
                spans.emit(spans.build_span(
                    span_name, span_type,
                    trace_id=trace_id, span_id=span_id, parent_span_id=parent,
                    session_key=sk, channel=channel, chat_id=chat_id,
                    start_time=start, end_time=spans.now_iso(),
                    status_code=status_code, status_message=status_msg,
                    attributes=attrs,
                    events=[{"time": start, "name": "tool.start"}, {"time": spans.now_iso(), "name": "tool.end"}],
                ))
        return wrapper

    return _wrap(ToolRegistry, "execute", factory, expect_params=["name", "params"])


# ── P1 helpers ──────────────────────────────────────────────────────────────


def _bound(original: Callable, self: Any, args: tuple, kwargs: dict) -> dict:
    """Bind call args to the original signature so extractors read by name,
    robust to positional-vs-keyword calling. Best-effort; {} on mismatch."""
    try:
        b = inspect.signature(original).bind(self, *args, **kwargs)
        b.apply_defaults()
        return dict(b.arguments)
    except Exception:  # noqa: BLE001
        return {}


def _wrap_sync(target_cls: type, method_name: str, factory: Callable[[Callable], Callable], *, expect_params: list[str]) -> bool:
    """Like _wrap, for a *synchronous* method (e.g. PluginRegistry.build_*)."""
    key = f"{target_cls.__name__}.{method_name}"
    try:
        original = getattr(target_cls, method_name)
        params = set(inspect.signature(original).parameters)
        missing = [p for p in expect_params if p not in params]
        if missing:
            logger.warning("everclaw-tracing: %s missing %s; probe disabled", key, missing)
            return False
        wrapper = factory(original)
        functools.update_wrapper(wrapper, original)
        setattr(target_cls, method_name, wrapper)
        _originals[key] = (target_cls, method_name, original)
        _installed[key] = True
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("everclaw-tracing: failed to patch %s: %s", key, e)
        return False


def _make_async_probe(name: str, span_type: str, *, extract_in, extract_out, nest: bool = False):
    """Build ``factory(original)`` → wrapper emitting one span around an async call.

    ``extract_in(bound, meta) -> dict`` and ``extract_out(result, meta) -> dict``
    return span attributes; either may persist artifacts via ``meta``.
    ``nest=True`` re-parents descendants onto this span (e.g. consolidate→extract).
    """
    def factory(original):
        async def wrapper(self, *args, **kwargs):
            tc = ctx.current()
            trace_id = tc.trace_id if tc else ctx.new_trace_id()
            parent = tc.parent_span_id if tc else None
            sk = tc.session_key if tc else None
            channel = tc.channel if tc else None
            chat_id = tc.chat_id if tc else None
            span_id = ctx.new_span_id()
            start = spans.now_iso()
            meta = {"traceId": trace_id, "sessionKey": sk}
            try:
                in_attrs = extract_in(_bound(original, self, args, kwargs), meta) or {}
            except Exception:  # noqa: BLE001
                in_attrs = {}
            status_code, status_msg, result = "OK", "", None
            scope = ctx.child_scope(span_id) if nest else contextlib.nullcontext()
            try:
                with scope:
                    result = await original(self, *args, **kwargs)
                return result
            except Exception as e:
                status_code, status_msg = "ERROR", repr(e)
                raise
            finally:
                try:
                    out_attrs = extract_out(result, meta) or {}
                except Exception:  # noqa: BLE001
                    out_attrs = {}
                attrs = {**in_attrs, **out_attrs}
                spans.emit(spans.build_span(
                    name, span_type,
                    trace_id=trace_id, span_id=span_id, parent_span_id=parent,
                    session_key=sk, channel=channel, chat_id=chat_id,
                    start_time=start, end_time=spans.now_iso(),
                    status_code=status_code, status_message=status_msg,
                    attributes=attrs,
                ))
        return wrapper
    return factory


# ── P1 probe: memory.recall ─────────────────────────────────────────────────


def _install_memory_recall(MemorySegmentBuilder: type) -> bool:
    def _in(b, meta):
        self = b.get("self")
        return {
            "memory.query": _preview(b.get("query"), 300),
            "memory.scope": "user",
            "memory.user_id": getattr(self, "_user_id", None),
            "memory.top_k": getattr(self, "_memory_top_k", None),
        }

    def _out(result, meta):
        hits = list(result or [])
        art = spans.persist_artifact(
            "memory-recall", meta,
            [{"text": getattr(m, "text", None), "score": getattr(m, "score", None),
              "metadata": getattr(m, "metadata", None)} for m in hits],
            label="memory-recall",
        )
        return {"memory.hits": len(hits), **spans.artifact_attributes("memory.recall", art)}

    return _wrap(MemorySegmentBuilder, "_recall",
                 _make_async_probe("memory.recall", "memory_recall", extract_in=_in, extract_out=_out),
                 expect_params=["query"])


# ── P1 probe: memory.store + memory.feedback (on AgentLoop) ──────────────────


def _install_agentloop_memory(AgentLoop: type) -> bool:
    def _store_in(b, meta):
        msgs = b.get("messages_slice") or []
        art = spans.persist_artifact(
            "memory-store", meta, {"session_id": b.get("session_key"), "messages": msgs}, label="memory-store"
        )
        return {"memory.session_id": b.get("session_key"), "memory.message_count": len(msgs),
                **spans.artifact_attributes("memory.store", art)}

    def _feedback_in(b, meta):
        return {"memory.session_id": b.get("session_key"),
                "memory.injected": b.get("injected_skill_ids"),
                "memory.used": b.get("used_skill_ids")}

    ok1 = _wrap(AgentLoop, "_dispatch_backend_store",
                _make_async_probe("memory.store", "memory_store", extract_in=_store_in, extract_out=lambda r, m: {}),
                expect_params=["session_key", "messages_slice"])
    ok2 = _wrap(AgentLoop, "_dispatch_backend_feedback",
                _make_async_probe("memory.feedback", "memory_feedback", extract_in=_feedback_in, extract_out=lambda r, m: {}),
                expect_params=["session_key", "injected_skill_ids"])
    return ok1 or ok2


# ── P1 probe: memory.extract + memory.profile_refresh (on MemoryStore) ───────


def _install_memory_store_class(MemoryStore: type) -> bool:
    def _extract_in(b, meta):
        msgs = b.get("messages") or []
        return {"memory.surface": "host", "memory.model": b.get("model"),
                "memory.message_count": len(msgs), "memory.enable_foresight": b.get("enable_foresight")}

    def _profile_in(b, meta):
        return {"memory.model": b.get("model"), "memory.threshold": b.get("threshold")}

    ok1 = _wrap(MemoryStore, "annotate",
                _make_async_probe("memory.extract", "memory_extract",
                                  extract_in=_extract_in, extract_out=lambda r, m: {"memory.annotated": bool(r)}),
                expect_params=["messages", "model"])
    ok2 = _wrap(MemoryStore, "maybe_refresh_hot_tags",
                _make_async_probe("memory.profile_refresh", "memory_profile_refresh",
                                  extract_in=_profile_in,
                                  extract_out=lambda r, m: {"memory.sections_rewritten": r if isinstance(r, int) else None}),
                expect_params=["provider", "model"])
    return ok1 or ok2


# ── P1 probe: memory.consolidate (on MemoryConsolidator) ─────────────────────


def _install_memory_consolidate(MemoryConsolidator: type) -> bool:
    def _in(b, meta):
        session = b.get("session")
        return {"memory.session_key": getattr(session, "key", None),
                "memory.last_consolidated": getattr(session, "last_consolidated", None),
                "memory.message_count": len(getattr(session, "messages", []) or [])}

    # nest=True so the extract / profile_refresh it triggers nest under it.
    return _wrap(MemoryConsolidator, "maybe_consolidate_by_tokens",
                 _make_async_probe("memory.consolidate", "memory_consolidate",
                                   extract_in=_in, extract_out=lambda r, m: {}, nest=True),
                 expect_params=["session"])


# ── P1 probe: subagent.call (spawn lifecycle, two methods) ───────────────────


_subagent_pending: dict[str, dict] = {}


def _emit_subagent_root(info: dict, *, in_progress: bool, status: Any = "ok") -> None:
    """Root span of the subagent's OWN trace. The subagent's llm/tool/memory
    spans hang off this (via the turn_scope opened in run_factory), so they form
    a separate trace group instead of bloating the parent turn's tree. Carries a
    back-link (parent_trace_id + parent_span_id) so the viewer can offer
    "return to main trace". Emitted in-progress at start, re-emitted closed at
    end (viewer dedups by spanId) so the trace has a root the whole time."""
    if not info.get("sub_trace_id") or not info.get("sub_root_id"):
        return
    end = info["start"] if in_progress else spans.now_iso()
    is_err = (not in_progress) and str(status).lower() not in {"ok", "completed", "ended", "unknown"}
    attrs = {
        "subagent.task": _preview(info["task"], 300),
        "subagent.label": info["label"],
        "subagent.status": status,
        "subagent.in_progress": in_progress,
        "subagent.parent_trace_id": info["trace_id"],
        "subagent.parent_span_id": info["span_id"],
    }
    spans.emit(spans.build_span(
        "subagent.run", "subagent_call",
        trace_id=info["sub_trace_id"], span_id=info["sub_root_id"], parent_span_id=None,
        session_key=info["session_key"], channel=info["channel"], chat_id=info["chat_id"],
        start_time=info["start"], end_time=end,
        status_code="ERROR" if is_err else "OK", status_message=str(status) if is_err else "",
        attributes=attrs,
        events=[{"time": info["start"], "name": "subagent.run.start"}],
    ))


def _emit_subagent(info: dict, *, status: Any, result: Any) -> None:
    """The dispatch node — stays in the PARENT trace as a leaf. Forward-links to
    the subagent's own trace via ``subagent.trace_id`` so the viewer can jump."""
    meta = {"traceId": info["trace_id"], "sessionKey": info["session_key"]}
    art = spans.persist_artifact("subagent-result", meta, {"task": info["task"], "result": result}, label="subagent-result")
    is_err = str(status).lower() not in {"ok", "completed", "ended"}
    attrs = {
        "subagent.task": _preview(info["task"], 300),
        "subagent.label": info["label"],
        "subagent.status": status,
        "subagent.result_preview": _preview(result),
        "subagent.trace_id": info.get("sub_trace_id"),
        **spans.artifact_attributes("subagent.result", art),
    }
    spans.emit(spans.build_span(
        "subagent.call", "subagent_call",
        trace_id=info["trace_id"], span_id=info["span_id"], parent_span_id=info["parent"],
        session_key=info["session_key"], channel=info["channel"], chat_id=info["chat_id"],
        start_time=info["start"], end_time=spans.now_iso(),
        status_code="ERROR" if is_err else "OK", status_message=str(status) if is_err else "",
        attributes=attrs,
    ))


def _install_subagent(SubagentManager: type) -> bool:
    def run_factory(original):
        async def wrapper(self, task_id, task, label, origin, *args, **kwargs):
            tc = ctx.current()
            span_id = ctx.new_span_id()       # dispatch (subagent.call) — parent trace
            sub_root_id = ctx.new_span_id()   # root of the subagent's OWN trace
            base = {
                "span_id": span_id,
                "trace_id": tc.trace_id if tc else ctx.new_trace_id(),
                "parent": tc.parent_span_id if tc else None,
                "session_key": tc.session_key if tc else None,
                "channel": tc.channel if tc else None,
                "chat_id": tc.chat_id if tc else None,
                "start": spans.now_iso(),
                "task": task, "label": label,
                "sub_root_id": sub_root_id,
            }
            # Open the subagent's OWN trace so its llm/tool/memory spans form a
            # separate trace group rooted at sub_root_id — keeps the parent turn's
            # tree short. The dispatch node above is emitted into the PARENT trace.
            with ctx.turn_scope(
                session_key=base["session_key"], channel=base["channel"],
                chat_id=base["chat_id"], root_span_id=sub_root_id,
            ) as sub_tc:
                base["sub_trace_id"] = sub_tc.trace_id
                _subagent_pending[task_id] = base
                _emit_subagent_root(base, in_progress=True)
                try:
                    return await original(self, task_id, task, label, origin, *args, **kwargs)
                finally:
                    _emit_subagent_root(base, in_progress=False, status="ok")
                    info = _subagent_pending.pop(task_id, None)
                    if info is not None:  # announce didn't fire (cancelled / crashed)
                        _emit_subagent(info, status="unknown", result=None)
        return wrapper

    def announce_factory(original):
        async def wrapper(self, task_id, label, task, result, origin, status, *args, **kwargs):
            info = _subagent_pending.pop(task_id, None) or {
                "span_id": ctx.new_span_id(), "trace_id": ctx.new_trace_id(), "parent": None,
                "session_key": None, "channel": None, "chat_id": None,
                "start": spans.now_iso(), "task": task, "label": label,
                "sub_trace_id": None, "sub_root_id": None,
            }
            _emit_subagent(info, status=status, result=result)
            return await original(self, task_id, label, task, result, origin, status, *args, **kwargs)
        return wrapper

    ok1 = _wrap(SubagentManager, "_run_subagent", run_factory, expect_params=["task_id", "task", "label", "origin"])
    ok2 = _wrap(SubagentManager, "_announce_result", announce_factory, expect_params=["task_id", "status", "result"])
    return ok1 or ok2


# ── P1 probe: plugin.load (sync build_* on PluginRegistry) ───────────────────


def _install_plugin_load(PluginRegistry: type) -> bool:
    def make_factory(contribution: str):
        def factory(original):
            def wrapper(self, name, *args, **kwargs):
                span_id = ctx.new_span_id()
                trace_id = ctx.new_trace_id()
                start = spans.now_iso()
                status_code, status_msg, result = "OK", "", None
                try:
                    result = original(self, name, *args, **kwargs)
                    if result is None:
                        status_msg = "factory returned None (opt-out)"
                    return result
                except Exception as e:
                    status_code, status_msg = "ERROR", repr(e)
                    raise
                finally:
                    spans.emit(spans.build_span(
                        "plugin.load", "plugin_load",
                        trace_id=trace_id, span_id=span_id, parent_span_id=None,
                        start_time=start, end_time=spans.now_iso(),
                        status_code=status_code, status_message=status_msg,
                        attributes={
                            "plugin.contribution": contribution,
                            "plugin.name": name,
                            "plugin.result_type": type(result).__name__ if result is not None else None,
                        },
                    ))
            return wrapper
        return factory

    ok1 = _wrap_sync(PluginRegistry, "build_memory_backend", make_factory("memory_backend"), expect_params=["name"])
    ok2 = _wrap_sync(PluginRegistry, "build_tool", make_factory("tool"), expect_params=["name"])
    return ok1 or ok2


# ── P1 probe: skill.inject (skill BODY auto-injected into the system prompt) ─
#
# Two segment builders render skill bodies into the live prompt (NOT the
# ContextBuilder.build_system_prompt path — that one only runs for token-budget
# estimation and its output is discarded). We wrap the segment builds, which
# fire exactly once per turn each, and emit only when something was injected.


def _emit_skill_inject(*, via: str, names: list, ids: list, sources: dict, body_len: int, start: str) -> None:
    tc = ctx.current()
    trace_id = tc.trace_id if tc else ctx.new_trace_id()
    parent = tc.parent_span_id if tc else None
    sk = tc.session_key if tc else None
    channel = tc.channel if tc else None
    chat_id = tc.chat_id if tc else None
    meta = {"traceId": trace_id, "sessionKey": sk}
    art = spans.persist_artifact(
        "skill-inject", meta,
        {"via": via, "skills": [{"name": n, "id": i} for n, i in zip(names, ids)],
         "sources": sources, "body_len": body_len},
        label=f"skill-inject:{via}",
    )
    attrs = {
        "skill.inject.via": via,
        "skill.inject.count": len(ids),
        "skill.inject.names": names,
        "skill.inject.ids": ids,
        "skill.inject.sources": sources,
        "skill.inject.body_len": body_len,
        **spans.artifact_attributes("skill.inject", art),
    }
    spans.emit(spans.build_span(
        "skill.inject", "skills",
        trace_id=trace_id, span_id=ctx.new_span_id(), parent_span_id=parent,
        session_key=sk, channel=channel, chat_id=chat_id,
        start_time=start, end_time=spans.now_iso(),
        status_code="OK", status_message="",
        attributes=attrs,
        events=[{"time": start, "name": "skill.inject"}],
    ))


def _install_active_skill_inject(ActiveSkillsSegmentBuilder: type) -> bool:
    """``# Active Skills`` — ``always: true`` skills, force-injected every turn."""
    def factory(original):
        async def wrapper(self, *args, **kwargs):
            start = spans.now_iso()
            result = await original(self, *args, **kwargs)
            try:
                if result is not None and getattr(result, "text", ""):
                    metas = list(self._skills.get_always_skills() or [])
                    cfg = getattr(self._skills, "_config", None)
                    always_max = getattr(cfg, "always_max", 5) or 5
                    if always_max:
                        metas = metas[:always_max]
                    if metas:
                        _emit_skill_inject(
                            via="active_skills",
                            names=[getattr(m, "name", None) for m in metas],
                            ids=[str(getattr(m, "id", "")) for m in metas],
                            sources=dict(Counter((getattr(m, "source", None) or "?") for m in metas)),
                            body_len=len(result.text), start=start,
                        )
            except Exception:  # noqa: BLE001 — telemetry must never break assembly
                pass
            return result
        return wrapper

    return _wrap(ActiveSkillsSegmentBuilder, "build", factory, expect_params=["ctx"])


def _install_skills_segment_inject(SkillsSegmentBuilder: type) -> bool:
    """``# Skills`` — rewriter → router → gate selected skills' bodies.

    The build's returned ``Segment.meta`` already carries exactly what we want:
    ``injected_skill_ids`` (the gate-selected skills whose body was rendered)
    and ``skill_hits_by_source``.
    """
    def factory(original):
        async def wrapper(self, *args, **kwargs):
            start = spans.now_iso()
            result = await original(self, *args, **kwargs)
            try:
                seg_meta = (getattr(result, "meta", None) or {}) if result is not None else {}
                ids = list(seg_meta.get("injected_skill_ids") or [])
                if ids:
                    _emit_skill_inject(
                        via="skills_segment",
                        names=[str(i).split("/")[-1] for i in ids],
                        ids=ids,
                        sources=dict(seg_meta.get("skill_hits_by_source") or {}),
                        body_len=len(getattr(result, "text", "") or ""), start=start,
                    )
            except Exception:  # noqa: BLE001
                pass
            return result
        return wrapper

    return _wrap(SkillsSegmentBuilder, "build", factory, expect_params=["ctx"])


# ── install / uninstall ─────────────────────────────────────────────────────


def install() -> dict[str, bool]:
    """Install every probe (P0 + P1). Idempotent. Never raises."""
    global _done
    if _done:
        return summary()
    # P0 — full-fidelity main chain
    _probe("llm", "everclaw.providers.base", "LLMProvider", _install_llm)
    _probe("llm-stream", "everclaw.agent.loop.main", "AgentLoop", _install_llm_stream)
    _probe("tool", "everclaw.agent.tools.registry", "ToolRegistry", _install_tool)
    _probe("turn", "everclaw.agent.loop.main", "AgentLoop", _install_turn)
    # P1 — memory (6 nodes), subagent, plugin.load
    _probe("memory.recall", "everclaw.context_engine.segments.memory", "MemorySegmentBuilder", _install_memory_recall)
    _probe("memory.store+feedback", "everclaw.agent.loop.main", "AgentLoop", _install_agentloop_memory)
    _probe("memory.extract+profile", "everclaw.memory_engine.consolidate.consolidator", "MemoryStore", _install_memory_store_class)
    _probe("memory.consolidate", "everclaw.memory_engine.consolidate.consolidator", "MemoryConsolidator", _install_memory_consolidate)
    _probe("subagent", "everclaw.agent.subagent.manager", "SubagentManager", _install_subagent)
    _probe("plugin.load", "everclaw.plugin.registry", "PluginRegistry", _install_plugin_load)
    # P1 — skill injection (body rendered into the system prompt, no tool call)
    _probe("skill.inject.active", "everclaw.context_engine.segments.active_skills", "ActiveSkillsSegmentBuilder", _install_active_skill_inject)
    _probe("skill.inject.skills", "everclaw.context_engine.segments.skills", "SkillsSegmentBuilder", _install_skills_segment_inject)
    _done = True
    s = summary()
    logger.info("everclaw-tracing installed: %s", s)
    _emit_bootstrap(s)
    return s


def _probe(label: str, module: str, cls_name: str, installer: Callable[[type], bool]) -> None:
    try:
        mod = __import__(module, fromlist=[cls_name])
        installer(getattr(mod, cls_name))
    except Exception as e:  # noqa: BLE001
        logger.warning("everclaw-tracing: %s probe unavailable (%s): %s", label, module, e)


def summary() -> dict[str, bool]:
    return {k: v for k, v in _installed.items()}


def _emit_bootstrap(s: dict[str, bool]) -> None:
    try:
        start = spans.now_iso()
        spans.emit(spans.build_span(
            "tracing.bootstrap", "plugin_load",
            trace_id=ctx.new_trace_id(), span_id=ctx.new_span_id(), parent_span_id=None,
            start_time=start, end_time=start,
            attributes={"plugin.id": "everclaw-tracing", "plugin.probes": s},
        ))
    except Exception:  # noqa: BLE001
        pass


def uninstall() -> None:
    """Restore original methods (tests / clean teardown)."""
    global _done
    for target_cls, method_name, original in _originals.values():
        try:
            setattr(target_cls, method_name, original)
        except Exception:  # noqa: BLE001
            pass
    _originals.clear()
    _installed.clear()
    _done = False
