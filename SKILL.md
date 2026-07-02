---
name: raven-tracing-setup
description: Install + view tracing (observability) for an Raven install. Use when the user wants to trace, observe, debug, or audit raven — capturing each conversation turn's LLM calls (incl. streaming), tool calls, skill use, memory ops, and subagent activity as audit.span.v1 spans, viewable in a local browser panel. ZERO edits to raven's source. Both the pip plugin and the viewer are bundled inside this skill (./package, ./viewer).
---

# Install + view Raven tracing

This skill installs **raven-tracing** (a zero-core-edit observability plugin) and ships a local
**viewer**. Once installed into the same Python env as raven, every conversation turn is traced,
grouped by **channel → session → turn**, with these span nodes:
`session.turn` · `llm.call` (provider/model/tokens/cache/cost; **streaming and non-streaming**) ·
`tool.call` · `skill.use`/`skill.read`/`skill.inject` · `memory.recall/store/extract/consolidate/profile_refresh/feedback` ·
`subagent.call` · `plugin.load`.

Bundled in this skill folder:
- **`./package/raven-tracing`** — the pip plugin (the trace producer).
- **`./viewer`** — a dependency-free Node viewer (the browser panel).

## How loading works (read this — it drives the steps)

raven's `raven tui` command does NOT run plugin discovery, so an entry-point-only plugin would
miss TUI turns. To cover **every** command (tui / gateway / agent / cron), the package ships a
`.pth` file that imports it at Python-interpreter startup (the OpenTelemetry / coverage.py pattern).
A pip install drops that `.pth` into site-packages, so any raven process in that venv auto-loads
the plugin and installs its probes before the agent runs. **Consequence: a long-running raven
process started BEFORE install is not instrumented — it must be restarted.**

---

## Procedure

### Step 1 — Find the Python env that runs raven
Install into the **same** env as raven, or it does nothing (silent no-op).
- uv project (`cd <repo> && uv run raven ...`): the repo's `.venv`; install with `uv pip install`.
- global CLI (`raven ...`): `head -1 "$(command -v raven)"` shows the interpreter; install with `<that-python> -m pip install`.
- Unsure → ask the user. Confirm: `<target-python> -c "import raven; print(raven.__version__)"`.

### Step 2 — Install the bundled plugin into that env
```bash
# uv project:
cd <raven_repo> && uv pip install "<abs-path-to-skill>/package/raven-tracing"
# global / plain pip:
"<target-python>" -m pip install "<abs-path-to-skill>/package/raven-tracing"
```

### Step 3 — Restart any RUNNING raven process
If a `raven tui` / gateway / etc. is already running, it predates the install and is NOT traced.
Tell the user to restart it (e.g. quit the TUI and `uv run raven tui` again). New turns will trace.

### Step 4 — Verify (cost-free, no LLM call)
1. Entry point registered: `<target-python> -c "from importlib.metadata import entry_points; print([e.name for e in entry_points(group='raven.plugins')])"` → includes `raven_tracing`.
2. `.pth` placed: confirm `raven_tracing_autoload.pth` exists in the env's `site-packages`.
3. Probes attach (any venv python autoloads + writes a `tracing.bootstrap` span):
   ```bash
   <target-python> - <<'PY'
   import json, pathlib
   log = pathlib.Path.home()/".raven"/"traces"/"logs"/"audit-spans.log"
   boot = [json.loads(l) for l in log.read_text().splitlines() if l.strip() and '"tracing.bootstrap"' in l]
   probes = boot[-1]["attributes"]["plugin.probes"] if boot else {}
   ok = sum(1 for v in probes.values() if v)
   disabled = [k for k, v in probes.items() if not v]
   print(f"probes bound: {ok}/{len(probes)}" if probes
         else "no bootstrap span yet — run any raven command, then re-check")
   if disabled: print("DISABLED (version drift):", disabled, "-> self-heal in Step 4b")
   PY
   ```
   Expect `probes bound: 16/16`. If any probe is **DISABLED**, run **Step 4b** to re-align it.

### Step 4b — Self-heal if any probe is DISABLED (version drift)
This plugin monkeypatches raven internals, so a probe disables itself (never crashes) when
this user's raven renamed or reshaped a method it wraps. **You are Claude Code — repair it
in place** rather than shipping degraded traces. For each `<Class>.<method>` from Step 4's
DISABLED list:
1. Find its probe in `package/raven-tracing/raven_tracing/instrument.py`: search
   `_wrap(<Class>, "<method>"` (or `_wrap_sync`). Note its `expect_params=[...]` and what the
   wrapper reads off the call args.
2. Inspect this raven's ACTUAL signature (the disabled log line names the module; or grep
   raven for `class <Class>`):
   `"<target-python>" -c "import inspect; from <module> import <Class>; print(inspect.signature(<Class>.<method>))"`
3. Reconcile — wrappers bind **positionally**, so a renamed positional arg only trips the *guard*:
   - guard names a param that no longer exists → set `expect_params` to a param that IS still
     present (prefer a stable one, e.g. `session_key`); the positional arg's name is irrelevant.
   - the wrapper reads a moved attribute (e.g. `msg.content` → `req.text`) → read the new one
     with a fallback: `getattr(x, "text", None) or getattr(x, "content", None)`.
   - the method was renamed/removed → grep raven for the method now playing that role and
     repoint the `_wrap` target.
4. Reinstall (Step 2) + restart raven (Step 3) + re-run Step 4. Repeat until `16/16` — or, if a
   choke point genuinely no longer exists in this raven, report which span is unavailable
   instead of forcing it.
> Worked example already in this code: the `AgentLoop._process_message` probe was re-aligned after
> raven renamed its first arg `msg` → `req: TurnRequest` (guard relaxed to `["session_key"]`,
> input read as `.text` with a `.content` fallback). Same move applies to any other drift.

### Step 5 — View the traces
Run any raven turn (tui / agent / gateway), then launch the bundled viewer:
```bash
node "<abs-path-to-skill>/viewer/server.js" --framework raven
# then open http://127.0.0.1:4318 in a browser
```
Grouped by channel → session → turn; click a turn to see each node's input/output, tokens, cost.
(Needs Node ≥18 — present on any machine running the raven TUI. The viewer has no npm deps.)
Note: the viewer reads on page load and does NOT auto-poll — refresh the browser after new turns.

### Step 6 — Tell the user
- Traces: `~/.raven/traces/logs/audit-spans.log` (JSONL; large payloads under `audit-artifacts/`).
- Off switch: `RAVEN_TRACING=0` in the env, or `pip uninstall raven-tracing`.

---

## Caveats (state these)

1. **Same env only** — installing into a different Python env than raven's is a silent no-op.
2. **Restart long-running processes** — a TUI/gateway started before install isn't traced until restarted (Step 3).
3. **`uv sync` / `ec.sh update` prunes it** — if raven is a uv project, `uv sync` removes packages
   not in raven's lockfile, including this one. Reinstall after such an update, or use `uv sync --inexact`.
4. **Version-drift** — it monkeypatches raven internals (verified against raven 0.1.0,
   branch `refactor/Raven`). If a future refactor changes a patched method's signature, that one
   probe self-disables with a warning (agent unaffected, others keep working). The `tracing.bootstrap`
   span's `plugin.probes` map shows which are live, and **Step 4b self-heals a disabled probe** in place.

## Troubleshooting
- **No spans / not listed in `raven plugins`** → wrong env (Step 1), or pruned by `uv sync` (caveat 3).
- **Only a small `llm.call` (the SkillForge rewriter) shows, no main model call** → the plugin predates
  the streaming-capture fix, or the running process wasn't restarted (Step 3). Reinstall + restart.
- **`probes bound: N/16` with N<16** → version drift; run **Step 4b** to re-align the disabled
  probe(s) against this raven (bound ones keep tracing meanwhile).
- **Viewer panel empty / stale** → refresh the browser (no auto-poll); confirm a turn actually ran.

## How it works (for the curious)
A pip install (a) registers the `raven.plugins` entry point (so `raven plugins` lists it) and
(b) drops `raven_tracing_autoload.pth` into site-packages. The `.pth` runs `import raven_tracing`
at interpreter startup, which calls `instrument.install()` — wrapping class methods
(`LLMProvider.chat_with_retry`, `AgentLoop._llm_call_stream` for streaming, `ToolRegistry.execute`,
`_process_message`, and the memory/subagent/plugin choke points) before any AgentLoop is built. It
contributes no memory backend or tool — it's a pure observer. Spans go to a stdlib JSONL store.
Full design + per-node I/O is in `./package/raven-tracing/README.md`.
