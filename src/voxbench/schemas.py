"""Pydantic models for Phase 0 config and capability manifest contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JsonObject = dict[str, Any]

PluginKind = Literal["engine", "provider", "processor"]
OverrideType = Literal["string", "integer", "number", "float", "boolean", "object", "array"]
Invariant = Literal["duration_preserving", "level_preserving", "isochronous"]
TurnTakingOwner = Literal["server_vad", "client_vad", "semantic", "external"]


class StrictModel(BaseModel):
    """Base class that keeps config and manifest contracts closed by default."""

    model_config = ConfigDict(extra="forbid")


class OverridePermission(StrictModel):
    target: str
    type: OverrideType


class OverrideValue(StrictModel):
    target: str
    value: Any


class IoContract(StrictModel):
    mode: Literal["passthrough", "rate_changing", "format_changing"]
    accepts: JsonObject = Field(default_factory=dict)
    produces: JsonObject | None = None


class ProviderCaps(StrictModel):
    turn_taking_owners: list[TurnTakingOwner] = Field(default_factory=list)
    supported_codecs: list[str] = Field(default_factory=list)
    input_rate: int | None = None
    output_rate: int | None = None


class CapabilityManifest(StrictModel):
    kind: PluginKind
    name: str
    version: str
    param_schema: JsonObject = Field(default_factory=dict)
    io: IoContract | None = None
    invariants_enforced: list[Invariant] = Field(default_factory=list)
    invariants_applicable: list[Invariant] = Field(default_factory=list)
    lossy_expected: list[str] = Field(default_factory=list)
    requires_host_capability: list[str] = Field(default_factory=list)
    allowed_overrides: list[OverridePermission] = Field(default_factory=list)
    provider_caps: ProviderCaps | None = None


class ConfigMeta(StrictModel):
    name: str
    version: str
    parent: str | None = None
    labels: JsonObject = Field(default_factory=dict)


class EngineConfig(StrictModel):
    kind: str
    params: JsonObject = Field(default_factory=dict)


class JitterBufferConfig(StrictModel):
    mode: str
    max_ms: int


class TransportConfig(StrictModel):
    codec: str
    ptime_ms: int
    jitter_buffer: JitterBufferConfig | None = None


class PipelineStage(StrictModel):
    type: str
    plugin: str
    params: JsonObject = Field(default_factory=dict)
    io: IoContract | None = None
    invariants_enforced: list[Invariant] | None = None
    invariants_applicable: list[Invariant] | None = None
    lossy_expected: list[str] | None = None
    requires_host_capability: list[str] | None = None
    host_capabilities: list[str] = Field(default_factory=list)
    overrides: list[OverrideValue] = Field(default_factory=list)


class MediaConfig(StrictModel):
    pipeline: list[PipelineStage]


class DetectorConfig(StrictModel):
    plugin: str
    params: JsonObject = Field(default_factory=dict)


class TurnTakingConfig(StrictModel):
    owner: TurnTakingOwner
    detector: DetectorConfig | None = None
    barge_in: bool = False


class AiConfig(StrictModel):
    provider: str
    model: str
    params: JsonObject = Field(default_factory=dict)
    system_prompt_ref: str | None = None
    tools: list[JsonObject] = Field(default_factory=list)


class ObservabilityConfig(StrictModel):
    stage_taps: bool = False
    record_audio: bool = False
    trace_sample: float = 0.0
    signal_metrics: list[str] = Field(default_factory=list)
    host_metrics: list[str] = Field(default_factory=list)
    cross_session: list[str] = Field(default_factory=list)


class VoiceSpec(StrictModel):
    engine: EngineConfig
    transport: TransportConfig
    media: MediaConfig
    turn_taking: TurnTakingConfig
    ai: AiConfig
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


class VoiceConfig(StrictModel):
    apiVersion: Literal["voxbench/v1"]
    kind: Literal["VoiceConfig"]
    meta: ConfigMeta
    spec: VoiceSpec

