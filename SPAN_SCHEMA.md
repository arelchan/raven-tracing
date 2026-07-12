# tracing span schema — `audit.span.v1`

The single contract shared by all three framework plugins. Every collector
writes JSONL in this schema, so one viewer renders any framework's traces.

## Storage layout (per framework state dir)

```
<state_dir>/logs/audit-events.log     # one JSON event record per line
<state_dir>/logs/audit-spans.log      # one JSON span per line
<state_dir>/logs/audit-artifacts/...   # large payloads, SHA1-deduped, daily-bucketed
<state_dir>/logs/archive/<date>/...    # rotated logs (by date or TRACE_LOG_MAX_BYTES)
```

State dir per framework:

| framework | state dir | env override |
|---|---|---|
| openclaw | `~/.openclaw` | `OPENCLAW_STATE_DIR` |
| hermes | `~/.hermes` | `HERMES_HOME` |
| everclaw | `~/.everclaw/traces` | config data dir |

## Span object

```jsonc
{
  "schemaVersion": "audit.span.v1",
  "traceId": "string",            // one turn / run
  "spanId": "span-<hex>-<hex>",
  "parentSpanId": "string | null",
  "name": "session.turn | llm.call | tool.call | subagent.call | skill.read",
  "kind": "INTERNAL",
  "startTime": "ISO8601",
  "endTime": "ISO8601",
  "status": { "code": "OK | ERROR", "message": "string" },
  "input":  { "kind": "text|json|messages|list|markdown", "preview": "string", "artifact_path": "string" } | null,
  "output": { "kind": "text|json|messages|list|markdown", "preview": "string", "artifact_path": "string" } | null,
  "attributes": { /* see below */ },
  "events": [ { "time": "ISO8601", "name": "string" } ]
}
```

`input` / `output` are optional top-level fields (a node's payloads); a provider
MAY omit them (`null`). Small payloads go inline in `preview`, large ones in an
`artifact_path`; `kind` is a render hint. Old spans without these fields stay
valid. See the semantic + rendering standard in
[`TRACING_STANDARD.md`](./TRACING_STANDARD.md) §2.2.

### Tree shape

```
session.turn (root)
├── llm.call
├── tool.call
│   └── skill.read           # when a read tool loads a SKILL.md
└── subagent.call
    └── session.turn         # child session's spans nest here
        ├── llm.call
        └── tool.call
```

`llm.call` and `tool.call` are direct children of the root (tools are NOT
nested under the llm call). A child session's root nests under the
`subagent.call` that spawned it via a child-session→parent-span link.

### Common attributes

| key | meaning |
|---|---|
| `span.type` | `session` / `model_call` / `tool_call` / `subagent_call` / `skills` |
| `framework` | `openclaw` / `hermes` / `everclaw` — span origin |
| `session.id`, `session.key`, `agent.id` | identity |
| `audit.schema_version` | `audit.span.v1` |

### Per-type attributes

| span | key attributes |
|---|---|
| `llm.call` | `llm.provider`, `llm.model`, `llm.call_id`, `llm.usage.{input,output,cache_read,cache_write,total}_tokens`, `llm.usage.cost_total`, `llm.{system,user}_prompt_preview`, `llm.output_preview`, `llm.invocation_source`, `llm.input/output.artifact_*` |
| `tool.call` | `tool.name`, `tool.call_id`, `tool.args_preview`, `tool.result_preview`, `tool.duration_ms`, `tool.error`, `tool.input/output.artifact_*` |
| `subagent.call` | `subagent.id`, `subagent.task`, `subagent.session_id`, `subagent.status` |
| `skill.read` | `skill.name`, `skill.path`, `skill.source` |

Artifacts: large payloads (full prompt/response/tool IO) are written under
`audit-artifacts/` and referenced from spans via `<prefix>.artifact_path` /
`.artifact_sha1` / `.artifact_bytes`; the span itself carries only a preview.

## Event → span mapping (how each framework produces the schema)

| span | openclaw hook | hermes hook | everclaw EventType |
|---|---|---|---|
| `session.turn` | `session_start` / `session_end` | `on_session_start` / `on_session_end` | `SESSION_CREATED` / `SESSION_IDLE` |
| `llm.call` | `llm_input` + `llm_output` | `pre_api_request` + `post_api_request` | `LLM_CALL_INITIATED` + `LLM_CALL_COMPLETED` |
| `tool.call` | `before_tool_call` + `tool_result_persist` | `pre_tool_call` + `post_tool_call` | `TOOL_EXECUTED` / `TOOL_FAILED` |
| `subagent.call` | `subagent_spawned` + `subagent_ended` | `subagent_start` + `subagent_stop` | `SUBAGENT_SPAWNED` + `SUBAGENT_COMPLETED/FAILED` |

Pending-span pairing keys: openclaw `callId` / `toolCallId`; hermes
`api_request_id` / `tool_call_id`; everclaw self-generated `call_id` +
`tool_call.id`. The Python collector (`core_py`) pairs `*_start` → `*_end`
internally; the openclaw JS plugin does the same in-process.
