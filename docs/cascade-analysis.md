# Cascade causal latency analysis

Implemented locally for [issue #19](https://github.com/engkimo/voxbench/issues/19).
The analyzer derives service waits only from explicitly related metadata-only
events. It does not inspect conversation text and does not require Pipecat or a
provider SDK.

## Output

`GET /runs/{run_id}/timeline` returns:

- safe top-level `ai_mode` and `ai_components` projections;
- one `lanes.turns` analysis per observed LLM operation;
- observed latency intervals with source `cascade_causal_analysis_v1`;
- latency incidents only when the resolved v2 config contains an applicable SLO.

An operation is scoped by turn, LLM request, response and generation epoch.
Segments and downstream request aliases remain separate. Retries and tool-call
continuations therefore remain distinct operations even when they belong to the
same turn. The analyzer walks explicit `parent_request_alias` links; it never
associates an event because its timestamp happens to be nearby.

Every measurement includes:

- `status`: `observed`, `unobserved` or `indeterminate`;
- start/end event references and the complete evidence-reference list;
- turn/request/response/segment/epoch scope;
- `definition` and `definition_version: cascade-latency/v1`;
- clock domain and alignment uncertainty when known;
- a stable reason alias when a value cannot be derived.

## Measurements

| Name | Endpoints |
| --- | --- |
| `stt_finalization_wait` | Independent `turn.speech_ended` → correlated `stt.final_emitted` |
| `turn_coordination_wait` | Correlated STT final → `turn.committed` |
| `llm_dispatch_wait` | Committed turn → actual LLM request start |
| `llm_first_output_wait` | LLM request start → first declared output of any kind |
| `llm_first_answer_text_wait` | LLM request start → first answer text eligible for speech |
| `text_aggregation_wait` | First answer text → each correlated segment ready for TTS |
| `tts_dispatch_queue_wait` | Segment ready → actual TTS request start |
| `tts_first_audio_wait` | TTS request start → first correlated PCM output |
| `output_playback_start_wait` | First TTS PCM → first matching local frame write |
| `end_to_end_local_response_wait` | Independent speech end → first causally matching local frame write |

`llm.first_output` carries only an allowlisted `output_kind` value: `answer`,
`reasoning`, `tool_call` or `other`. It carries no output content. Reasoning or a
tool call does not satisfy the first-answer boundary.

The end-to-end value stops at a local write. `remote_playout_observed` remains
false. It is not a remote audible-response measurement.

## Critical path

Each operation contains a `critical_path` projection for the first causally
matching playback segment. Its adjacent waits cover speech end, STT finalization,
turn coordination, LLM dispatch/answer, aggregation, TTS dispatch/first PCM and
local playback start. Later segments retain their own measurements.

The analyzer does not add complete STT, LLM and TTS durations. Service work,
generation and playback can overlap; summing their lifecycle durations would
double-count time. A complete critical path instead reports the actual
end-to-end span, covered adjacent waits and any unexplained duration.

## Missing and clock evidence

- A missing endpoint returns `unobserved`, never zero.
- An unresolved or explicitly unobserved parent remains in coverage.
- Different clock domains return `indeterminate` until a calibrated mapping is
  available. An uncertainty number alone does not invent that mapping.
- An end timestamp before its start returns `indeterminate`.
- A new generation epoch cannot reuse an older response's playback boundary.
- Missing terminal events keep `completion_observed` false; run completion does
  not synthesize them.

Observed same-domain durations sum the two endpoint alignment uncertainties when
both are known. If either endpoint uncertainty is unknown, the derived value
keeps uncertainty as null.

## Explicit latency SLOs

V2 configs can opt into a latency contract under `spec.observability`:

```json
{
  "service_latency_slos": [
    {
      "id": "local-response",
      "measurement": "end_to_end_local_response_wait",
      "max_ms": 800
    }
  ]
}
```

IDs and measurement names are unique. `max_ms` must be finite and positive.
Only an observed measurement above its matching threshold creates a
`cascade_latency_slo_v1` incident. Without a contract, intervals report
`latency_threshold_status: not_configured` and ordinary service metadata creates
no latency incident.

An SLO does not authorize transcript/audio retention and does not imply STT
accuracy, semantic answer quality, pronunciation quality or remote playout.

## Persistence and current limits

Analysis is reconstructed deterministically from persisted timeline events and
the pinned resolved config. No separate mutable analysis row is required.
SQLAlchemy/SQLite restart reconstruction is verified by default; an equivalent
real-Postgres restart test is opt-in through `VOXBENCH_TEST_POSTGRES_URL`.

The next product goal is a pair of deterministic Cascade test projects: one
application-owned direct STT/LLM/TTS loop and one Pipecat-backed loop. Both will
emit the same common observations so VoxBench can compare, tune and evaluate the
runtime choices. That work follows this analyzer and remains separate from the
current C3 implementation.
