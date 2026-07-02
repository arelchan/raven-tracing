# raven-tracing

Pluggable, **non-invasive** tracing for [Raven](https://github.com/) — zero edits to
raven source. Install it and every turn is traced by **channel → session → turn**, with
spans for each LLM call, tool call, and skill use (`audit.span.v1`, the shared schema the
local viewer already renders).

## How it works (and the honest caveat)

Raven has no first-class observation hook today (the `EventBus` is unwired, the plugin
`ctx` exposes no bus/hooks). So this plugin uses **import-time auto-instrumentation** — the
same pattern OpenTelemetry uses: Raven's plugin discovery imports this package (to read
the bundled `raven-plugin.toml`), and on import we wrap a few stable choke points:

| span | wrapped method |
|---|---|
| `session.turn` | `AgentLoop._process_message` |
| `llm.call` | `LLMProvider.chat_with_retry` |
| `tool.call` / `skill.use` / `skill.read` | `ToolRegistry.execute` |

We patch **class** methods before any `AgentLoop` is built, so every instance is observed.
Each patch is **guarded**: if an raven refactor changes a signature, that one probe
disables itself with a warning — it never crashes the agent, and the others keep working.

> Caveat: monkeypatching internal methods is sensitive to raven internals. This is
> deliberate (it's the only zero-core-edit path today). The longer-term fix is a tiny
> upstream PR adding a stable observation seam; this plugin can then prefer it.

## Install

```bash
pip install raven-tracing          # into the same env as raven
# or, from source:
pip install -e /path/to/raven-tracing
```

That's it — `enabled_by_default = true`, discovered via the `raven.plugins` entry point.
Run any raven command; traces land at `~/.raven/traces/logs/audit-spans.log`.

> Note: the auto-instrumentation fires only on the **pip/entry-point** install path (that's
> what triggers the package import). Copying files into `~/.raven/plugins/` does not.

## Configure

| env var | default | meaning |
|---|---|---|
| `RAVEN_TRACING` | `1` | set `0`/`off` to disable |
| `RAVEN_TRACING_DIR` | `~/.raven/traces` | where spans/artifacts are written |
| `RAVEN_TRACING_PREVIEW` | `500` | inline preview length; full payloads go to artifacts |

## View

Reuses the shared tracing-plugin viewer:

```bash
tracing raven    # opens the browser, grouped by channel → session → turn
```

## Scope

- **Main chain:** `session.turn`, `llm.call` (full usage/cost, streaming + non-streaming),
  `tool.call`, `skill.use` / `skill.read` / `skill.inject` — full fidelity.
- **Extended (also live):** `memory.recall/store/extract/consolidate/profile_refresh/feedback`,
  `subagent.call` (emitted as its own trace, back-linked to the parent), `plugin.load`.

See `../RAVEN_AUTOTRACE_DESIGN.md` for the full design and per-node I/O definitions.

## Test (offline, no raven / no API cost)

```bash
python3 tests/test_p0_instrument.py
```

Drives a fake turn (LLM + tool + skill) through the real probes and asserts the span tree.
