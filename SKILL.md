---
name: everclaw-tracing-setup
description: Install + view tracing (observability) for an EverClaw install. Use when the user wants to trace, observe, debug, or audit everclaw — capturing each conversation turn's LLM calls (incl. streaming), tool calls, skill use, memory ops, and subagent activity as audit.span.v1 spans, viewable in a local browser panel. ZERO edits to everclaw's source. Both the pip plugin and the viewer are bundled inside this skill (./package, ./viewer).
---

# Install + view EverClaw tracing

This skill installs **everclaw-tracing** (a zero-core-edit observability plugin) and ships a local
**viewer**. Once installed into the same Python env as everclaw, every conversation turn is traced,
grouped by **channel → session → turn**, with these span nodes:
`session.turn` · `llm.call` (provider/model/tokens/cache/cost; **streaming and non-streaming**) ·
`tool.call` · `skill.use`/`skill.read`/`skill.inject` · `memory.recall/store/extract/consolidate/profile_refresh/feedback` ·
`subagent.call` · `plugin.load`.

Bundled in this skill folder:
- **`./package/everclaw-tracing`** — the pip plugin (the trace producer).
- **`./viewer`** — a dependency-free Node viewer (the browser panel).

## How loading works (read this — it drives the steps)

everclaw's `everclaw tui` command does NOT run plugin discovery, so an entry-point-only plugin would
miss TUI turns. To cover **every** command (tui / gateway / agent / cron), the package ships a
`.pth` file that imports it at Python-interpreter startup (the OpenTelemetry / coverage.py pattern).
A pip install drops that `.pth` into site-packages, so any everclaw process in that venv auto-loads
the plugin and installs its probes before the agent runs. **Consequence: a long-running everclaw
process started BEFORE install is not instrumented — it must be restarted.**

---

## Procedure

### Step 1 — Find the Python env that runs everclaw
Install into the **same** env as everclaw, or it does nothing (silent no-op).
- uv project (`cd <repo> && uv run everclaw ...`): the repo's `.venv`; install with `uv pip install`.
- global CLI (`everclaw ...`): `head -1 "$(command -v everclaw)"` shows the interpreter; install with `<that-python> -m pip install`.
- Unsure → ask the user. Confirm: `<target-python> -c "import everclaw; print(everclaw.__version__)"`.

### Step 2 — Install the bundled plugin into that env
```bash
# uv project:
cd <everclaw_repo> && uv pip install "<abs-path-to-skill>/package/everclaw-tracing"
# global / plain pip:
"<target-python>" -m pip install "<abs-path-to-skill>/package/everclaw-tracing"
```

### Step 3 — Restart any RUNNING everclaw process
If a `everclaw tui` / gateway / etc. is already running, it predates the install and is NOT traced.
Tell the user to restart it (e.g. quit the TUI and `uv run everclaw tui` again). New turns will trace.

### Step 4 — Verify (cost-free, no LLM call)
1. Entry point registered: `<target-python> -c "from importlib.metadata import entry_points; print([e.name for e in entry_points(group='everclaw.plugins')])"` → includes `everclaw_tracing`.
2. `.pth` placed: confirm `everclaw_tracing_autoload.pth` exists in the env's `site-packages`.
3. Probes attach (any venv python autoloads + writes a `tracing.bootstrap` span):
   ```bash
   <target-python> - <<'PY'
   import json, pathlib
   log = pathlib.Path.home()/".everclaw"/"traces"/"logs"/"audit-spans.log"
   boot = [json.loads(l) for l in log.read_text().splitlines() if l.strip() and '"tracing.bootstrap"' in l]
   probes = boot[-1]["attributes"]["plugin.probes"] if boot else {}
   ok = sum(1 for v in probes.values() if v)
   print(f"probes bound: {ok}/16", "OK" if ok >= 15 else "(some disabled — version drift, see Troubleshooting)")
   PY
   ```
   Expect `probes bound: 16/16`.

### Step 5 — View the traces
Run any everclaw turn (tui / agent / gateway), then launch the bundled viewer:
```bash
node "<abs-path-to-skill>/viewer/server.js" --framework everclaw
# then open http://127.0.0.1:4318 in a browser
```
Grouped by channel → session → turn; click a turn to see each node's input/output, tokens, cost.
(Needs Node ≥18 — present on any machine running the everclaw TUI. The viewer has no npm deps.)
Note: the viewer reads on page load and does NOT auto-poll — refresh the browser after new turns.

### Step 6 — Tell the user
- Traces: `~/.everclaw/traces/logs/audit-spans.log` (JSONL; large payloads under `audit-artifacts/`).
- Off switch: `EVERCLAW_TRACING=0` in the env, or `pip uninstall everclaw-tracing`.

---

## Caveats (state these)

1. **Same env only** — installing into a different Python env than everclaw's is a silent no-op.
2. **Restart long-running processes** — a TUI/gateway started before install isn't traced until restarted (Step 3).
3. **`uv sync` / `ec.sh update` prunes it** — if everclaw is a uv project, `uv sync` removes packages
   not in everclaw's lockfile, including this one. Reinstall after such an update, or use `uv sync --inexact`.
4. **Version-drift** — it monkeypatches everclaw internals (verified against everclaw 0.1.0,
   branch `refactor/EverClaw`). If a future refactor changes a patched method's signature, that one
   probe self-disables with a warning (agent unaffected, others keep working). The `tracing.bootstrap`
   span's `plugin.probes` map shows which are live.

## Troubleshooting
- **No spans / not listed in `everclaw plugins`** → wrong env (Step 1), or pruned by `uv sync` (caveat 3).
- **Only a small `llm.call` (the SkillForge rewriter) shows, no main model call** → the plugin predates
  the streaming-capture fix, or the running process wasn't restarted (Step 3). Reinstall + restart.
- **`probes bound: N/16` with N<16** → version drift (caveat 4); bound probes still trace.
- **Viewer panel empty / stale** → refresh the browser (no auto-poll); confirm a turn actually ran.

## How it works (for the curious)
A pip install (a) registers the `everclaw.plugins` entry point (so `everclaw plugins` lists it) and
(b) drops `everclaw_tracing_autoload.pth` into site-packages. The `.pth` runs `import everclaw_tracing`
at interpreter startup, which calls `instrument.install()` — wrapping class methods
(`LLMProvider.chat_with_retry`, `AgentLoop._llm_call_stream` for streaming, `ToolRegistry.execute`,
`_process_message`, and the memory/subagent/plugin choke points) before any AgentLoop is built. It
contributes no memory backend or tool — it's a pure observer. Spans go to a stdlib JSONL store.
Full design + per-node I/O is in `./package/everclaw-tracing/README.md`.
