"""Framework-independent v2 voice configuration and capability contracts.

Keep these separate from v1: adding defaults to a legacy model changes resolved
JSON and the identity of previously published experiments.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from voxbench.schemas import (
    ConfigMeta,
    Invariant,
    JsonObject,
    ObservabilityConfig,
    OverridePermission,
    OverrideValue,
    PluginKind,
    StrictModel,
    TransportConfig,
)

ComponentId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
NonEmpty = Annotated[str, Field(min_length=1)]
ServiceRole = Literal["stt", "llm", "tts", "realtime"]
Modality = Literal["audio", "text"]
Authority = Literal["speech_detector", "end_of_turn", "interruption"]
ServiceLatencyMeasurement = Literal[
    "stt_finalization_wait",
    "turn_coordination_wait",
    "llm_dispatch_wait",
    "llm_first_output_wait",
    "llm_first_answer_text_wait",
    "text_aggregation_wait",
    "tts_dispatch_queue_wait",
    "tts_first_audio_wait",
    "output_playback_start_wait",
    "end_to_end_local_response_wait",
]


class AudioFormat(StrictModel):
    encoding: Literal["pcm16", "pcm24", "pcm32", "float32"]
    rate: int = Field(gt=0, strict=True)
    channels: int = Field(gt=0, strict=True)


class AudioIoContract(StrictModel):
    mode: Literal["passthrough", "rate_changing", "format_changing"]
    accepts: AudioFormat
    produces: AudioFormat

    @model_validator(mode="after")
    def validate_mode(self) -> AudioIoContract:
        if self.mode == "passthrough" and self.accepts != self.produces:
            raise ValueError("passthrough IO must preserve the audio format")
        if self.mode == "rate_changing" and (
            self.accepts.encoding != self.produces.encoding
            or self.accepts.channels != self.produces.channels
        ):
            raise ValueError("rate_changing IO may change only the sample rate")
        return self


class EngineCaps(StrictModel):
    supported_codecs: list[NonEmpty] = Field(min_length=1)
    decoded_input: AudioFormat
    decoded_output: AudioFormat
    authorities: list[Authority] = Field(default_factory=list)


class LifecycleCaps(StrictModel):
    partial_output: bool = False
    final_output: bool = False
    response_boundaries: bool = False
    usage: bool = False


class CancellationCaps(StrictModel):
    request: bool = False
    terminal_ack: bool = False
    transport_abort: bool = False
    conversation_truncation: bool = False


class ServiceCaps(StrictModel):
    roles: list[ServiceRole] = Field(min_length=1)
    input_modality: Modality
    output_modality: Modality
    audio_inputs: list[AudioFormat] = Field(default_factory=list)
    audio_outputs: list[AudioFormat] = Field(default_factory=list)
    lifecycle: LifecycleCaps = Field(default_factory=LifecycleCaps)
    cancellation: CancellationCaps = Field(default_factory=CancellationCaps)
    authorities: list[Authority] = Field(default_factory=list)
    supported_models: list[NonEmpty] = Field(default_factory=list)
    model_selection: Literal["explicit", "deployment_pinned"] = "explicit"

    @model_validator(mode="after")
    def validate_modalities(self) -> ServiceCaps:
        modalities = {
            "stt": ("audio", "text"),
            "llm": ("text", "text"),
            "tts": ("text", "audio"),
            "realtime": ("audio", "audio"),
        }
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("service roles must be unique")
        for role in self.roles:
            if (self.input_modality, self.output_modality) != modalities[role]:
                raise ValueError(f"service modality mismatch for role '{role}'")
        for modality, formats in (
            (self.input_modality, self.audio_inputs),
            (self.output_modality, self.audio_outputs),
        ):
            if (modality == "audio") != bool(formats):
                raise ValueError("audio boundaries require formats; text boundaries forbid them")
        if "tts" in self.roles and self.authorities:
            raise ValueError("TTS cannot own caller speech, end-of-turn or interruption decisions")
        if "llm" in self.roles and "speech_detector" in self.authorities:
            raise ValueError("text LLM cannot own caller speech detection")
        return self


class TextIoContract(StrictModel):
    input_modality: Literal["text"]
    output_modality: Literal["text"]


class CapabilityManifestV2(StrictModel):
    apiVersion: Literal["voxbench/v2"]
    kind: PluginKind
    name: NonEmpty
    version: NonEmpty
    param_schema: JsonObject = Field(default_factory=dict)
    io: AudioIoContract | None = None
    text_io: TextIoContract | None = None
    engine_caps: EngineCaps | None = None
    service_caps: ServiceCaps | None = None
    authorities: list[Authority] = Field(default_factory=list)
    invariants_enforced: list[Invariant] = Field(default_factory=list)
    invariants_applicable: list[Invariant] = Field(default_factory=list)
    lossy_expected: list[str] = Field(default_factory=list)
    requires_host_capability: list[str] = Field(default_factory=list)
    allowed_overrides: list[OverridePermission] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_kind(self) -> CapabilityManifestV2:
        targets = [override.target for override in self.allowed_overrides]
        if len(targets) != len(set(targets)):
            raise ValueError("manifest allowed override targets must be unique")
        if self.kind == "engine":
            if self.engine_caps is None or self.service_caps or self.io or self.text_io:
                raise ValueError("engine manifest must declare only engine_caps")
        elif self.kind == "provider":
            if self.service_caps is None or self.engine_caps or self.io or self.text_io:
                raise ValueError("provider manifest must declare only service_caps")
        elif self.engine_caps or self.service_caps or (self.io is None) == (self.text_io is None):
            raise ValueError("processor manifest must declare exactly one audio IO or text IO")
        if self.text_io and (
            self.invariants_enforced or self.invariants_applicable or self.lossy_expected
        ):
            raise ValueError("text processors cannot declare audio invariants")
        if self.kind != "processor" and self.authorities:
            raise ValueError("engine/provider authorities belong inside their capability contract")
        return self


class EngineConfigV2(StrictModel):
    kind: NonEmpty
    manifest_version: NonEmpty
    params: JsonObject = Field(default_factory=dict)


class ComponentConfig(StrictModel):
    id: ComponentId
    plugin: NonEmpty
    manifest_version: NonEmpty
    params: JsonObject = Field(default_factory=dict)


class ServiceConfig(ComponentConfig):
    model: NonEmpty
    input_audio: AudioFormat | None = None
    output_audio: AudioFormat | None = None


class ConversationServiceConfig(ServiceConfig):
    system_prompt_ref: NonEmpty | None = None
    tools: list[JsonObject] = Field(default_factory=list)


class RealtimeAiConfig(StrictModel):
    mode: Literal["realtime"]
    realtime: ConversationServiceConfig


class CascadeAiConfig(StrictModel):
    mode: Literal["cascade"]
    stt: ServiceConfig
    llm: ConversationServiceConfig
    text_aggregation: ComponentConfig
    tts: ServiceConfig


class PipelineStageV2(ComponentConfig):
    type: NonEmpty
    io: AudioIoContract | None = None
    invariants_enforced: list[Invariant] | None = None
    invariants_applicable: list[Invariant] | None = None
    lossy_expected: list[str] | None = None
    requires_host_capability: list[str] | None = None
    host_capabilities: list[str] = Field(default_factory=list)
    overrides: list[OverrideValue] = Field(default_factory=list)


class MediaConfigV2(StrictModel):
    input_pipeline: list[PipelineStageV2]
    output_pipeline: list[PipelineStageV2]


class TurnTakingConfigV2(StrictModel):
    # "application" is the application-owned coordinator; "engine" refers to
    # the declared engine. Other references must identify configured components.
    speech_detector: ComponentId
    end_of_turn: ComponentId
    interruption: ComponentId | None = None


class ServiceLatencySloV2(StrictModel):
    id: ComponentId
    measurement: ServiceLatencyMeasurement
    max_ms: float = Field(gt=0, allow_inf_nan=False)


class ObservabilityConfigV2(ObservabilityConfig):
    service_latency_slos: list[ServiceLatencySloV2] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_service_latency_slos(self) -> ObservabilityConfigV2:
        ids = [item.id for item in self.service_latency_slos]
        measurements = [item.measurement for item in self.service_latency_slos]
        if len(ids) != len(set(ids)):
            raise ValueError("service latency SLO IDs must be unique")
        if len(measurements) != len(set(measurements)):
            raise ValueError("service latency SLO measurements must be unique")
        return self


class VoiceSpecV2(StrictModel):
    engine: EngineConfigV2
    transport: TransportConfig
    media: MediaConfigV2
    turn_taking: TurnTakingConfigV2
    ai: Annotated[RealtimeAiConfig | CascadeAiConfig, Field(discriminator="mode")]
    observability: ObservabilityConfigV2 = Field(default_factory=ObservabilityConfigV2)


class VoiceConfigV2(StrictModel):
    apiVersion: Literal["voxbench/v2"]
    kind: Literal["VoiceConfig"]
    meta: ConfigMeta
    spec: VoiceSpecV2
