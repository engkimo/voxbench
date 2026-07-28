# Realtime Provider Adapter Guide

VoxBench provider adapters translate a provider's realtime session into a small,
provider-neutral stream of PCM audio and lifecycle evidence. They do not move
provider-specific response bodies, URLs, credentials, or identifiers into the
core timeline.

## Adapter boundary

Implement the protocols in
`src/voxbench/realtime_providers/providers.py`:

```python
from collections.abc import AsyncIterator

from voxbench.realtime_providers import (
    AudioChunk,
    PlaybackPosition,
    ProviderEvent,
    RealtimeProviderSession,
)


class ExampleSession:
    input_rate = 16_000
    output_rate = 24_000
    auto_interrupts_on_speech = True
    persistent_receive_stream = True

    async def send_pcm(self, audio: AudioChunk) -> None:
        ...

    def receive(self) -> AsyncIterator[AudioChunk | ProviderEvent]:
        ...

    def receive_pcm(self) -> AsyncIterator[AudioChunk]:
        ...

    async def interrupt(self) -> bool:
        ...

    async def truncate_audio(self, position: PlaybackPosition) -> bool:
        ...

    async def close(self) -> None:
        ...
```

The provider object exposes:

```python
class ExampleProvider:
    async def connect(self, *, dry_run: bool = True) -> RealtimeProviderSession:
        ...
```

Use `DryRunRealtimeProviderSession` or an equivalent in-memory implementation so
readiness and conformance tests do not require credentials or network access.

## PCM contract

`AudioChunk` contains mono signed PCM16 little-endian audio:

- `pcm`: complete sample bytes;
- `sample_rate`: the provider-side rate;
- `channels`: currently `1`;
- `encoding`: currently `pcm16`;
- optional item/content positions only when required for provider truncation.

Reject malformed chunks at the adapter boundary. Do not silently reinterpret
sample rate, channel count, or encoding. Resampling belongs to the observed
telephony pipeline, not the provider adapter.

## Normalized lifecycle events

Yield only the lifecycle events that were actually observed:

| Event | Meaning |
| --- | --- |
| `input_speech_started` | Provider VAD observed caller speech start |
| `input_speech_stopped` | Provider VAD observed caller speech end |
| `response_started` | Provider reported an active assistant response |
| `response_done` | Provider reported response completion |
| `interrupted` | Provider reported that active output was interrupted |

Do not synthesize a lifecycle event merely because audio arrived or stopped.
When a provider does not expose an event, leave that boundary unobserved and
document the limitation.

## Interruption behavior

Set `auto_interrupts_on_speech` to describe provider behavior:

- `True`: provider VAD automatically interrupts active output;
- `False`: VoxBench may call `interrupt()` after observed caller speech.

Return `True` from `interrupt()` only when an interruption request was actually
sent. Return `True` from `truncate_audio()` only when the provider accepted a
request to remove unheard audio after the supplied playback position.

`PlaybackPosition.audio_end_ms` is the amount of assistant audio written toward
the caller, not a claim that remote playout occurred.

Set `persistent_receive_stream` to `True` only when one receive iterator is
expected to remain active for the session. This changes how stream completion is
interpreted.

## Safe readiness and failures

Provider readiness should report:

- provider and selected model aliases;
- the name of the required environment variable, never its value;
- whether the optional SDK dependency is installed;
- whether the adapter is in dry-run mode; and
- any alternate environment variable names.

Use `classify_provider_error()` and `ProviderConnectionError` for external
failures. Persist safe aliases such as:

- `invalid-api-key`;
- `permission-denied`;
- `quota-or-rate-limit`;
- `model-unavailable`;
- `location-unavailable`; and
- `provider-temporarily-unavailable`.

Do not persist raw exception messages. They may contain request URLs, project
identifiers, credentials, or provider payloads.

Initial connection retries use `connect_with_retry()`. An established
conversation must not be replayed automatically after a mid-call failure.

## Privacy rules

An adapter must not add these values to observations or tests:

- API keys or bearer tokens;
- raw provider messages;
- external URLs;
- provider project or account identifiers;
- raw item IDs;
- transcripts or caller identifiers; or
- unbounded PCM payloads.

Tests should use synthetic PCM and fake transports. Any metadata flight recorder
must be bounded and must not retain media bytes.

## Required tests

Add deterministic tests under `tests/test_realtime_providers.py` or a focused
provider test module. Cover:

1. dry-run connection without SDK or credentials;
2. missing credential readiness;
3. input PCM validation;
4. output PCM and each supported lifecycle event;
5. interruption and truncation return semantics;
6. clean close and receive-stream completion;
7. safe error classification without raw exception retention; and
8. no network access in the default test suite.

If the adapter changes AudioSocket behavior, add coverage in
`tests/test_audiosocket.py`.

## Registration checklist

When adding a provider:

- export the adapter from `voxbench.realtime_providers`;
- add an optional dependency rather than a mandatory SDK dependency;
- add the CLI selection without provider logic in the control-plane core;
- add a provider manifest and example config;
- document input/output sample rates and VAD behavior;
- record the exact selected model as an environment alias;
- add a credential/model preflight when the SDK supports it; and
- update README and the live-demo guide.

## Pull request evidence

In the pull request, state:

- which lifecycle events are directly observed;
- which events remain unobserved;
- whether the provider auto-interrupts;
- whether truncation is supported;
- input/output audio contracts;
- retry and mid-call failure behavior; and
- how the adapter was tested without exposing credentials or call content.
