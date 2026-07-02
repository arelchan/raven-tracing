"""Isolated REAL-raven turn with a STUB provider — zero API credit, zero pollution.

Runs raven's REAL AgentLoop._process_message / ToolRegistry.execute / context
assembly with our probes active (loaded via PYTHONPATH, NOT installed into the
raven venv), an isolated workspace, and traces to an isolated dir. Proves the
probes fire on LIVE raven code and build a correct span tree — without any
network/LLM call.

Run (nothing is written into the raven repo or its venv) — from an raven
checkout that has a usable .venv, with this package's dir on PYTHONPATH:

  cd <raven-repo>
  RAVEN_TRACING_DIR=/tmp/ectrace-real \
  PYTHONPATH=<path-to-this-package> \
  .venv/bin/python <path-to-this-package>/tests/real_turn_isolated.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import raven_tracing  # noqa: F401 — import installs the probes
from raven_tracing import instrument

from raven.agent.loop import AgentLoop
from raven.bus.events import InboundMessage
from raven.bus.queue import MessageBus
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class StubProvider(LLMProvider):
    """Scripted provider: ask for one real tool, then answer. No API calls."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self._n = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        self._n += 1
        if self._n == 1:
            return LLMResponse(
                content="", finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="t1", name="list_dir", arguments={"path": "."})],
                usage={"prompt_tokens": 1200, "completion_tokens": 30,
                       "cache_read_input_tokens": 1000, "total_tokens": 1230},
            )
        return LLMResponse(content="Here is the workspace listing.", finish_reason="stop",
                           usage={"prompt_tokens": 1300, "completion_tokens": 12, "total_tokens": 1312})

    def get_default_model(self) -> str:
        return "stub"


async def main() -> None:
    ws = Path(tempfile.mkdtemp(prefix="ecws-"))
    (ws / "hello.txt").write_text("hi", encoding="utf-8")
    agent = AgentLoop(
        bus=MessageBus(), provider=StubProvider(), workspace=ws,
        model="stub", max_iterations=3, restrict_to_workspace=True,
    )
    out = await agent._process_message(
        InboundMessage(channel="cli", sender_id="u", chat_id="c1", content="list the workspace"),
        session_key="cli:c1",
    )
    print("probes bound:", sum(1 for v in instrument.summary().values() if v), "/ 13")
    print("turn output:", getattr(out, "content", out))


asyncio.run(main())


# ── render the emitted span tree ────────────────────────────────────────────
sd = Path(os.environ["RAVEN_TRACING_DIR"]) / "logs" / "audit-spans.log"
spans = [json.loads(line) for line in sd.read_text().splitlines() if line.strip()]
ids = {s["spanId"] for s in spans}


def show(s: dict, depth: int = 0) -> None:
    a = s.get("attributes", {})
    extra = ""
    if s["name"] == "llm.call":
        extra = f"  [{a.get('llm.usage.input_tokens')}in/{a.get('llm.usage.output_tokens')}out, {a.get('llm.provider')}]"
    elif s["name"] == "tool.call":
        extra = f"  [{a.get('tool.name')} {a.get('tool.duration_ms')}ms]"
    elif s["name"] == "session.turn":
        extra = f"  [{a.get('channel')} | in={a.get('turn.input_preview')!r} out={a.get('turn.output_preview')!r}]"
    print("  " * depth + f"- {s['name']} ({a.get('span.type')}) {s['status']['code']}{extra}")
    for c in sorted([x for x in spans if x.get("parentSpanId") == s["spanId"]], key=lambda x: x["startTime"]):
        show(c, depth + 1)


roots = [s for s in spans if not s.get("parentSpanId") or s["parentSpanId"] not in ids]
print(f"\nspan tree ({len(spans)} spans @ {sd}):")
for r in sorted(roots, key=lambda x: x["startTime"]):
    show(r)
