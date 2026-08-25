# Append-only Working Memory Protocol

## Goal

Keep the early request prefix stable for provider-side context caching without
making the Agent forget durable task state. The protocol applies to the main
Agent, sub-agents, chat Agent, and verifier; each role still controls which
state sections it is allowed to receive.

## Message layout

```text
0  system     Agent system prompt
1  system     immutable Working Memory Protocol
2+ user       user input or explicitly marked orchestrator instruction
2+ assistant  response, reasoning, and tool calls
2+ tool       tool result paired by tool_call_id
2+ system     append-only Working Memory Snapshot, when state changed
```

Tool definitions remain in the request's top-level `tools` field. Context
Inspector displays them after the protocol for readability, but they are not a
message and they are not Working Memory.

## Protocol envelope

The second system message is fixed for the life of an append-only context epoch.
Every memory entry begins with this exact marker:

```text
[working memory snapshot — append-only-v1; system-managed; not user input]
protocol: append-only-v1
epoch: 1
version: 3
state_sha256: <sha256 of the materialized body>
representation: complete-materialized-state
supersedes: epoch=1,version=2
---
<complete current memory state>
[end working memory snapshot]
```

Snapshots are full materialized states rather than deltas. This costs more
cached tokens but removes ambiguity and makes interruption recovery
deterministic: the highest version in the highest epoch is sufficient by itself.

## Append rules

1. Compute a deterministic SHA-256 digest of the current durable state.
2. Append nothing when the digest has not changed.
3. When it changed, append one new system snapshot at the end of the current
   transcript and increment the version.
4. Never insert a snapshot while any assistant tool call lacks its matching
   `role=tool` result.
5. Never edit an existing protocol or snapshot message.
6. Persist epoch, version, and digest with the session checkpoint so a resumed
   worker continues the same sequence.

## Compaction

Compaction is the sole epoch boundary. Historical snapshots are excluded from
the transcript summary, the epoch increases, and one complete version-1
snapshot is appended after the retained tail. The protocol remains unchanged.
This is the explicit exception to retaining every old snapshot indefinitely;
the state survives as a new complete snapshot.

## Experiment control

Set `context.working_memory_mode` in `config.yaml`:

- `append_only`: immutable protocol and versioned snapshots.
- `replace`: legacy behavior that regenerates message 2 on every request.

Compare cached/uncached input tokens, request cost, context size, latency,
decision recall, and interruption recovery on otherwise identical runs.

For source-frozen benchmark runs, select the group without editing the shared
configuration:

```bash
python -m mathmodel.experiment submit --label wm-append \
  --working-memory-mode append_only
python -m mathmodel.experiment submit --label wm-replace \
  --working-memory-mode replace
```

The selected mode is frozen into `config.yaml` and recorded as
`settings.working_memory_mode` in the experiment manifest.
