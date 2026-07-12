# Pluggable tracing standard (OpenTelemetry-aligned)

**Status: APPROVED spec. No code yet — implement per §11.** Modeled on OpenTelemetry's
structure: a **span/resource envelope**, a **semantic-attribute registry** (keys
+ types + requirement levels), and a **node-type taxonomy with per-type attribute
schemas** — the last is where field differences live (an `llm.call` carries
token usage; a `tool.call` does not). The plugin defines this standard + a
generic renderer; each provider (raven natively, plus any third party) *aligns*
by emitting conforming spans. New provider or node type = **zero viewer changes**.

Companion to [`SPAN_SCHEMA.md`](./SPAN_SCHEMA.md) (the on-the-wire envelope,
`audit.span.v1`), which is unchanged.

## 0. What we borrow from OpenTelemetry

| OTel concept | here |
|---|---|
| Span (name, kind, start/end, status, attributes, events, links) | `audit.span.v1` envelope (SPAN_SCHEMA.md) |
| Resource (`service.name`, …) — identity of the emitter | `framework` + resource attrs (§2.1) |
| Semantic conventions — a registry of well-known attribute keys | attribute registry (§2) |
| Requirement levels (Required / Conditionally Required / Recommended / Opt-In) | used verbatim (§2) |
| Per-operation conventions (GenAI, DB, HTTP spans each define their attrs) | per-type attribute schemas (§4) |

We adopt OTel's **structure and requirement-level vocabulary**, and keep our own
namespaces (`llm.*`, `tool.*`, `memory.*`, …) rather than renaming to `gen_ai.*`
(existing spans stay valid); alignment is noted where relevant. `span.kind` stays
`INTERNAL` for all nodes (single-process); the meaningful taxonomy is `name` (§3).

**One deliberate deviation from OTel:** every node is "one step of a request", so
`input` and `output` are **top-level span fields** (siblings of `name`/`status`),
not buried in `attributes`. OTel keeps everything in attributes; we elevate these
two because they are universal and central. Both are **optional** — a provider MAY
omit them (`null`) — but their *position* is fixed at the top level (§2.2).

## 1. Roles

| Role | Owns | Ships |
|---|---|---|
| **Plugin (this repo)** | the standard | envelope, attribute registry, type taxonomy + per-type schemas, generic renderer + fallback, raven's descriptor |
| **Provider (adapter)** | alignment | code emitting conforming spans + (optional) a `<provider>.json` descriptor |
| **Viewer** | generic rendering | renders from type schema + descriptor; unknown types → fallback |

raven is simply provider #1, its descriptor bundled — not special-cased in viewer code.

## 2. The span

### 2.0 Envelope — top-level fields (full shape)

Every span is one JSON object. Identity + tree + timing + status live at the top
level (from SPAN_SCHEMA.md, unchanged), and we add `input`/`output` (§2.2)
alongside them. `attributes` holds everything else (§2.1, §2.3–2.5).

```jsonc
{
  "schemaVersion": "audit.span.v1",
  "traceId":      "string",        // groups one turn / run
  "spanId":       "span-<hex>",    // this node's unique id
  "parentSpanId": "string | null", // tree edge → its parent node (null = root)
  "name":   "memory.recall",       // node type (§3)
  "kind":   "INTERNAL",
  "startTime": "ISO8601", "endTime": "ISO8601",
  "status": { "code": "OK | ERROR", "message": "string" },
  "input":  { … } | null,          // §2.2
  "output": { … } | null,          // §2.2
  "attributes": { … },             // §2.1, §2.3–2.5
  "events": [ { "time": "ISO8601", "name": "string" } ]
}
```

- **Identity / tree**: `traceId` groups a turn; `spanId` + `parentSpanId` build
  the node tree (a root has `parentSpanId: null`); a child session's root re-parents
  under the `subagent.call` that spawned it via `link.*` (§2.4). These are
  **Required** and unchanged from `audit.span.v1`.
- The **attribute registry** below (§2.1, §2.3–2.5) describes only the
  `attributes` map — the metadata *around* the payloads.

### 2.1 Resource / common (any span)

| key | type | level | meaning |
|---|---|---|---|
| `framework` | string | Required | emitter identity (`raven`/`openclaw`/…) — the "resource" |
| `session.id` | string | Required | session this span belongs to |
| `session.key` | string | Recommended | human-facing session key |
| `agent.id` | string | Recommended | agent identity |
| `channel.id` | string | Opt-In | channel (tui/gateway/…) |
| `workspace.dir` | path | Opt-In | working dir |

### 2.2 Input & output (top-level span fields)

Not attributes — two **top-level fields on the span envelope** (extends
SPAN_SCHEMA.md, additive & optional):

```jsonc
{
  "name": "memory.recall",
  "status": { "code": "OK" },
  "input":  { "kind": "text", "preview": "你好" },              // small: inline
  "output": { "kind": "list", "artifact_path": "…/recall.json" }, // large: artifact ref
  "attributes": { "memory.hits": 4, ... },
  ...
}
```

Shape of each (both optional → `null` when a provider omits):

| field | type | meaning |
|---|---|---|
| `kind` | string | render hint: `text` \| `json` \| `messages` \| `list` \| `markdown` (default `json`) |
| `preview` | string | small payload inline |
| `artifact_path` | path | large payload, in a dedup'd artifact file |

A provider fills `preview` (small) and/or `artifact_path` (large); `kind` lets the
viewer render it **with no descriptor at all**. This replaces the old `io.*`
attribute idea — input/output are now first-class, every node has the slot.

### 2.3 Usage (`llm.usage.*` — LLM-scoped, NOT universal)

Present **only** on nodes that call an LLM and on ancestors that roll their usage
up (§4). Aligns with OTel `gen_ai.usage.*`.

| key | type | level | meaning |
|---|---|---|---|
| `llm.usage.input_tokens` | int | Recommended | prompt tokens |
| `llm.usage.output_tokens` | int | Recommended | completion tokens |
| `llm.usage.cost_total` | double | Recommended | cost (USD) |
| `llm.usage.cache_read` / `cache_write` | int | Opt-In | cache tokens |
| `llm.usage.rolled_up` | bool | Conditionally Required | `true` on ancestor totals (turn/subagent); absent/false on the real `llm.call` |

### 2.4 Link (`link.*` — cross-trace)

| key | type | level | meaning |
|---|---|---|---|
| `link.trace_id` | string | Conditionally Required | required when the node spawns a child trace (subagent) |
| `link.span_id` | string | Recommended | target span within the linked trace |

### 2.5 Domain namespaces

Each node type draws from its own namespace; §4 says which keys apply per type.
The node's payloads go in the top-level `input`/`output` fields (§2.2); these
namespaces are the *metadata* around them. Registry (grounded in raven's real attrs):

- **`llm.*`** — `llm.provider` (str), `llm.model` (str), + `llm.usage.*` (§2.3).
- **`tool.*`** — `tool.name` (str), `tool.call_id` (str), `tool.duration_ms` (int), `tool.error` (str).
- **`memory.*`** — `memory.query` (str), `memory.scope` (str), `memory.user_id` (str), `memory.top_k` (int), `memory.hits` (int), `memory.session_id` (str), `memory.message_count` (int), `memory.injected`/`memory.used` (str[]).
- **`subagent.*`** — `subagent.id` (str), `subagent.task` (str), `subagent.status` (str), `subagent.session_id` (str).
- **`skill.*`** — `skill.path` (str), `skill.read.artifact_bytes` (int), `skills.prompt.names` (str[]).
- **`turn.*`** — `turn.plugin.backend` (str).

(raven today stores payloads in `llm.input.artifact_path` etc.; its adapter moves
them into the top-level `input`/`output` slots — see §9.)

## 3. Node-type taxonomy (standard types)

`name` is the operation type. Canonical set (providers MAY add custom types, §5):

| type | represents | tree position |
|---|---|---|
| `session.turn` | one turn / run (root) | root; child sessions nest under a `subagent.call` |
| `llm.call` | a model request (streaming or not) | child of the turn |
| `tool.call` | a tool execution | child of the turn |
| `subagent.call` | spawning a sub-agent | child of the turn; links to a child `session.turn` |
| `skill.read` / `skill.inject` / `skill.use` | loading / injecting a skill | usually under a tool or turn |
| `memory.recall` | retrieval before the turn | under the turn |
| `memory.store` | writing the turn to long-term memory | under the turn |
| `memory.feedback` | skill-usage signal back to memory | under the turn |
| `plugin.load` / `tracing.bootstrap` | lifecycle / meta | standalone |

## 4. Per-type attribute schema (where field differences live)

The heart of the standard: each type declares which **attributes** apply, at what
level. Top-level `input`/`output` (§2.2) are universal (optional on every type),
so they are **not** listed here — this table is the metadata *around* the payloads.
**Usage appears only on `llm.call` (real) and `session.turn` / `subagent.call`
(rolled-up); tool / memory / skill carry none.**

| type | input/output (top-level) | Required attrs | Recommended attrs | Conditionally Required | Opt-In |
|---|---|---|---|---|---|
| `session.turn` | the user turn / final reply | `framework`, `session.id` | `session.key`, `agent.id`, `turn.plugin.backend` | `llm.usage.*`+`rolled_up=true` (⇔ subtree has ≥1 `llm.call`) | `channel.id`, `workspace.dir`, `trigger` |
| `llm.call` | prompt / completion | `framework`, `session.id`, `llm.provider`, `llm.model` | `llm.usage.{input_tokens,output_tokens,cost_total}` | — | `llm.usage.cache_*` |
| `tool.call` | args / result | `framework`, `session.id`, `tool.name` | `tool.call_id`, `tool.duration_ms` | `tool.error` (⇔ failed) | `tool.persisted.artifact_path` |
| `subagent.call` | task / result | `framework`, `session.id` | `subagent.id`, `subagent.task`, `subagent.status` | `link.trace_id` (⇔ child session exists), `llm.usage.*`+`rolled_up` (⇔ child subtree has llm) | — |
| `skill.read` | request / content | `framework`, `session.id` | `skill.path` | — | `skill.read.artifact_bytes` |
| `memory.recall` | query / recalled list | `framework`, `session.id` | `memory.query`, `memory.scope`, `memory.hits` | — | `memory.user_id`, `memory.top_k` |
| `memory.store` | messages / (async deposit) | `framework`, `session.id` | `memory.message_count` | — | `memory.session_id`, deposit enrichment |
| `memory.feedback` | injected/used ids / — | `framework`, `session.id` | — | — | `memory.injected`, `memory.used` |

Note: no `llm.usage.*` row on tool/skill/memory — by schema, not by omission.

## 5. Extension model (custom types + attributes)

Providers extend, they don't fork:
- **Custom node types**: any `name` not in §3. MUST still carry §2.1 common attrs
  and SHOULD carry §2.2 `io.*`. Renders via descriptor (§6) or fallback (§8).
- **Custom attributes**: put provider-specific keys under `x-<provider>.*`
  (reserved-safe). Standard namespaces (`llm.*`, `tool.*`, `memory.*`,
  `subagent.*`, `skill.*`, `turn.*`, `io.*`, `link.*`, `session.*`, `framework`)
  are reserved — don't repurpose their keys with different meaning.
- **Unknown types/attrs never error**: the viewer renders them generically.

## 6. Rendering — derived from the schema

A type's **default rendering needs no descriptor** — it comes straight from the
envelope + schema:
- Input panel ← top-level `input` (rendered by its `kind`)
- Output panel ← top-level `output` (rendered by its `kind`)
- usage `kv` panel ← **iff** `llm.usage.*` present (the §4 schema says which types)
- `link` button ← iff `link.*` present
- failure ← envelope `status` (+ any `*.error` the schema marks)

A **descriptor** (`<provider>.json`) is an optional layer *only* for enrichment —
custom panel titles, a `custom:*` renderer, or pulling extra data from
`attributes`. Panel vocabulary (fixed): `text` · `json` · `list` (+`item` template)
· `messages` · `kv` · `markdown` · `link` · `custom:<id>`. Sources: the top-level
`input`/`output`, `attr:<key>`, or `artifact:<path-attr>` (+ `pick:<field>`).

```jsonc
// raven descriptor — only needed because store's output is a bespoke deposit card
{ "type": "memory.store", "label": "memory store", "subtitle": "{memory.message_count} msgs",
  "panels": [
    { "title": "Input",  "source": "input", "render": "messages" },  // top-level input
    { "title": "Output", "render": "custom:storeDeposit" }           // bespoke
  ]}
```

`custom:<id>` resolves from `viewer/panels/custom.js`, signature
`(span, ctx) => htmlString`, `ctx = { artifact, attr, jumpTo, terms }`.

## 7. Registration & merge order (low → high precedence)

1. bundled `viewer/descriptors/raven.json` (native)
2. other bundled `viewer/descriptors/*.json`
3. `<state_dir>/descriptors/*.json` (per-install drop-in)
4. `--descriptors <path>` (explicit)

Same-`type` later overrides earlier. Adding a provider = emit conforming spans +
drop a `<name>.json`. No viewer edit, no rebuild.

## 8. Generic fallback (no descriptor — never crashes, never dumps raw)

1. title = `name`; subtitle = a short attr if any
2. Input panel = top-level `input` (by its `kind`), if present
3. Output panel = top-level `output` (by its `kind`), if present
4. usage `kv` **only if** `llm.usage.*` present (never invented)
5. remaining attributes → one `kv` panel

Because `input`/`output` are top-level (§2.2), the fallback is trivial and
correct for **any** provider — no `io.*` sniffing needed.

## 9. raven native mapping (proof the standard covers today's viewer)

9 node types today → 6 fully declarative from schema/descriptor, 3 use a `custom:*`
hook. The ~53 `name ===` branches in server.js collapse into descriptor
`label`/`subtitle`/`status` + the §4 schema.

raven's adapter fills top-level `input`/`output` (with a `kind`), so most nodes
render straight from the envelope; only 3 keep a `custom:*` hook.

| node | input.kind → output.kind | usage | needs descriptor? |
|---|---|---|---|
| `session.turn` | text → text | rolled-up `kv` | no |
| `llm.call` | (custom modelInput) → text | real `kv` | yes — Model Input custom |
| `tool.call` | json → json | none | no |
| `subagent.call` | text → text (+ `link`) | rolled-up `kv` | no |
| `skill.read` | json → text | none | no |
| `memory.recall` | text → list | none | no |
| `memory.store` | messages → (custom storeDeposit) | none | yes — Output custom |
| `memory.feedback` | text → text | none | no |
| `plugin.load`/`tracing.bootstrap` | — (attrs `kv`) | none | no |

## 10. Third-party extension example (zero viewer change)

A reranker provider emits `retrieval.rerank` spans with `framework=acme`, a
top-level `input` (candidates, `kind:"list"`) + `output` (ranked, `kind:"list"`),
no usage. **With no descriptor at all**, §8 already renders both as lists and
invents no usage panel. It only needs a descriptor to prettify (title + item template):

```jsonc
[{ "type": "retrieval.rerank", "label": "rerank", "subtitle": "{x-acme.model}",
   "panels": [
     { "title": "Candidates", "source": "input",  "render": "list", "item": "{score} — {text}" },
     { "title": "Ranked",     "source": "output", "render": "list", "item": "{score} — {text}" }
   ]}]
```

## 11. Migration (on implement — not now)

1. Build the generic engine: schema-derived defaults + panel renderers + fallback;
   keep the 9 cards as temporary `custom:*` shims → zero regression.
2. Write `descriptors/raven.json` (§9); convert the 6 declarative nodes off shims;
   keep 3 real customs.
3. Delete the dead `name ===` branches / card dispatch.
4. Docs + the §10 example provider as a smoke test. `/api/search` is already
   generic (keys off `*.artifact_path`) — no per-provider change.

## 12. Non-goals (pragmatic bounds)

- No plugin marketplace / remote descriptor fetch — local files only.
- No general templating language — `{attr}` interpolation + a fixed panel set;
  richer needs use `custom:*`.
- `audit.span.v1` envelope gains **only** two optional top-level fields
  (`input`, `output`, §2.2); otherwise unchanged. Old spans without them still
  read fine (they show no input/output panel). SPAN_SCHEMA.md's envelope gets
  synced to match on lock.
- Viewer never computes roll-up usage (that's the provider's job, §2.3).
