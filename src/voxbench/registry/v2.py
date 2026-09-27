"""Static v2 validation; declared capabilities never establish live readiness."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from voxbench.registry.errors import ConfigValidationError
from voxbench.registry.hashing import resolved_hash
from voxbench.schemas_v2 import (
    AudioFormat,
    CapabilityManifestV2,
    CascadeAiConfig,
    ComponentConfig,
    PipelineStageV2,
    ServiceConfig,
    VoiceConfigV2,
)

ManifestLookup = Callable[[str, str, str], CapabilityManifestV2]
ParamValidator = Callable[[str, Any, dict[str, Any]], None]
StageValidator = Callable[[Any, Any], None]


def resolve_v2(
    config: VoiceConfigV2,
    *,
    lookup: ManifestLookup,
    validate_params: ParamValidator,
    validate_stage: StageValidator,
) -> dict[str, Any]:
    spec = config.spec
    engine = lookup("engine", spec.engine.kind, spec.engine.manifest_version)
    validate_params("engine", engine, spec.engine.params)
    caps = engine.engine_caps
    assert caps is not None  # Manifest kind validation establishes this contract.
    if spec.transport.codec not in caps.supported_codecs:
        raise ConfigValidationError("transport.codec is not supported by engine decode contract")
    if spec.transport.ptime_ms <= 0:
        raise ConfigValidationError("transport.ptime_ms must be positive")

    resolved = config.model_dump(mode="json", exclude_none=True)
    pins = [_pin("engine", engine)]
    authorities = {"application": {"speech_detector", "end_of_turn", "interruption"}}
    authorities["engine"] = set(caps.authorities)
    ids = {"application", "engine"}

    def register(component: ComponentConfig, kind: str) -> CapabilityManifestV2:
        if component.id in ids:
            raise ConfigValidationError(f"duplicate or reserved component id '{component.id}'")
        ids.add(component.id)
        manifest = lookup(kind, component.plugin, component.manifest_version)
        validate_params(f"component '{component.id}'", manifest, component.params)
        pins.append(_pin(component.id, manifest))
        authorities[component.id] = set(manifest.authorities)
        return manifest

    services: dict[str, ServiceConfig]
    if isinstance(spec.ai, CascadeAiConfig):
        services = {role: getattr(spec.ai, role) for role in ("stt", "llm", "tts")}
        aggregation = register(spec.ai.text_aggregation, "processor")
        if aggregation.text_io is None:
            raise ConfigValidationError("text aggregation requires a text→text processor")
        if aggregation.requires_host_capability:
            raise ConfigValidationError("text aggregation host capabilities are not declared")
    else:
        services = {"realtime": spec.ai.realtime}

    for role, service in services.items():
        manifest = register(service, "provider")
        service_caps = manifest.service_caps
        assert service_caps is not None
        if role not in service_caps.roles:
            raise ConfigValidationError(f"component '{service.id}' does not support role '{role}'")
        if service_caps.supported_models and service.model not in service_caps.supported_models:
            raise ConfigValidationError(f"component '{service.id}' model is not supported")
        authorities[service.id] = set(service_caps.authorities)
        for field, formats, modality in (
            ("input_audio", service_caps.audio_inputs, service_caps.input_modality),
            ("output_audio", service_caps.audio_outputs, service_caps.output_modality),
        ):
            selected = getattr(service, field)
            if modality == "audio" and (selected is None or selected not in formats):
                raise ConfigValidationError(
                    f"component '{service.id}' {field} must select a supported audio format"
                )
            if modality == "text" and selected is not None:
                raise ConfigValidationError(
                    f"component '{service.id}' has audio on a text boundary"
                )

    input_service = services.get("stt") or services["realtime"]
    output_service = services.get("tts") or services["realtime"]
    assert input_service.input_audio is not None and output_service.output_audio is not None

    for direction, stages, start, end in (
        ("input", spec.media.input_pipeline, caps.decoded_input, input_service.input_audio),
        ("output", spec.media.output_pipeline, output_service.output_audio, caps.decoded_output),
    ):
        current: AudioFormat = start
        for index, stage in enumerate(stages):
            manifest = register(stage, "processor")
            io = manifest.io
            if io is None:
                raise ConfigValidationError(f"PCM stage '{stage.id}' requires audio IO")
            if stage.io is not None and stage.io != io:
                raise ConfigValidationError(f"stage '{stage.id}' cannot replace manifest IO")
            validate_stage(stage, manifest)
            _validate_stage_contract(stage, manifest)
            if current != io.accepts:
                raise ConfigValidationError(
                    f"{direction} pipeline io mismatch before stage '{stage.id}'"
                )
            current = io.produces
            target = resolved["spec"]["media"][f"{direction}_pipeline"][index]
            target["io"] = io.model_dump(mode="json")
            for field in (
                "invariants_enforced", "invariants_applicable", "lossy_expected",
                "requires_host_capability",
            ):
                if field not in target:
                    target[field] = list(getattr(manifest, field))
        if current != end:
            raise ConfigValidationError(
                f"{direction} pipeline boundary format mismatch; add an explicit converter"
            )

    for decision, component_id in spec.turn_taking.model_dump(exclude_none=True).items():
        if decision not in authorities.get(component_id, set()):
            raise ConfigValidationError(
                f"turn authority '{component_id}' does not support '{decision}'"
            )

    resolved["manifest_pins"] = sorted(pins, key=lambda pin: pin["component_id"])
    return resolved


def _pin(component_id: str, manifest: CapabilityManifestV2) -> dict[str, str]:
    return {
        "component_id": component_id,
        "kind": manifest.kind,
        "name": manifest.name,
        "version": manifest.version,
        "digest": resolved_hash(manifest.model_dump(mode="json", exclude_none=True)),
    }


def _validate_stage_contract(stage: PipelineStageV2, manifest: CapabilityManifestV2) -> None:
    # A caller cannot remove actual host requirements or claim enforcement that
    # the pinned implementation does not advertise.
    targets = [override.target for override in stage.overrides]
    if len(targets) != len(set(targets)):
        raise ConfigValidationError(f"stage '{stage.id}' has duplicate override targets")
    if stage.requires_host_capability is not None and not set(
        manifest.requires_host_capability
    ).issubset(stage.requires_host_capability):
        raise ConfigValidationError(f"stage '{stage.id}' cannot remove host requirements")
    if stage.invariants_enforced is not None and not set(stage.invariants_enforced).issubset(
        manifest.invariants_enforced
    ):
        raise ConfigValidationError(f"stage '{stage.id}' cannot invent enforced invariants")
