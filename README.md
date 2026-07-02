# raven-tracing

Pluggable, **non-invasive** tracing for [Raven](https://github.com/) — **zero edits to raven's
source**. Install it and every conversation turn is traced, grouped by **channel → session → turn**,
with spans for each LLM call (incl. streaming), tool call, skill use, memory op, subagent, and plugin
load — viewable in a bundled local browser panel.

It auto-instruments at Python-interpreter startup (the OpenTelemetry / coverage.py pattern): once
pip-installed into the same env as raven, a shipped `.pth` imports it and it wraps a few stable
internal methods before the agent runs. It contributes no memory backend or tool — it's a pure observer.

## Quick start — let Claude Code do it

Clone this repo, then in a Claude Code session say:

> Install raven tracing for me — follow `SKILL.md` in this repo.

Claude Code reads [`SKILL.md`](SKILL.md) and: detects the Python env that runs your raven,
pip-installs the bundled package, verifies it, and tells you how to view traces.

## Quick start — manual

```bash
# 1. Install into the SAME Python env that runs raven:
uv pip install ./package/raven-tracing          # uv project
#   or:  <raven-python> -m pip install ./package/raven-tracing

# 2. Restart any running raven process (TUI / gateway) so it loads the plugin.

# 3. Run a turn (uv run raven tui / agent / ...), then view:
node ./viewer/server.js --framework raven       # then open http://127.0.0.1:4318

# Off switch:
RAVEN_TRACING=0    # env var, or:  pip uninstall raven-tracing
```

Traces are JSONL at `~/.raven/traces/logs/audit-spans.log` (large payloads under `audit-artifacts/`).

## What's in here

| Path | What |
|---|---|
| `package/raven-tracing/` | the pip plugin (the trace producer) |
| `viewer/` | dependency-free Node browser panel (needs Node ≥18) |
| `SKILL.md` | install instructions Claude Code follows |

## Caveats

- **Same env only** — install into a different Python env than raven's is a silent no-op.
- **Restart long-running processes** — a TUI/gateway started before install isn't traced until restarted.
- **`uv sync` / `ec.sh update` prunes it** — reinstall after such an update, or use `uv sync --inexact`.
- **Version-drift** — it monkeypatches raven internals (verified against raven 0.1.0,
  branch `refactor/Raven`). If a future refactor changes a patched method, that one probe
  self-disables with a warning (agent unaffected); check the `tracing.bootstrap` span's
  `plugin.probes` map to see which are live.

## License

MIT — see [LICENSE](LICENSE).
