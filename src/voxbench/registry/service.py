"""In-memory Phase 0 registry for manifests and voice configs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
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
from voxbench.schemas import CapabilityManifest, IoContract, PipelineStage, VoiceConfig


class RegistryService:
    """Resolve config overlays and validate them against capability manifests."""

    def __init__(self) -> None:
        self._manifests: dict[tuple[str, str], CapabilityManifest] = {}
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

    def register_manifest(self, raw: dict[str, Any]) -> CapabilityManifest:
        try:
            manifest = CapabilityManifest.model_validate(raw)
            Draft202012Validator.check_schema(manifest.param_schema)
        except (PydanticValidationError, JsonSchemaValidationError) as exc:
            raise ConfigValidationError(str(exc)) from exc

        self._manifests[(manifest.kind, manifest.name)] = manifest
        return manifest

    def register_config(self, raw: dict[str, Any]) -> None:
        name = raw.get("meta", {}).get("name")
        if not isinstance(name, str) or not name:
            raise ConfigValidationError("config meta.name is required")
        self._configs[name] = raw

    def resolve_config(self, name: str) -> ResolvedConfig:
        raw = self._resolve_raw(name, seen=set())
        try:
            config = VoiceConfig.model_validate(raw)
        except PydanticValidationError as exc:
            raise ConfigValidationError(str(exc)) from exc

        self._validate_cross_fields(config)
        resolved = config.model_dump(mode="json", exclude_none=True)
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
        stage: PipelineStage,
        manifest: CapabilityManifest,
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
        stage: PipelineStage,
        manifest: CapabilityManifest,
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
        manifest: CapabilityManifest,
        params: dict[str, Any],
    ) -> None:
        try:
            Draft202012Validator(manifest.param_schema).validate(params)
        except JsonSchemaValidationError as exc:
            raise ConfigValidationError(
                f"{label} params do not match param_schema: {exc.message}"
            ) from exc

    def _manifest(self, kind: str, name: str) -> CapabilityManifest:
        try:
            return self._manifests[(kind, name)]
        except KeyError as exc:
            raise ManifestNotFoundError(f"unknown {kind} manifest '{name}'") from exc


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
