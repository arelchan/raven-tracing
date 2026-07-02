"""Offline P0+P1 verification — no real Raven, no API calls (cost-free).

Fake classes mirror raven's real method signatures (verified against source).
We install every probe onto them, drive one full turn that exercises
LLM / tool / skill / memory(×6) / subagent, plus a boot-time plugin.load, then
assert the emitted audit.span.v1 spans form the right tree with full I/O and
correct nesting (subagent child LLM under subagent.call; extract under
consolidate).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── fakes mirroring raven's real signatures ──────────────────────────────


@dataclass
class FakeToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class FakeLLMResponse:
    content: str | None
    tool_calls: list = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    reasoning_content: str | None = None


@dataclass
class FakeInbound:
    channel: str
    chat_id: str
    content: str
    media: list = field(default_factory=list)

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"


@dataclass
class FakeOutbound:
    channel: str
    chat_id: str
    content: str


@dataclass
class FakeMemory:
    text: str
    score: float = 0.5
    metadata: dict = field(default_factory=dict)


@dataclass
class FakeSession:
    key: str
    last_consolidated: int = 0
    messages: list = field(default_factory=list)


class FakeProvider:
    default_model = "fake/model-1"

    def __init__(self, scripted):
        self._scripted = list(scripted)

    # mirrors LLMProvider.chat_with_retry
    async def chat_with_retry(self, messages, tools=None, model=None,
                              max_tokens=None, temperature=None, reasoning_effort=None,
                              tool_choice=None, fallback_models=None):
        return self._scripted.pop(0)


class FakeRegistry:
    # mirrors ToolRegistry.execute
    async def execute(self, name: str, params: dict) -> str:
        if name == "read_skill":
            return f"## Weather Skill\nGet weather for a city.\n(params={params})"
        return f"ok:{name}:{json.dumps(params)}"


class FakeMemSegment:
    # mirrors MemorySegmentBuilder
    def __init__(self):
        self._user_id = "default"
        self._memory_top_k = 5

    async def _recall(self, query):
        return [FakeMemory(text=f"recalled:{query}", metadata={"type": "episode", "owner_type": "user"})]


class FakeMemStore:
    # mirrors MemoryStore.annotate / maybe_refresh_hot_tags
    async def annotate(self, messages, provider, model, *, enable_foresight=False):
        return True

    async def maybe_refresh_hot_tags(self, provider, model, *, threshold):
        return 2


class FakeConsolidator:
    # mirrors MemoryConsolidator.maybe_consolidate_by_tokens
    def __init__(self, store):
        self._store = store

    async def maybe_consolidate_by_tokens(self, session):
        # triggers extract — should nest UNDER the consolidate span (nest=True)
        await self._store.annotate([{"role": "user"}], None, "fake/model-1")


class FakeSubMgr:
    # mirrors SubagentManager._run_subagent / _announce_result
    def __init__(self, provider):
        self.provider = provider

    async def _run_subagent(self, task_id, task, label, origin):
        # child LLM call — should nest UNDER subagent.call
        await self.provider.chat_with_retry(messages=[{"role": "user", "content": task}], model="fake/sub")
        await self._announce_result(task_id, label, task, "child result", origin, "ok")

    async def _announce_result(self, task_id, label, task, result, origin, status):
        return None


class FakeRegistry2:
    # mirrors PluginRegistry.build_memory_backend / build_tool (SYNC)
    def build_memory_backend(self, name, *, config, services, logger=None):
        return object()

    def build_tool(self, name, *, config, services, logger=None):
        return None  # legal opt-out


class FakeLoop:
    # mirrors AgentLoop._process_message + the after-turn dispatch methods
    def __init__(self, provider, tools, mem_segment, submgr, consolidator, memstore):
        self.provider = provider
        self.tools = tools
        self.mem_segment = mem_segment
        self.submgr = submgr
        self.consolidator = consolidator
        self.memstore = memstore

    async def _dispatch_backend_store(self, session_key, messages_slice):
        return None

    async def _dispatch_backend_feedback(self, session_key, injected_skill_ids, used_skill_ids=None):
        return None

    async def _process_message(self, msg, session_key=None, on_progress=None,
                               on_token_delta=None, on_reasoning_delta=None,
                               on_tool_event=None, usage_sink=None):
        sk = session_key or msg.session_key
        # context assembly → recall
        await self.mem_segment._recall(msg.content)
        # ReAct iteration 1: model asks for a tool + a skill
        r1 = await self.provider.chat_with_retry(
            messages=[{"role": "user", "content": msg.content}], tools=[{"x": 1}], model="fake/model-1")
        for tc in r1.tool_calls:
            await self.tools.execute(tc.name, tc.arguments)
        # subagent (in real life via spawn tool + bg task; here driven inline)
        await self.submgr._run_subagent("ab12cd34", "research the topic", "research",
                                        {"channel": msg.channel, "chat_id": msg.chat_id})
        # ReAct iteration 2: final answer
        r2 = await self.provider.chat_with_retry(
            messages=[{"role": "user", "content": msg.content}], model="fake/model-1")
        # after-turn pipeline
        await self._dispatch_backend_store(sk, [{"role": "user", "content": msg.content},
                                                {"role": "assistant", "content": r2.content}])
        await self._dispatch_backend_feedback(sk, ["everos/x"], ["everos/x"])
        await self.consolidator.maybe_consolidate_by_tokens(FakeSession(key=sk, messages=[1, 2, 3]))
        await self.memstore.maybe_refresh_hot_tags(None, "fake/model-1", threshold=5)
        return FakeOutbound(channel=msg.channel, chat_id=msg.chat_id, content=r2.content)


# ── the test ────────────────────────────────────────────────────────────────


def _read_spans(state_dir: Path) -> list[dict]:
    log = state_dir / "logs" / "audit-spans.log"
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def run() -> None:
    tmp = tempfile.mkdtemp(prefix="ectrace-")
    os.environ["RAVEN_TRACING_DIR"] = tmp
    os.environ["RAVEN_TRACING"] = "1"

    for m in list(sys.modules):
        if m.startswith("raven_tracing"):
            del sys.modules[m]
    from raven_tracing import instrument

    # install P0 + P1 probes onto the fakes
    instrument._install_llm(FakeProvider)
    instrument._install_tool(FakeRegistry)
    instrument._install_turn(FakeLoop)
    instrument._install_memory_recall(FakeMemSegment)
    instrument._install_agentloop_memory(FakeLoop)
    instrument._install_memory_store_class(FakeMemStore)
    instrument._install_memory_consolidate(FakeConsolidator)
    instrument._install_subagent(FakeSubMgr)
    instrument._install_plugin_load(FakeRegistry2)

    # boot: plugin.load (orphan, before any turn)
    reg = FakeRegistry2()
    reg.build_memory_backend("everos", config={}, services=None)
    reg.build_tool("understand_media", config={}, services=None)

    main_provider = FakeProvider([
        FakeLLMResponse(
            content="let me check",
            tool_calls=[
                FakeToolCall(id="t1", name="exec", arguments={"cmd": "ls"}),
                FakeToolCall(id="t2", name="read_skill", arguments={"skill_id": "hub/weather"}),
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 1200, "completion_tokens": 40,
                   "cache_read_input_tokens": 1000, "total_tokens": 1240},
        ),
        FakeLLMResponse(content="done", finish_reason="stop",
                        usage={"prompt_tokens": 1300, "completion_tokens": 10, "total_tokens": 1310}),
    ])
    sub_provider = FakeProvider([
        FakeLLMResponse(content="sub done", finish_reason="stop",
                        usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55}),
    ])
    store = FakeMemStore()
    loop = FakeLoop(main_provider, FakeRegistry(), FakeMemSegment(),
                    FakeSubMgr(sub_provider), FakeConsolidator(store), store)
    msg = FakeInbound(channel="cli", chat_id="20260625_120000_abc123", content="weather in NYC?")

    out = asyncio.run(loop._process_message(msg, session_key="cli:20260625_120000_abc123"))
    assert out.content == "done", out

    spans = _read_spans(Path(tmp))
    by_name: dict[str, list[dict]] = {}
    for s in spans:
        by_name.setdefault(s["name"], []).append(s)

    def names_count():
        return sorted((k, len(v)) for k, v in by_name.items())

    # ---- structure: one turn, the right node set ----
    assert len(by_name.get("session.turn", [])) == 1, names_count()
    turn = by_name["session.turn"][0]
    root_id, trace_id = turn["spanId"], turn["traceId"]
    assert turn["parentSpanId"] is None
    assert turn["attributes"]["channel"] == "cli"
    assert turn["attributes"]["turn.input_preview"] == "weather in NYC?"
    assert turn["attributes"]["turn.output_preview"] == "done"

    for nm in ["llm.call", "tool.call", "skill.read", "memory.recall", "memory.store",
               "memory.feedback", "memory.extract", "memory.consolidate",
               "memory.profile_refresh", "subagent.call", "plugin.load"]:
        assert nm in by_name, f"missing span: {nm}; got {list(by_name)}"

    # ---- LLM: 3 calls (2 main + 1 sub), usage normalized ----
    assert len(by_name["llm.call"]) == 3, names_count()
    main_llms = [s for s in by_name["llm.call"] if s["parentSpanId"] == root_id]
    assert len(main_llms) == 2, "2 main LLM calls under the turn root"
    first = sorted(main_llms, key=lambda s: s["startTime"])[0]["attributes"]
    assert first["llm.provider"] == "FakeProvider"
    assert first["llm.usage.input_tokens"] == 200    # 1200 - 1000 cache
    assert first["llm.usage.cache_read_tokens"] == 1000
    assert first["llm.tool_call_count"] == 2

    # ---- tool + skill ----
    tool = by_name["tool.call"][0]["attributes"]
    assert tool["tool.name"] == "exec" and isinstance(tool["tool.duration_ms"], int)
    skill = by_name["skill.read"][0]["attributes"]
    assert skill["skill.id"] == "hub/weather" and skill["skill.source"] == "hub"
    assert skill["skill.name"] == "Weather Skill"

    # ---- memory nodes hang off the turn, with their I/O ----
    rec = by_name["memory.recall"][0]
    assert rec["parentSpanId"] == root_id and rec["traceId"] == trace_id
    assert rec["attributes"]["memory.hits"] == 1
    assert rec["attributes"]["memory.top_k"] == 5
    assert by_name["memory.store"][0]["attributes"]["memory.message_count"] == 2
    assert by_name["memory.feedback"][0]["attributes"]["memory.injected"] == ["everos/x"]
    assert by_name["memory.profile_refresh"][0]["attributes"]["memory.sections_rewritten"] == 2

    # ---- nesting: extract under consolidate ----
    consolidate = by_name["memory.consolidate"][0]
    extract = by_name["memory.extract"][0]
    assert extract["parentSpanId"] == consolidate["spanId"], "extract must nest under consolidate"
    assert extract["attributes"]["memory.surface"] == "host"

    # ---- nesting: subagent child LLM under subagent.call ----
    sub = by_name["subagent.call"][0]
    assert sub["attributes"]["subagent.status"] == "ok"
    assert sub["traceId"] == trace_id, "subagent shares the turn's trace"
    sub_llms = [s for s in by_name["llm.call"] if s["parentSpanId"] == sub["spanId"]]
    assert len(sub_llms) == 1 and sub_llms[0]["attributes"]["llm.output_preview"] == "sub done", \
        "the subagent's own LLM call must nest under subagent.call"

    # ---- plugin.load: orphan boot spans, distinct trace ----
    pls = by_name["plugin.load"]
    assert len(pls) == 2
    assert {p["attributes"]["plugin.contribution"] for p in pls} == {"memory_backend", "tool"}
    assert all(p["parentSpanId"] is None and p["traceId"] != trace_id for p in pls)
    opt_out = next(p for p in pls if p["attributes"]["plugin.contribution"] == "tool")
    assert opt_out["attributes"]["plugin.result_type"] is None  # build_tool returned None

    instrument.uninstall()
    print("OK: P0+P1 instrumentation — correct channel/session/turn tree + nesting")
    print(f"  spans emitted: {names_count()}")
    print(f"  trace dir: {tmp}")


if __name__ == "__main__":
    run()
