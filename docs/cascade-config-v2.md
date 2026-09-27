# Versioned realtime and cascade configuration

Implemented locally for [issue #17](https://github.com/engkimo/voxbench/issues/17).
This slice validates and resolves configuration. Metadata-only service observation
is now a separate local candidate described in
[cascade-observation-api.md](cascade-observation-api.md). V2 recording ingestion,
inspector support and test-call execution remain future slices. `/runs` and
`/runs/async` reject v2 before creating a run; `/runs/observed` accepts it for
typed service events. Legacy harness, synthetic generation and recording
verification reject it before writing audio.

No Pipecat or other middleware SDK is required. The example plugins are static
capability declarations for deterministic validation, not live provider adapters.

## Resolve an example

Use the same registry API as v1:

```python
from pathlib import Path
from voxbench.registry.service import RegistryService

registry = RegistryService.from_files(
    config_paths=["examples/v2/configs/cascade.json"],
    manifest_paths=Path("examples/v2/manifests").glob("*.json"),
)
result = registry.resolve_config("example-cascade")
print(result.hash)
print(result.resolved["manifest_pins"])
```

For realtime, use `examples/v2/configs/realtime.json` and `example-realtime`.
The CLI also accepts these documents:

```bash
voxbench resolve-config \
  --config examples/v2/configs/cascade.json \
  --manifest examples/v2/manifests/engine.json \
  --manifest examples/v2/manifests/stt.json \
  --manifest examples/v2/manifests/llm.json \
  --manifest examples/v2/manifests/tts.json \
  --manifest examples/v2/manifests/aggregation.json \
  --manifest examples/v2/manifests/input-resampler.json \
  --manifest examples/v2/manifests/output-resampler.json
```

## Source contract

Config and manifest documents both use `apiVersion: voxbench/v2`. Existing
unversioned manifests and `voxbench/v1` configs retain their original models,
default materialization and resolved hash behavior.

- `spec.ai.mode` selects `realtime` or `cascade`, with closed discriminated shapes.
  Realtime has `realtime`; cascade requires `stt`, `llm`, `text_aggregation`, `tts`.
- Every component has `id`, `plugin`, `manifest_version`, `params`. Services also
  require a selected `model`. LLM/realtime may declare `system_prompt_ref` and
  `tools`; STT/TTS may not.
- `spec.engine` has `kind`, `manifest_version`, `params`.
- `spec.media` has separate `input_pipeline` and `output_pipeline` lists. Each
  stage has a globally unique `id`, `type`, plugin reference and existing host,
  override and invariant settings. `type` is a category, not an identity.
- `input_audio` / `output_audio` select exact formats at service audio boundaries.
  Audio formats require an encoding (`pcm16`, `pcm24`, `pcm32`, `float32`), a
  positive integer `rate`, and positive integer `channels`. Text boundaries
  forbid audio selections.

Component IDs start with a lowercase letter and contain up to 64 lowercase
letters, digits, underscores or hyphens. `engine` and `application` are reserved.
IDs are unique across both PCM directions, services and text aggregation.

## Manifest contract

V2 retains plugin kinds `engine`, `provider`, `processor`, with explicit contracts:

| Kind | Required contract |
| --- | --- |
| Engine | `engine_caps`: supported wire codecs, decoded input/output PCM, supported decision authorities |
| Provider | `service_caps`: roles, modalities, supported audio formats, lifecycle/cancellation capabilities and decision authorities |
| PCM processor | `io`: exact accepted/produced PCM and passthrough/rate-changing/format-changing behavior |
| Text aggregation processor | `text_io`: text input and output; audio invariants are forbidden |

Provider roles have fixed modalities: STT audio→text, LLM text→text, TTS
text→audio, realtime audio→audio. Audio boundaries require nonempty supported
format lists; text boundaries forbid those lists. `supported_models`, when
nonempty, constrains the selected model. An empty model list leaves support
unverified; accepting a name does not establish live readiness. `model_selection`
states whether the model is explicit or deployment-pinned.

Lifecycle flags declare partial output, final output, response boundaries and
usage support. Cancellation flags distinguish request, terminal acknowledgement,
transport abort and conversation truncation. They describe expected hooks and
capability, not observed provider behavior.

Every engine/service/processor params object is checked against that manifest's
`param_schema`. Host requirements cannot be removed by stage settings. An audio
stage cannot replace pinned manifest IO or invent enforced invariants. Overrides
must use unique allowlisted targets with valid types.

## Audio and decision boundaries

The input chain connects engine decoded input to STT/realtime input. The output
chain connects TTS/realtime output to engine decoded output. Registry compares
complete formats at every adjacent boundary. Empty chains require equal endpoint
formats; a missing converter fails validation. STT and TTS audio are independent:
no preservation comparison is made through the text LLM.

The wire codec is validated against the engine decode contract. Text LLM manifests
do not contain telephony codec capabilities.

`spec.turn_taking` selects one `speech_detector`, one `end_of_turn` and optional
`interruption` authority. References identify `application`, `engine`, or a
configured component. The application-owned coordinator is an explicit source
selection; engine/components must declare the corresponding authority in their
caps. TTS cannot own any caller decision; a text LLM cannot own speech detection.
Multiple observations do not create additional authorities or commit a turn.

V2 `spec.observability.service_latency_slos` can explicitly bind one positive
`max_ms` threshold to each named Cascade measurement. The list is empty by
default. Ordinary service metadata has no implicit latency threshold; see the
[causal analysis contract](cascade-analysis.md).

## Resolution, pinning and overlays

V2 lookup includes kind, name and manifest version. Re-registering an identical
manifest is idempotent; changing its effective contents under the same identity
fails. Publish a new manifest version instead. The registry stores its own copy
so mutating the registration result does not modify a pinned manifest.
V2 config registration also snapshots the supplied object; changes require
explicit re-registration.

Resolved v2 objects materialize PCM manifest defaults and include sorted
`manifest_pins`, one per component and the engine. Each pin contains component
ID, kind, name, version and SHA-256 of the normalized manifest. Resolved hashes
include the selected models, aggregation policy/params and effective pins.
Config source schemas describe authored configs, not the additional resolution
metadata in `manifest_pins`.

Overlays must explicitly retain `apiVersion: voxbench/v2` and the same AI mode.
Cross-version and cross-mode inheritance fail. Changing just STT, LLM or TTS
within cascade mode is supported; unrelated components retain their values.
Migration from v1 and mode replacement require a separate base today.

`registry.config_views` supplies version-aware service/chain reads as independent
copies without adding v2 fields to stored v1 objects. These helpers do not
establish v2 run support.

## Schemas and verification

`python -m voxbench.schemas_export` generates `config.v2.schema.json` and
`manifest.v2.schema.json` alongside unchanged v1 documents. JSON Schema describes
the structural contract; registry validation also checks semantic relationships.

`tests/test_registry_v2.py` covers valid modes, formats, roles, IDs, decisions,
params, overrides, manifest immutability/version selection and overlays. Frozen
v1 base/overlay fixtures assert canonical JSON/hash compatibility. A SQLAlchemy
repository roundtrip using SQLite checks stored v1 config preservation; it is
not a real Postgres connectivity test. A subprocess rejects middleware imports
while resolving both v2 modes. Run/legacy-audio tests verify explicit rejection
and no created run/audio artifacts.

Credentials remain application/deployment inputs. References may be declared by
plugin params schemas; this slice introduces no credentials, content retention
or diagnostic-model egress. Safe projections for v2 runs belong to later slices.
