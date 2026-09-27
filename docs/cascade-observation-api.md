# Metadata-only service observation API

Implemented on [Draft PR #25](https://github.com/engkimo/voxbench/pull/25),
pending review and merge, for [issue #18](https://github.com/engkimo/voxbench/issues/18).
This contract lets direct applications, Pipecat applications and other runtimes
report the same observed STT/LLM/TTS/realtime boundaries. It does not require a
framework SDK and does not execute providers or retain conversation content.

## Lifecycle

1. Resolve a v1 or v2 config. V2 examples are under `examples/v2/`.
2. Start an observation-owned run with `POST /runs/observed`. V2 `/runs` and
   `/runs/async` remain unsupported because execution belongs to a later slice.
3. Send batches to `POST /v1/observations` with `run_id` and `service_events`.
4. Finish with `POST /runs/{run_id}/complete` or `/fail`.

The observation endpoint retains its existing URL because `service_events` is a
backwards-compatible batch extension. Every event carries its own required
`schema_version: voxbench/service-event/v1`.

## Plain HTTP example

Any language capable of sending JSON can use the API:

```json
{
  "run_id": "run UUID returned by /runs/observed",
  "service_events": [
    {
      "schema_version": "voxbench/service-event/v1",
      "collector_alias": "application-a",
      "event_alias": "llm-start-1",
      "kind": "llm.request_started",
      "component_id": "llm-main",
      "role": "llm",
      "generation_epoch": 0,
      "turn_alias": "turn-1",
      "request_alias": "llm-request-1",
      "parent_request_alias": "stt-request-1",
      "response_alias": "response-1",
      "attributes": {"attempt_ordinal": 0},
      "ts": "2026-09-27T01:02:03.456+00:00",
      "clock_domain": "application-wall",
      "alignment_uncertainty_ms": 2.0
    }
  ]
}
```

The standalone JSON Schema is `schemas/service-event.v1.schema.json`. Registry
semantic checks add component/role/authority validation after structural parsing.

`collector_alias` namespaces `event_alias`; persisted IDs have the form
`service:{collector_alias}:{event_alias}`. Aliases are application-generated,
run-local safe identifiers. Do not copy provider IDs into them. A timezone-aware
timestamp is required and normalized to UTC. Preserve the actual clock domain
and alignment uncertainty; do not claim clocks are aligned when they are not.

## Python observer

```python
from voxbench.observability import ServiceEvent, VoxBenchObserver

observer = VoxBenchObserver(run_id, transport, max_pending_items=2048)
observer.observe_service_event(ServiceEvent(
    collector_alias="application-a",
    event_alias="tts-first-1",
    kind="tts.first_pcm",
    component_id="tts-main",
    role="tts",
    request_alias="tts-request-1",
    response_alias="response-1",
    segment_alias="segment-1",
    generation_epoch=0,
    attributes={"observed_boundary": "first-pcm-frame"},
))
observer.flush()  # call outside the media callback
```

The observer bounds pending items. Items arriving after `max_pending_items` are
dropped rather than blocking a callback; the cumulative count is exposed as
`observation_drop_count`. A failed transport restores the unsent remainder.
Applications should export or alert on the drop counter. Batch limits do not
replace this queue budget.

## Closed event vocabulary

| Boundary | Kinds |
| --- | --- |
| Session | `service.session_started`, `service.session_ended`, `service.failed` |
| STT | `stt.partial_emitted`, `stt.final_emitted` |
| Turn | `turn.speech_ended`, `turn.committed` |
| Text LLM | `llm.request_started`, `llm.first_output`, `llm.first_answer_text`, `llm.response_completed` |
| Aggregation | `aggregation.segment_ready` |
| TTS | `tts.request_started`, `tts.first_pcm`, `tts.response_completed` |
| Integrated realtime | `realtime.request_started`, `realtime.first_audio`, `realtime.response_completed` |
| Local playback | `playback.write_started`, `playback.write_completed`, `playback.discarded` |
| Cancellation | `cancel.requested`, `cancel.acknowledged` |

Kinds and roles are checked together. The server also checks `component_id`
against the resolved run config. STT/LLM/TTS/realtime IDs must match their service
roles; aggregation uses its text processor; playback uses a configured output
PCM stage. `turn.committed` must name the configured end-of-turn authority.
V1 realtime uses the normalized component ID `ai`.

Observed boundaries retain their literal meaning:

- `stt.final_emitted` is a bounded STT result, not a committed conversational turn.
- `turn.speech_ended` is an independently observed caller boundary used for STT
  finalization and end-to-end local response timing.
- `llm.first_output` records only `answer`, `reasoning`, `tool_call` or `other`;
  it does not make reasoning/tool output eligible for speech.
- `llm.first_answer_text` is the first answer boundary; tool/reasoning content is
  neither stored nor treated as an answer.
- `tts.first_pcm` is the first observed PCM frame from TTS.
- `playback.write_started` is a local write boundary and does not prove remote
  audible playout.
- `cancel.requested` and `cancel.acknowledged` are separate evidence. One does not
  imply the other.

The absence of a terminal event remains incomplete evidence. Completion of the
VoxBench run does not synthesize service terminals.

## Causal aliases and missing coverage

Events can carry `session_alias`, `turn_alias`, `request_alias`,
`parent_request_alias`, `response_alias`, `segment_alias`, and a non-negative
`generation_epoch`. Continuous STT uses a session alias; bounded STT final
operations use request and segment aliases. Retries get new request aliases while
remaining associated with the same turn/response as appropriate.

Each kind requires the relations needed to interpret it. If a required relation
was not observed, omit its alias and list its name in `unobserved_relations`.
This is different from sending an alias whose parent event has not arrived:
unknown parent requests are projected as `unresolved_relations: parent_request`.
That derived marker disappears if the parent later arrives out of order. An
explicit `unobserved_relations` marker remains.

The payload has no run-reference field inside an event. All relationships are
scoped to the batch `run_id`; attempts to add `run_id`, parent run IDs or other
cross-run fields fail closed.

## Privacy and scalar attributes

The DTO forbids extra fields and nested values. It has no transcript, prompt,
generated text, content hash, raw payload, URL, credential, provider ID or tool
argument field. The only optional attributes are allowlisted normalized scalars:

```text
attempt_ordinal, sequence_ordinal, duration_ms, audio_duration_ms,
input_units, output_units, cached_input_units, tool_call_count, queue_depth,
sample_rate_hz, channels, encoding, retry, truncated, terminal_outcome,
failure_alias, cancel_scope, observed_boundary, output_kind
```

Counts and ordinals are non-negative integers, durations are finite and
non-negative, flags are booleans, and strings are short safe aliases. Nulls,
URLs, whitespace-bearing content, containers and unknown keys fail validation.
After causal fields and coverage are normalized, at most 16 scalar attributes
are persisted per event.

Reserved service kind names cannot be submitted through generic
`timeline_events`; callers must pass the typed validation path.

## Retry, conflicts and limits

- Repeating the same collector/event ID with an identical normalized payload is
  idempotent. Changing its payload returns HTTP 409.
- Different collectors may use the same event alias without collision.
- Generic timeline retries now follow the same same-payload/conflict rule.
- A batch may contain at most 128 timeline and service events combined. The
  Python observer splits larger flushes while preserving unsent data on failure.
- A run accepts at most 10,000 persisted timeline/service events and returns HTTP
  429 before mutation when the limit would be exceeded.
- V2 audio chunks remain unsupported until recording segment maps are added.

Events use the existing transactional timeline-event persistence. Repository
reload reconstructs the names, component, clock fields, causal aliases, safe
attributes and coverage markers. This establishes event storage, not deterministic
latency derivation. The local issue #19 implementation is documented in
[cascade-analysis.md](cascade-analysis.md).
