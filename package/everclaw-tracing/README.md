# everclaw-tracing

Pluggable, **non-invasive** tracing for [EverClaw](https://github.com/) — zero edits to
everclaw source. Install it and every turn is traced by **channel → session → turn**, with
spans for each LLM call, tool call, and skill use (`audit.span.v1`, the shared schema the
local viewer already renders).

## How it works (and the honest caveat)

EverClaw has no first-class observation hook today (the `EventBus` is unwired, the plugin
`ctx` exposes no bus/hooks). So this plugin uses **import-time auto-instrumentation** — the
same pattern OpenTelemetry uses: EverClaw's plugin discovery imports this package (to read
the bundled `everclaw-plugin.toml`), and on import we wrap a few stable choke points:

| span | wrapped method |
|---|---|
| `session.turn` | `AgentLoop._process_message` |
| `llm.call` | `LLMProvider.chat_with_retry` |
| `tool.call` / `skill.use` / `skill.read` | `ToolRegistry.execute` |

We patch **class** methods before any `AgentLoop` is built, so every instance is observed.
Each patch is **guarded**: if an everclaw refactor changes a signature, that one probe
disables itself with a warning — it never crashes the agent, and the others keep working.

> Caveat: monkeypatching internal methods is sensitive to everclaw internals. This is
> deliberate (it's the only zero-core-edit path today). The longer-term fix is a tiny
> upstream PR adding a stable observation seam; this plugin can then prefer it.

## Install

```bash
pip install everclaw-tracing          # into the same env as everclaw
# or, from source:
pip install -e /path/to/everclaw-tracing
```

That's it — `enabled_by_default = true`, discovered via the `everclaw.plugins` entry point.
Run any everclaw command; traces land at `~/.everclaw/traces/logs/audit-spans.log`.

> Note: the auto-instrumentation fires only on the **pip/entry-point** install path (that's
> what triggers the package import). Copying files into `~/.everclaw/plugins/` does not.

## Configure

| env var | default | meaning |
|---|---|---|
| `EVERCLAW_TRACING` | `1` | set `0`/`off` to disable |
| `EVERCLAW_TRACING_DIR` | `~/.everclaw/traces` | where spans/artifacts are written |
| `EVERCLAW_TRACING_PREVIEW` | `500` | inline preview length; full payloads go to artifacts |

## View

Reuses the shared tracing-plugin viewer:

```bash
tracing everclaw    # opens the browser, grouped by channel → session → turn
```

## Scope

- **Main chain:** `session.turn`, `llm.call` (full usage/cost, streaming + non-streaming),
  `tool.call`, `skill.use` / `skill.read` / `skill.inject` — full fidelity.
- **Extended (also live):** `memory.recall/store/extract/consolidate/profile_refresh/feedback`,
  `subagent.call` (emitted as its own trace, back-linked to the parent), `plugin.load`.

See `../EVERCLAW_AUTOTRACE_DESIGN.md` for the full design and per-node I/O definitions.

## Test (offline, no everclaw / no API cost)

```bash
python3 tests/test_p0_instrument.py
```

Drives a fake turn (LLM + tool + skill) through the real probes and asserts the span tree.
