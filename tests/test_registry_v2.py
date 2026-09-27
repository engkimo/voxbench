from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from voxbench.control_plane.app import create_app
from voxbench.control_plane.models import Base
from voxbench.control_plane.run_api import PostgresRunRepository, StoredRun
from voxbench.engine_harness.harness import EngineHarness
from voxbench.engine_harness.plan import build_stage_plan
from voxbench.engine_harness.storage import LocalRecordingSink
from voxbench.registry.config_views import ai_mode, pipeline_chains, service_components
from voxbench.registry.errors import ConfigValidationError, ManifestNotFoundError
from voxbench.registry.hashing import canonical_json, resolved_hash
from voxbench.registry.service import RegistryService, load_json
from voxbench.schemas_export import export_schemas
from voxbench.synthetic_caller import SyntheticAudioSpec
from voxbench.synthetic_caller.offline import generate_synthetic_artifacts
from voxbench.verification import verify_recordings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/v2"


def config_for(mode: str = "cascade") -> dict:
    return load_json(EXAMPLES / f"configs/{mode}.json")


def manifests_for() -> list[dict]:
    return [load_json(path) for path in sorted((EXAMPLES / "manifests").glob("*.json"))]


def registry_for(config: dict, manifests: list[dict] | None = None) -> RegistryService:
    registry = RegistryService()
    for manifest in manifests if manifests is not None else manifests_for():
        registry.register_manifest(manifest)
    registry.register_config(config)
    return registry


def resolve(config: dict):
    return registry_for(config).resolve_config(config["meta"]["name"])


def set_path(config: dict, path: str, value: object) -> None:
    keys = path.split(".")
    target = config
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    key = keys[-1]
    target[int(key) if isinstance(target, list) else key] = value


@pytest.mark.parametrize("mode", ["realtime", "cascade"])
def test_v2_resolves_independent_chains_and_pinned_services(mode: str) -> None:
    config = config_for(mode)
    before = deepcopy(config)
    result = resolve(config)
    roles = {"realtime"} if mode == "realtime" else {"stt", "llm", "tts"}

    assert config == before
    assert ai_mode(result.resolved) == mode
    assert set(service_components(result.resolved)) == roles
    chains = pipeline_chains(result.resolved)
    assert chains["input"][0]["id"] == "input-resampler"
    assert chains["output"][0]["id"] == "output-resampler"
    assert chains["input"][0]["io"]["produces"]["rate"] == 16000
    assert chains["output"][0]["io"]["produces"]["rate"] == 8000
    assert all(stage["type"] == "resampler" for stages in chains.values() for stage in stages)
    pins = result.resolved["manifest_pins"]
    expected_ids = {"engine", "input-resampler", "output-resampler"}
    expected_ids |= {f"{role}-main" for role in roles}
    if mode == "cascade":
        expected_ids.add("aggregation-main")
    assert {pin["component_id"] for pin in pins} == expected_ids
    assert all(pin["version"] == "1.0.0" and len(pin["digest"]) == 64 for pin in pins)
    assert result.hash == resolve(deepcopy(config)).hash


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("spec.ai.tts.id", "stt-main", "duplicate or reserved"),
        ("spec.media.output_pipeline.0.id", "input-resampler", "duplicate or reserved"),
        ("spec.ai.stt.id", "engine", "duplicate or reserved"),
        ("spec.ai.text_aggregation.id", "application", "duplicate or reserved"),
        ("spec.ai.stt.plugin", "example-tts", "does not support role"),
        ("spec.ai.text_aggregation.plugin", "example-input-resampler", "text aggregation"),
        ("spec.media.input_pipeline.0.plugin", "example-sentence-aggregator", "requires audio IO"),
        ("spec.ai.stt.model", "not-declared", "model is not supported"),
        ("spec.ai.stt.input_audio.rate", 44100, "supported audio format"),
        ("spec.ai.llm.input_audio", {"encoding": "pcm16", "rate": 8000, "channels": 1},
         "audio on a text boundary"),
        ("spec.ai.stt.output_audio", {"encoding": "pcm16", "rate": 8000, "channels": 1},
         "audio on a text boundary"),
        ("spec.ai.stt.input_audio.rate", 0, "greater than 0"),
        ("spec.ai.stt.input_audio.channels", True, "valid integer"),
        ("spec.ai.stt.params", {"unknown": True}, "param_schema"),
        ("spec.ai.stt.system_prompt_ref", "unexpected", "Extra inputs"),
        ("spec.ai.stt.manifest_version", "9.0.0", "unknown v2 provider manifest"),
        ("spec.transport.codec", "unsupported", "engine decode contract"),
        ("spec.transport.ptime_ms", 0, "ptime_ms must be positive"),
        ("spec.media.input_pipeline", [], "input pipeline boundary format mismatch"),
        ("spec.media.output_pipeline", [], "output pipeline boundary format mismatch"),
        ("spec.media.input_pipeline.0.plugin", "example-output-resampler", "input pipeline io"),
        ("spec.media.output_pipeline.0.plugin", "example-input-resampler", "output pipeline io"),
        ("spec.media.input_pipeline.0.host_capabilities", [], "requires host capabilities"),
        ("spec.media.input_pipeline.0.requires_host_capability", [], "cannot remove host"),
        ("spec.media.input_pipeline.0.invariants_enforced", ["level_preserving"], "cannot invent"),
        ("spec.media.input_pipeline.0.overrides", [{"target": "undeclared", "value": 1}],
         "override target is not allowed"),
        ("spec.media.input_pipeline.0.overrides", [{"target": "quality", "value": 1}],
         "invalid type"),
        ("spec.media.input_pipeline.0.overrides", [
            {"target": "quality", "value": "high"}, {"target": "quality", "value": "low"},
        ], "duplicate override targets"),
        ("spec.turn_taking.speech_detector", "tts-main", "turn authority"),
        ("spec.turn_taking.end_of_turn", "llm-main", "turn authority"),
        ("spec.turn_taking.interruption", "missing", "turn authority"),
        ("spec.turn_taking.end_of_turn", ["application", "stt-main"], "valid string"),
        ("spec.turn_taking.owner", "server_vad", "Extra inputs"),
        ("spec.ai.provider", "legacy-provider", "Extra inputs"),
    ],
)
def test_invalid_v2_configs_fail(path: str, value: object, message: str) -> None:
    config = config_for()
    set_path(config, path, value)
    with pytest.raises((ConfigValidationError, ManifestNotFoundError), match=message):
        resolve(config)


def test_audio_stage_cannot_replace_pinned_manifest_io() -> None:
    config = config_for()
    config["spec"]["media"]["input_pipeline"][0]["io"] = {
        "mode": "rate_changing",
        "accepts": {"encoding": "pcm16", "rate": 8000, "channels": 1},
        "produces": {"encoding": "pcm16", "rate": 44100, "channels": 1},
    }
    with pytest.raises(ConfigValidationError, match="cannot replace manifest IO"):
        resolve(config)


def test_empty_chains_are_valid_only_for_equal_service_engine_formats() -> None:
    config = config_for()
    manifests = manifests_for()
    for manifest in manifests:
        if manifest["name"] == "example-telephony":
            caps = manifest["engine_caps"]
            caps["decoded_input"]["rate"] = 16000
            caps["decoded_output"]["rate"] = 24000
    config["spec"]["media"] = {"input_pipeline": [], "output_pipeline": []}
    registry = registry_for(config, manifests)
    assert pipeline_chains(registry.resolve_config("example-cascade").resolved) == {
        "input": [], "output": [],
    }


@pytest.mark.parametrize("mode", ["cascade", "realtime"])
def test_declared_service_can_own_separate_turn_decisions(mode: str) -> None:
    config = config_for(mode)
    authority = "stt-main" if mode == "cascade" else "realtime-main"
    config["spec"]["turn_taking"] = {
        "speech_detector": authority, "end_of_turn": authority, "interruption": authority,
    }
    assert resolve(config).resolved["spec"]["turn_taking"]["end_of_turn"] == authority


def test_engine_authority_requires_engine_capability_declaration() -> None:
    config = config_for()
    config["spec"]["turn_taking"]["interruption"] = "engine"
    with pytest.raises(ConfigValidationError, match="turn authority"):
        resolve(config)
    manifests = manifests_for()
    next(m for m in manifests if m["kind"] == "engine")["engine_caps"]["authorities"] = [
        "interruption"
    ]
    assert registry_for(config, manifests).resolve_config("example-cascade")


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("service_caps.output_modality", "audio", "modality mismatch"),
        ("service_caps.audio_inputs", [], "audio boundaries require formats"),
        ("service_caps.audio_outputs", [{"encoding": "pcm16", "rate": 8000, "channels": 1}],
         "text boundaries forbid them"),
        ("service_caps.roles", ["stt", "stt"], "roles must be unique"),
        ("provider_caps", {}, "Extra inputs"),
        ("kind", "engine", "engine manifest"),
    ],
)
def test_malformed_service_manifests_fail(path: str, value: object, message: str) -> None:
    manifest = load_json(EXAMPLES / "manifests/stt.json")
    set_path(manifest, path, value)
    with pytest.raises(ConfigValidationError, match=message):
        RegistryService().register_manifest(manifest)


def test_text_aggregation_cannot_claim_audio_invariants() -> None:
    manifest = load_json(EXAMPLES / "manifests/aggregation.json")
    manifest["invariants_applicable"] = ["duration_preserving"]
    with pytest.raises(ConfigValidationError, match="text processors cannot declare audio"):
        RegistryService().register_manifest(manifest)


def test_tts_manifest_cannot_claim_caller_authority() -> None:
    manifest = load_json(EXAMPLES / "manifests/tts.json")
    manifest["service_caps"]["authorities"] = ["speech_detector"]
    with pytest.raises(ConfigValidationError, match="TTS cannot own"):
        RegistryService().register_manifest(manifest)


def test_invalid_plugin_parameter_schema_is_a_registry_error() -> None:
    manifest = load_json(EXAMPLES / "manifests/stt.json")
    manifest["param_schema"] = {"type": "invalid-json-schema-type"}
    with pytest.raises(ConfigValidationError):
        RegistryService().register_manifest(manifest)


def test_ambiguous_allowed_overrides_are_rejected() -> None:
    manifest = load_json(EXAMPLES / "manifests/input-resampler.json")
    manifest["allowed_overrides"].append({"target": "quality", "type": "integer"})
    with pytest.raises(ConfigValidationError, match="override targets must be unique"):
        RegistryService().register_manifest(manifest)


def test_manifest_identity_is_immutable_but_multiple_versions_are_selectable() -> None:
    config = config_for()
    registry = registry_for(config)
    original = registry.resolve_config("example-cascade")
    manifest = load_json(EXAMPLES / "manifests/stt.json")
    assert registry.register_manifest(deepcopy(manifest))
    manifest["service_caps"]["lifecycle"]["usage"] = True
    with pytest.raises(ConfigValidationError, match="identity is immutable"):
        registry.register_manifest(manifest)
    manifest["version"] = "1.1.0"
    registry.register_manifest(manifest)
    assert registry.resolve_config("example-cascade").hash == original.hash
    config["spec"]["ai"]["stt"]["manifest_version"] = "1.1.0"
    registry.register_config(config)
    updated = registry.resolve_config("example-cascade")
    assert updated.hash != original.hash
    pin = next(p for p in updated.resolved["manifest_pins"] if p["component_id"] == "stt-main")
    assert pin["version"] == "1.1.0"
    assert pin["digest"] == resolved_hash(registry.register_manifest(manifest).model_dump(
        mode="json", exclude_none=True,
    ))


def test_mutating_registration_result_does_not_change_pinned_manifest() -> None:
    config = config_for()
    registry = registry_for(config)
    original = registry.resolve_config("example-cascade")
    manifest = registry.register_manifest(load_json(EXAMPLES / "manifests/stt.json"))
    manifest.service_caps.lifecycle.usage = True
    assert registry.resolve_config("example-cascade").hash == original.hash


def test_registered_v2_config_is_a_snapshot_of_the_supplied_object() -> None:
    config = config_for()
    registry = registry_for(config)
    original = registry.resolve_config("example-cascade")
    config["spec"]["ai"]["stt"]["model"] = "unregistered-change"
    assert registry.resolve_config("example-cascade").hash == original.hash


def test_v1_and_v2_provider_names_coexist_without_changing_legacy_resolution() -> None:
    registry = RegistryService.from_files(
        config_paths=[ROOT / "examples/configs/valid-baseline.json"],
        manifest_paths=(ROOT / "examples/manifests").rglob("*.json"),
    )
    original = registry.resolve_config("baseline")
    config = config_for()
    config["spec"]["ai"]["stt"]["plugin"] = "gemini"
    for manifest in manifests_for():
        if manifest["name"] == "example-stt":
            manifest["name"] = "gemini"
        registry.register_manifest(manifest)
    registry.register_config(config)
    assert registry.resolve_config("example-cascade").resolved["spec"]["ai"]["stt"]["plugin"] == (
        "gemini"
    )
    assert registry.resolve_config("baseline") == original


@pytest.mark.parametrize("role", ["stt", "llm", "tts"])
def test_same_mode_overlay_changes_only_selected_model(role: str) -> None:
    config = config_for()
    manifests = manifests_for()
    manifest = next(m for m in manifests if m["name"] == f"example-{role}")
    manifest["service_caps"]["supported_models"].append("pinned-alternative-model")
    registry = registry_for(config, manifests)
    base = registry.resolve_config("example-cascade")
    child = {
        "apiVersion": "voxbench/v2", "kind": "VoiceConfig",
        "meta": {"name": "child", "version": "1.1.0", "parent": "example-cascade"},
        "spec": {"ai": {role: {"model": "pinned-alternative-model"}}},
    }
    registry.register_config(child)
    result = registry.resolve_config("child")
    assert result.resolved["spec"]["ai"][role]["model"] == "pinned-alternative-model"
    for other in {"stt", "llm", "tts"} - {role}:
        assert result.resolved["spec"]["ai"][other] == base.resolved["spec"]["ai"][other]
    assert result.hash != base.hash
    assert registry.resolve_config("child").hash == result.hash


@pytest.mark.parametrize(
    ("base_version", "child_version", "child_mode", "message"),
    [
        ("voxbench/v2", "voxbench/v1", None, "cross-version"),
        ("voxbench/v1", "voxbench/v2", "cascade", "cross-version"),
        ("voxbench/v2", "voxbench/v2", "realtime", "cross-mode"),
    ],
)
def test_overlay_cannot_cross_version_or_mode(
    base_version: str, child_version: str, child_mode: str | None, message: str,
) -> None:
    config = config_for()
    config["apiVersion"] = base_version
    registry = registry_for(config)
    child = {
        "apiVersion": child_version, "meta": {"name": "child", "parent": "example-cascade"},
        "spec": {"ai": {"mode": child_mode}} if child_mode else {},
    }
    registry.register_config(child)
    with pytest.raises(ConfigValidationError, match=message):
        registry.resolve_config("child")


@pytest.mark.parametrize("spec", [None, [], {"ai": None}, {"ai": []}])
def test_malformed_overlay_shapes_fail_as_registry_errors(spec: object) -> None:
    registry = registry_for(config_for())
    registry.register_config({
        "apiVersion": "voxbench/v2", "kind": "VoiceConfig",
        "meta": {"name": "child", "version": "1.1.0", "parent": "example-cascade"},
        "spec": spec,
    })
    with pytest.raises(ConfigValidationError, match="v2 overlay"):
        registry.resolve_config("child")


def test_exported_v1_schemas_stay_identical_and_v2_examples_validate(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    for filename in ("config.schema.json", "manifest.schema.json"):
        assert (tmp_path / filename).read_bytes() == (ROOT / "schemas" / filename).read_bytes()
    config_schema = load_json(tmp_path / "config.v2.schema.json")
    manifest_schema = load_json(tmp_path / "manifest.v2.schema.json")
    for schema in (config_schema, manifest_schema):
        Draft202012Validator.check_schema(schema)
    for mode in ("cascade", "realtime"):
        Draft202012Validator(config_schema).validate(config_for(mode))
    for manifest in manifests_for():
        Draft202012Validator(manifest_schema).validate(manifest)


def test_v2_core_resolution_does_not_import_framework_sdks() -> None:
    code = """
import importlib.abc, sys
class BlockFrameworks(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'pipecat', 'livekit'}:
            raise AssertionError('core imported middleware: ' + fullname)
sys.meta_path.insert(0, BlockFrameworks())
from pathlib import Path
from voxbench.registry.service import RegistryService
for mode in ('cascade', 'realtime'):
    r = RegistryService.from_files(
        config_paths=[f'examples/v2/configs/{mode}.json'],
        manifest_paths=Path('examples/v2/manifests').glob('*.json'),
    ).resolve_config(f'example-{mode}')
    assert len(r.hash) == 64
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["cascade", "realtime"])
@pytest.mark.parametrize("endpoint", ["/runs", "/runs/async"])
def test_v2_run_api_fails_explicitly_until_observation_execution_support(
    mode: str, endpoint: str, tmp_path: Path,
) -> None:
    config = config_for(mode)
    artifact_root = tmp_path / "recordings"
    client = TestClient(create_app(artifact_root=artifact_root))
    response = client.post(endpoint, json={
        "config_name": config["meta"]["name"], "configs": [config], "manifests": manifests_for(),
    })
    assert response.status_code == 400
    assert "configuration resolution only" in response.json()["detail"]
    assert client.get("/runs").json() == []
    assert not artifact_root.exists()


@pytest.mark.parametrize("mode", ["cascade", "realtime"])
def test_v2_observation_run_can_start_without_executing_audio(
    mode: str, tmp_path: Path,
) -> None:
    config = config_for(mode)
    artifact_root = tmp_path / "recordings"
    response = TestClient(create_app(artifact_root=artifact_root)).post(
        "/runs/observed",
        json={
            "config_name": config["meta"]["name"],
            "configs": [config],
            "manifests": manifests_for(),
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert not artifact_root.exists()


def test_v2_legacy_audio_tools_fail_before_writing_artifacts(tmp_path: Path) -> None:
    config = resolve(config_for()).resolved
    root = tmp_path / "artifacts"
    with pytest.raises(ConfigValidationError, match="stage planning"):
        build_stage_plan(config)
    harness = EngineHarness(recording_sink=LocalRecordingSink(root))
    with pytest.raises(ConfigValidationError, match="legacy harness execution"):
        harness.run_once(run_id="test", resolved_config=config, config_hash=resolved_hash(config))
    with pytest.raises(ConfigValidationError, match="synthetic audio generation"):
        generate_synthetic_artifacts(
            resolved_config=config, output_root=root,
            audio_spec=SyntheticAudioSpec(8000, 1, 0.1, 1000, 440),
        )
    with pytest.raises(ConfigValidationError, match="recording verification"):
        verify_recordings(resolved_config=config, recordings=[])
    assert not root.exists()


@pytest.mark.parametrize("overlay", [False, True])
def test_v1_resolved_bytes_hash_and_read_views_remain_unchanged(overlay: bool) -> None:
    fixture = "v1-overlay" if overlay else "v1-baseline"
    golden = (ROOT / f"tests/fixtures/configs/{fixture}.resolved.json").read_text().strip()
    registry = RegistryService.from_files(
        config_paths=[ROOT / "examples/configs/valid-baseline.json"],
        manifest_paths=(ROOT / "examples/manifests").rglob("*.json"),
    )
    if overlay:
        registry.register_config({
            "apiVersion": "voxbench/v1", "kind": "VoiceConfig",
            "meta": {"name": "overlay-golden", "version": "1.0.1", "parent": "baseline"},
            "spec": {"ai": {"model": "pinned-overlay-model"}},
        })
    result = registry.resolve_config("overlay-golden" if overlay else "baseline")
    assert canonical_json(result.resolved) == golden
    expected_hash = (
        "6e0d5937a5fcf3e94cc3581533246ecee81e1b2c4528d896ca830173f176bdfa"
        if overlay else "6b3467de65af01c527f2dfd95e260fe734e357f8acc392916b1d86567199bde1"
    )
    assert result.hash == expected_hash
    assert ai_mode(result.resolved) == "realtime"
    assert service_components(result.resolved)["realtime"]["plugin"] == "gemini"
    assert list(pipeline_chains(result.resolved)) == ["legacy"]
    service_components(result.resolved)["realtime"]["params"]["example"] = "view-only"
    pipeline_chains(result.resolved)["legacy"][0]["params"]["input_rate"] = 1234
    assert canonical_json(result.resolved) == golden


def test_v2_read_views_cannot_mutate_the_resolved_config() -> None:
    config = resolve(config_for()).resolved
    before = canonical_json(config)
    service_components(config)["stt"]["input_audio"]["rate"] = 1234
    pipeline_chains(config)["input"][0]["io"]["accepts"]["rate"] = 1234
    assert canonical_json(config) == before


def test_stored_v1_config_loads_without_v2_defaults() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = PostgresRunRepository(sessions)
    golden = (ROOT / "tests/fixtures/configs/v1-baseline.resolved.json").read_text().strip()
    config = json.loads(golden)
    run = StoredRun(
        run_id=str(uuid4()), config_hash=resolved_hash(config), call_id=None,
        conversation_id="historical-v1", provider="gemini", engine="asterisk",
        status="completed", started_at=datetime.now(UTC), ended_at=datetime.now(UTC),
        resolved_config=config, recordings=[], spans=[], metrics=[],
    )
    repository.save(run)
    restored = PostgresRunRepository(sessions).get(run.run_id)
    assert restored is not None
    assert canonical_json(restored.resolved_config) == golden
    assert restored.config_hash == run.config_hash
    assert ai_mode(restored.resolved_config) == "realtime"
    assert canonical_json(restored.resolved_config) == golden
    engine.dispose()
