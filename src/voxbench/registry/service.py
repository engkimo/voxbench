"""In-memory Phase 0 registry for manifests and voice configs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import SchemaError as JsonSchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from voxbench.registry.errors import (
    ConfigNotFoundError,
    ConfigValidationError,
    ManifestNotFoundError,
)
from voxbench.registry.hashing import resolved_hash
from voxbench.registry.merge import deep_merge
from voxbench.registry.resolved import ResolvedConfig
from voxbench.registry.v2 import resolve_v2
from voxbench.schemas import CapabilityManifest, IoContract, PipelineStage, VoiceConfig
from voxbench.schemas_v2 import CapabilityManifestV2, PipelineStageV2, VoiceConfigV2


class RegistryService:
    """Resolve config overlays and validate them against capability manifests."""

    def __init__(self) -> None:
        self._manifests: dict[tuple[str, str], CapabilityManifest] = {}
        self._v2_manifests: dict[tuple[str, str, str], CapabilityManifestV2] = {}
        self._configs: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_files(
        cls,
        *,
        config_paths: Iterable[str | Path],
        manifest_paths: Iterable[str | Path],
    ) -> RegistryService:
        service = cls()
        for manifest_path in manifest_paths:
            service.register_manifest(load_json(manifest_path))
        for config_path in config_paths:
            service.register_config(load_json(config_path))
        return service

    def register_manifest(self, raw: dict[str, Any]) -> CapabilityManifest | CapabilityManifestV2:
        try:
            manifest = (
                CapabilityManifestV2.model_validate(raw)
                if raw.get("apiVersion") == "voxbench/v2"
                else CapabilityManifest.model_validate(raw)
            )
            Draft202012Validator.check_schema(manifest.param_schema)
        except (PydanticValidationError, JsonSchemaValidationError, JsonSchemaError) as exc:
            raise ConfigValidationError(str(exc)) from exc

        if isinstance(manifest, CapabilityManifestV2):
            key = (manifest.kind, manifest.name, manifest.version)
            previous = self._v2_manifests.get(key)
            if previous is not None and previous != manifest:
                raise ConfigValidationError(
                    "v2 manifest identity is immutable; publish a new version"
                )
            self._v2_manifests[key] = manifest.model_copy(deep=True)
        else:
            self._manifests[(manifest.kind, manifest.name)] = manifest
        return manifest

    def register_config(self, raw: dict[str, Any]) -> None:
        meta = raw.get("meta")
        name = meta.get("name") if isinstance(meta, dict) else None
        if not isinstance(name, str) or not name:
            raise ConfigValidationError("config meta.name is required")
        self._configs[name] = deepcopy(raw) if raw.get("apiVersion") == "voxbench/v2" else raw

    def resolve_config(self, name: str) -> ResolvedConfig:
        raw = self._resolve_raw(name, seen=set())
        try:
            config = (
                VoiceConfigV2.model_validate(raw)
                if raw.get("apiVersion") == "voxbench/v2"
                else VoiceConfig.model_validate(raw)
            )
        except PydanticValidationError as exc:
            raise ConfigValidationError(str(exc)) from exc

        if isinstance(config, VoiceConfigV2):
            resolved = resolve_v2(
                config, lookup=self._v2_manifest, validate_params=self._validate_params,
                validate_stage=self._validate_v2_stage,
            )
        else:
            self._validate_cross_fields(config)
            resolved = config.model_dump(mode="json", exclude_none=True)
            self._materialize_manifest_defaults(resolved)
        return ResolvedConfig(
            name=config.meta.name,
            resolved=resolved,
            hash=resolved_hash(resolved),
        )

    def _resolve_raw(self, name: str, *, seen: set[str]) -> dict[str, Any]:
        if name in seen:
            raise ConfigValidationError(f"cyclic parent overlay detected for config '{name}'")
        try:
            raw = self._configs[name]
        except KeyError as exc:
            raise ConfigNotFoundError(f"unknown config '{name}'") from exc

        parent = raw.get("meta", {}).get("parent")
        if parent is None:
            return raw
        if not isinstance(parent, str):
            raise ConfigValidationError(f"config '{name}' meta.parent must be a string")

        base = self._resolve_raw(parent, seen=seen | {name})
        if "voxbench/v2" in (base.get("apiVersion"), raw.get("apiVersion")):
            if raw.get("apiVersion") != base.get("apiVersion"):
                raise ConfigValidationError("cross-version overlay inheritance is forbidden")
            base_spec, overlay_spec = base.get("spec"), raw.get("spec", {})
            if not isinstance(base_spec, dict) or not isinstance(overlay_spec, dict):
                raise ConfigValidationError("v2 overlay spec must be an object")
            base_ai, overlay_ai = base_spec.get("ai"), overlay_spec.get("ai", {})
            if not isinstance(base_ai, dict) or not isinstance(overlay_ai, dict):
                raise ConfigValidationError("v2 overlay ai must be an object")
            base_mode = base_ai.get("mode")
            overlay_mode = overlay_ai.get("mode", base_mode)
            if overlay_mode != base_mode:
                raise ConfigValidationError("cross-mode overlay inheritance is forbidden")
        return deep_merge(base, raw)

    def _validate_cross_fields(self, config: VoiceConfig) -> None:
        engine_manifest = self._manifest("engine", config.spec.engine.kind)
        self._validate_params("engine", engine_manifest, config.spec.engine.params)

        provider_manifest = self._manifest("provider", config.spec.ai.provider)
        self._validate_params("provider", provider_manifest, config.spec.ai.params)
        self._validate_provider_caps(config, provider_manifest)

        self._validate_pipeline(config)

    def _validate_pipeline(self, config: VoiceConfig) -> None:
        effective_ios: list[IoContract | None] = []

        for stage in config.spec.media.pipeline:
            manifest = self._manifest("processor", stage.plugin)
            self._validate_params(f"processor '{stage.plugin}'", manifest, stage.params)
            self._validate_host_capabilities(stage, manifest)
            self._validate_overrides(stage, manifest)
            effective_ios.append(stage.io or manifest.io)

        for index, (left, right) in enumerate(pairwise(effective_ios)):
            if left is None or right is None or left.produces is None:
                continue
            mismatches = {
                key: (left.produces[key], right.accepts[key])
                for key in left.produces.keys() & right.accepts.keys()
                if left.produces[key] != right.accepts[key]
            }
            if mismatches:
                raise ConfigValidationError(
                    f"adjacent pipeline io mismatch after stage {index}: {mismatches}"
                )

    def _validate_provider_caps(
        self,
        config: VoiceConfig,
        provider_manifest: CapabilityManifest,
    ) -> None:
        caps = provider_manifest.provider_caps
        if caps is None:
            raise ConfigValidationError(
                f"provider '{provider_manifest.name}' manifest must declare provider_caps"
            )

        owner = config.spec.turn_taking.owner
        if owner not in caps.turn_taking_owners:
            raise ConfigValidationError(
                f"turn_taking.owner '{owner}' is not supported by provider "
                f"'{provider_manifest.name}'"
            )

        if owner == "server_vad" and config.spec.turn_taking.detector is not None:
            raise ConfigValidationError(
                "turn_taking.detector with owner=server_vad configures server/client VAD twice"
            )

        codec = config.spec.transport.codec
        if caps.supported_codecs and codec not in caps.supported_codecs:
            raise ConfigValidationError(
                f"transport.codec '{codec}' is not supported by provider '{provider_manifest.name}'"
            )

    def _validate_host_capabilities(
        self,
        stage: PipelineStage | PipelineStageV2,
        manifest: CapabilityManifest | CapabilityManifestV2,
    ) -> None:
        required = set(stage.requires_host_capability or manifest.requires_host_capability)
        available = set(stage.host_capabilities)
        missing = sorted(required - available)
        if missing:
            raise ConfigValidationError(
                f"stage '{stage.plugin}' requires host capabilities not present at its position: "
                f"{missing}"
            )

    def _validate_overrides(
        self,
        stage: PipelineStage | PipelineStageV2,
        manifest: CapabilityManifest | CapabilityManifestV2,
    ) -> None:
        allowed = {override.target: override.type for override in manifest.allowed_overrides}
        for override in stage.overrides:
            if override.target not in allowed:
                raise ConfigValidationError(
                    f"stage '{stage.plugin}' override target is not allowed: {override.target}"
                )
            if not _matches_override_type(override.value, allowed[override.target]):
                raise ConfigValidationError(
                    f"stage '{stage.plugin}' override target '{override.target}' has invalid type"
                )

    def _validate_params(
        self,
        label: str,
        manifest: CapabilityManifest | CapabilityManifestV2,
        params: dict[str, Any],
    ) -> None:
        try:
            Draft202012Validator(manifest.param_schema).validate(params)
        except JsonSchemaValidationError as exc:
            raise ConfigValidationError(
                f"{label} params do not match param_schema: {exc.message}"
            ) from exc

    def _materialize_manifest_defaults(self, resolved: dict[str, Any]) -> None:
        for stage in resolved["spec"]["media"]["pipeline"]:
            manifest = self._manifest("processor", stage["plugin"])
            if "io" not in stage and manifest.io is not None:
                stage["io"] = manifest.io.model_dump(mode="json", exclude_none=True)
            for field_name in (
                "invariants_enforced",
                "invariants_applicable",
                "lossy_expected",
                "requires_host_capability",
            ):
                if field_name not in stage:
                    value = getattr(manifest, field_name)
                    if value:
                        stage[field_name] = list(value)

    def _manifest(self, kind: str, name: str) -> CapabilityManifest:
        try:
            return self._manifests[(kind, name)]
        except KeyError as exc:
            raise ManifestNotFoundError(f"unknown {kind} manifest '{name}'") from exc

    def _v2_manifest(self, kind: str, name: str, version: str) -> CapabilityManifestV2:
        try:
            return self._v2_manifests[(kind, name, version)]
        except KeyError as exc:
            raise ManifestNotFoundError(
                f"unknown v2 {kind} manifest '{name}' version '{version}'"
            ) from exc

    def _validate_v2_stage(
        self, stage: PipelineStageV2, manifest: CapabilityManifestV2,
    ) -> None:
        self._validate_host_capabilities(stage, manifest)
        self._validate_overrides(stage, manifest)


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ConfigValidationError(f"{path} must contain a JSON object")
    return value


def _matches_override_type(value: Any, expected: str) -> bool:
    match expected:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number" | "float":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case _:
            return False
