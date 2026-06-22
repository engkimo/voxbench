from __future__ import annotations

from pathlib import Path

import pytest

from voxbench.registry.errors import ConfigValidationError
from voxbench.registry.service import RegistryService, load_json

ROOT = Path(__file__).resolve().parents[1]

MANIFESTS = [
    ROOT / "examples/manifests/engine/asterisk.json",
    ROOT / "examples/manifests/provider/gemini.json",
    ROOT / "examples/manifests/processor/resampler.json",
    ROOT / "examples/manifests/processor/agc.json",
    ROOT / "examples/manifests/processor/limiter.json",
    ROOT / "examples/manifests/processor/serializer.json",
]


def service_for(*config_names: str) -> RegistryService:
    service = RegistryService()
    for manifest in MANIFESTS:
        service.register_manifest(load_json(manifest))
    for config_name in config_names:
        service.register_config(load_json(ROOT / f"examples/configs/{config_name}.json"))
    return service


def test_valid_config_resolves_and_hashes() -> None:
    service = service_for("valid-baseline")

    resolved = service.resolve_config("baseline")

    assert resolved.name == "baseline"
    assert len(resolved.hash) == 64
    assert resolved.resolved["spec"]["ai"]["provider"] == "gemini"


@pytest.mark.parametrize(
    ("config_name", "expected_message"),
    [
        ("invalid-double-vad", "server/client VAD twice"),
        ("invalid-io-mismatch", "adjacent pipeline io mismatch"),
        ("invalid-missing-host-capability", "requires host capabilities"),
        ("invalid-override", "override target is not allowed"),
        ("invalid-turn-owner", "is not supported by provider"),
    ],
)
def test_invalid_configs_hard_fail(config_name: str, expected_message: str) -> None:
    service = service_for(config_name)

    with pytest.raises(ConfigValidationError, match=expected_message):
        service.resolve_config(config_name)


def test_parent_overlay_resolves_before_hashing() -> None:
    parent = load_json(ROOT / "examples/configs/valid-baseline.json")
    child = {
        "apiVersion": "voxbench/v1",
        "kind": "VoiceConfig",
        "meta": {
            "name": "overlay-child",
            "version": "1.0.1",
            "parent": "baseline",
            "labels": {"env": "test"},
        },
        "spec": {
            "ai": {
                "provider": "gemini",
                "model": "gemini-live-overlay",
                "params": {
                    "api_key_ref": "secret://gemini",
                    "response_modalities": ["audio"],
                },
            }
        },
    }
    service = RegistryService()
    for manifest in MANIFESTS:
        service.register_manifest(load_json(manifest))
    service.register_config(parent)
    service.register_config(child)

    resolved = service.resolve_config("overlay-child")

    assert resolved.resolved["spec"]["engine"]["kind"] == "asterisk"
    assert resolved.resolved["spec"]["ai"]["model"] == "gemini-live-overlay"
    assert len(resolved.hash) == 64

