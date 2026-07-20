from __future__ import annotations

from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voxbench.control_plane.app import create_app, create_app_from_env
from voxbench.control_plane.run_api import RunCreateRequest
from voxbench.control_plane.storage_config import (
    StorageConfigurationError,
    build_recording_sink_from_env,
    build_recording_storage_from_env,
)
from voxbench.engine_harness.storage import MinioRecordingReader, MinioRecordingSink


class FakeMinioClient:
    def __init__(self, *, bucket_exists: bool = True) -> None:
        self.bucket_exists_result = bucket_exists
        self.bucket_exists_calls: list[str] = []

    def fput_object(self, **kwargs: Any) -> object:
        return object()

    def bucket_exists(self, bucket_name: str) -> bool:
        self.bucket_exists_calls.append(bucket_name)
        return self.bucket_exists_result

    def get_object(self, **kwargs: Any):
        raise AssertionError("remote object read was not expected")


class CapturingClientFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.client = FakeMinioClient()

    def __call__(self, **kwargs: object) -> FakeMinioClient:
        self.calls.append(kwargs)
        return self.client


def _minio_env() -> dict[str, str]:
    return {
        "VOXBENCH_RECORDING_SINK": "minio",
        "VOXBENCH_MINIO_ENDPOINT": "minio.internal:9000",
        "VOXBENCH_MINIO_ACCESS_KEY": "private-access-key",
        "VOXBENCH_MINIO_SECRET_KEY": "token=private-secret",
        "VOXBENCH_MINIO_BUCKET": "voxbench-recordings",
        "VOXBENCH_MINIO_PREFIX": "stage-taps/v1",
        "VOXBENCH_MINIO_SECURE": "false",
    }


def test_default_environment_selects_local_storage() -> None:
    sink, readiness = build_recording_sink_from_env({})

    assert sink is None
    assert readiness.mode == "local"
    assert readiness.state == "ready"
    assert readiness.bucket_alias is None
    assert readiness.prefix_alias is None
    assert readiness.secure is None
    assert readiness.reason_alias is None
    assert readiness.remote_audio_proxy_enabled is False


def test_run_payload_cannot_select_or_configure_storage() -> None:
    with pytest.raises(ValidationError) as caught:
        RunCreateRequest.model_validate(
            {
                "config_name": "baseline",
                "configs": [],
                "manifests": [],
                "storage": {
                    "endpoint": "https://private.invalid",
                    "secret_key": "token=private-secret",
                },
            }
        )

    assert caught.value.error_count() == 1
    assert caught.value.errors(include_url=False)[0]["type"] == "extra_forbidden"


def test_minio_environment_builds_sink_and_credential_free_readiness() -> None:
    factory = CapturingClientFactory()

    sink, readiness = build_recording_sink_from_env(
        _minio_env(),
        client_factory=factory,
    )

    assert isinstance(sink, MinioRecordingSink)
    assert factory.calls == [
        {
            "endpoint": "minio.internal:9000",
            "access_key": "private-access-key",
            "secret_key": "token=private-secret",
            "secure": False,
        }
    ]
    assert readiness.mode == "minio"
    assert readiness.state == "configured"
    assert readiness.bucket_alias == "voxbench-recordings"
    assert readiness.prefix_alias == "stage-taps/v1"
    assert readiness.secure is False
    assert readiness.reason_alias == "connectivity-not-checked"
    serialized = repr(readiness)
    assert "minio.internal" not in serialized
    assert "private-access-key" not in serialized
    assert "private-secret" not in serialized
    assert factory.client.bucket_exists_calls == []


def test_remote_audio_proxy_requires_process_token_and_omits_it_from_repr() -> None:
    environment = _minio_env()
    environment["VOXBENCH_REMOTE_AUDIO_PROXY"] = "true"
    environment["VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN"] = "private-token-" + "a" * 32

    runtime = build_recording_storage_from_env(
        environment,
        client_factory=CapturingClientFactory(),
    )

    assert isinstance(runtime.recording_sink, MinioRecordingSink)
    assert isinstance(runtime.remote_recording_reader, MinioRecordingReader)
    assert runtime.remote_audio_access_token == "private-token-" + "a" * 32
    assert runtime.readiness.remote_audio_proxy_enabled is True
    assert "private-token" not in repr(runtime)


def test_remote_audio_proxy_missing_token_uses_safe_error() -> None:
    environment = _minio_env()
    environment["VOXBENCH_REMOTE_AUDIO_PROXY"] = "true"
    environment["VOXBENCH_MINIO_PROBE_BUCKET"] = "true"
    factory = CapturingClientFactory()

    with pytest.raises(StorageConfigurationError) as caught:
        build_recording_storage_from_env(
            environment,
            client_factory=factory,
        )

    assert caught.value.reason_alias == "remote-audio-token-missing"
    assert factory.calls == []
    assert factory.client.bucket_exists_calls == []


def test_remote_audio_proxy_rejects_excessive_inflight_capacity_before_probe() -> None:
    environment = _minio_env()
    environment["VOXBENCH_REMOTE_AUDIO_PROXY"] = "true"
    environment["VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN"] = "a" * 32
    environment["VOXBENCH_REMOTE_AUDIO_MAX_BYTES"] = str(64 * 1024 * 1024)
    environment["VOXBENCH_REMOTE_AUDIO_MAX_CONCURRENT"] = "3"
    environment["VOXBENCH_MINIO_PROBE_BUCKET"] = "true"
    factory = CapturingClientFactory()

    with pytest.raises(StorageConfigurationError) as caught:
        build_recording_storage_from_env(environment, client_factory=factory)

    assert caught.value.reason_alias == "remote-audio-capacity-invalid"
    assert factory.calls == []
    assert factory.client.bucket_exists_calls == []


@pytest.mark.parametrize(
    ("bucket_exists", "state", "reason_alias"),
    [
        (True, "ready", None),
        (False, "unavailable", "bucket-not-found"),
    ],
)
def test_opt_in_bucket_probe_reports_safe_readiness(
    bucket_exists: bool,
    state: str,
    reason_alias: str | None,
) -> None:
    environment = _minio_env()
    environment["VOXBENCH_MINIO_PROBE_BUCKET"] = "true"
    factory = CapturingClientFactory()
    factory.client = FakeMinioClient(bucket_exists=bucket_exists)

    _, readiness = build_recording_sink_from_env(
        environment,
        client_factory=factory,
    )

    assert readiness.state == state
    assert readiness.reason_alias == reason_alias
    assert factory.client.bucket_exists_calls == ["voxbench-recordings"]


def test_bucket_probe_failure_discards_raw_error() -> None:
    class FailingProbeClient(FakeMinioClient):
        def bucket_exists(self, bucket_name: str) -> bool:
            raise RuntimeError("token=private-secret endpoint=https://private.invalid")

    environment = _minio_env()
    environment["VOXBENCH_MINIO_PROBE_BUCKET"] = "true"
    factory = CapturingClientFactory()
    factory.client = FailingProbeClient()

    _, readiness = build_recording_sink_from_env(
        environment,
        client_factory=factory,
    )

    assert readiness.state == "unavailable"
    assert readiness.reason_alias == "bucket-probe-failed"
    assert "private-secret" not in repr(readiness)
    assert "private.invalid" not in repr(readiness)


def test_bucket_probe_timeout_is_bounded_and_safe() -> None:
    release = Event()

    class BlockingProbeClient(FakeMinioClient):
        def bucket_exists(self, bucket_name: str) -> bool:
            release.wait()
            return True

    environment = _minio_env()
    environment["VOXBENCH_MINIO_PROBE_BUCKET"] = "true"
    environment["VOXBENCH_MINIO_PROBE_TIMEOUT_MS"] = "10"
    factory = CapturingClientFactory()
    factory.client = BlockingProbeClient()

    try:
        _, readiness = build_recording_sink_from_env(
            environment,
            client_factory=factory,
        )
    finally:
        release.set()

    assert readiness.state == "unavailable"
    assert readiness.reason_alias == "bucket-probe-timeout"


def test_minio_environment_reports_only_missing_variable_names() -> None:
    environment = _minio_env()
    environment["VOXBENCH_MINIO_ENDPOINT"] = ""
    environment.pop("VOXBENCH_MINIO_SECRET_KEY")

    with pytest.raises(StorageConfigurationError) as caught:
        build_recording_sink_from_env(environment)

    assert caught.value.reason_alias == "minio-config-missing"
    assert caught.value.missing_env_names == (
        "VOXBENCH_MINIO_ENDPOINT",
        "VOXBENCH_MINIO_SECRET_KEY",
    )
    assert "private-access-key" not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "value", "reason_alias"),
    [
        ("VOXBENCH_RECORDING_SINK", "private-backend", "unsupported-recording-sink"),
        ("VOXBENCH_MINIO_SECURE", "secret-bool", "minio-secure-invalid"),
        (
            "VOXBENCH_REMOTE_AUDIO_PROXY",
            "secret-bool",
            "remote-audio-proxy-flag-invalid",
        ),
        (
            "VOXBENCH_MINIO_PROBE_BUCKET",
            "secret-bool",
            "minio-probe-flag-invalid",
        ),
        (
            "VOXBENCH_MINIO_PROBE_TIMEOUT_MS",
            "private-timeout",
            "minio-probe-timeout-invalid",
        ),
        ("VOXBENCH_MINIO_PROBE_TIMEOUT_MS", "9", "minio-probe-timeout-invalid"),
        (
            "VOXBENCH_MINIO_PROBE_TIMEOUT_MS",
            "10001",
            "minio-probe-timeout-invalid",
        ),
        ("VOXBENCH_MINIO_IO_TIMEOUT_MS", "99", "minio-io-timeout-invalid"),
        ("VOXBENCH_MINIO_IO_TIMEOUT_MS", "30001", "minio-io-timeout-invalid"),
        (
            "VOXBENCH_REMOTE_AUDIO_MAX_BYTES",
            "43",
            "remote-audio-max-bytes-invalid",
        ),
        (
            "VOXBENCH_REMOTE_AUDIO_MAX_BYTES",
            str(64 * 1024 * 1024 + 1),
            "remote-audio-max-bytes-invalid",
        ),
        (
            "VOXBENCH_REMOTE_AUDIO_MAX_CONCURRENT",
            "0",
            "remote-audio-max-concurrent-invalid",
        ),
        (
            "VOXBENCH_REMOTE_AUDIO_MAX_CONCURRENT",
            "9",
            "remote-audio-max-concurrent-invalid",
        ),
        (
            "VOXBENCH_MINIO_ENDPOINT",
            "https://user:private-secret@minio.internal/path",
            "minio-endpoint-invalid",
        ),
        ("VOXBENCH_MINIO_ENDPOINT", "minio.internal:70000", "minio-endpoint-invalid"),
        ("VOXBENCH_MINIO_BUCKET", "private/secret", "minio-object-config-invalid"),
    ],
)
def test_invalid_environment_uses_safe_error_alias(
    name: str,
    value: str,
    reason_alias: str,
) -> None:
    environment = _minio_env()
    environment[name] = value

    with pytest.raises(StorageConfigurationError) as caught:
        build_recording_sink_from_env(
            environment,
            client_factory=CapturingClientFactory(),
        )

    assert caught.value.reason_alias == reason_alias
    assert value not in str(caught.value)


def test_client_factory_failure_discards_raw_error() -> None:
    def failing_factory(**kwargs: object) -> FakeMinioClient:
        raise RuntimeError("token=private-secret endpoint=https://private.invalid")

    with pytest.raises(StorageConfigurationError) as caught:
        build_recording_sink_from_env(
            _minio_env(),
            client_factory=failing_factory,
        )

    assert caught.value.reason_alias == "minio-client-construction-failed"
    assert "private-secret" not in str(caught.value)
    assert "private.invalid" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("token", ["short", "a" * 257, "a" * 31 + " "])
def test_remote_audio_proxy_rejects_unsafe_token_without_echo(token: str) -> None:
    environment = _minio_env()
    environment["VOXBENCH_REMOTE_AUDIO_PROXY"] = "true"
    environment["VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN"] = token

    with pytest.raises(StorageConfigurationError) as caught:
        build_recording_storage_from_env(
            environment,
            client_factory=CapturingClientFactory(),
        )

    assert caught.value.reason_alias == "remote-audio-token-invalid"
    assert token not in str(caught.value)


def test_remote_audio_proxy_cannot_be_enabled_for_local_storage() -> None:
    with pytest.raises(StorageConfigurationError) as caught:
        build_recording_storage_from_env({"VOXBENCH_REMOTE_AUDIO_PROXY": "true"})

    assert caught.value.reason_alias == "remote-audio-proxy-requires-minio"


def test_storage_readiness_endpoint_is_local_by_default() -> None:
    response = TestClient(create_app()).get("/storage/readiness")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "local",
        "state": "ready",
        "bucket_alias": None,
        "prefix_alias": None,
        "secure": None,
        "reason_alias": None,
        "remote_audio_proxy_enabled": False,
        "web_audio_session_enabled": False,
        "web_audio_cookie_secure": None,
        "web_audio_session_ttl_seconds": None,
    }


def test_environment_app_exposes_only_safe_minio_readiness() -> None:
    response = TestClient(
        create_app_from_env(
            environ=_minio_env(),
            client_factory=CapturingClientFactory(),
        )
    ).get("/storage/readiness")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "minio",
        "state": "configured",
        "bucket_alias": "voxbench-recordings",
        "prefix_alias": "stage-taps/v1",
        "secure": False,
        "reason_alias": "connectivity-not-checked",
        "remote_audio_proxy_enabled": False,
        "web_audio_session_enabled": False,
        "web_audio_cookie_secure": None,
        "web_audio_session_ttl_seconds": None,
    }
    assert "minio.internal" not in response.text
    assert "private-access-key" not in response.text
    assert "private-secret" not in response.text


def test_environment_app_exposes_proxy_capability_without_access_token() -> None:
    environment = _minio_env()
    environment["VOXBENCH_REMOTE_AUDIO_PROXY"] = "true"
    environment["VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN"] = "private-token-" + "a" * 32

    app = create_app_from_env(
        environ=environment,
        client_factory=CapturingClientFactory(),
    )
    response = TestClient(app).get("/storage/readiness")

    assert response.status_code == 200
    assert response.json()["remote_audio_proxy_enabled"] is True
    assert response.json()["web_audio_session_enabled"] is False
    assert "private-token" not in response.text
    assert "private-access-key" not in response.text
    assert "private-secret" not in response.text


def test_environment_app_wires_safe_web_audio_session_capability() -> None:
    environment = _minio_env()
    environment.update(
        {
            "VOXBENCH_REMOTE_AUDIO_PROXY": "true",
            "VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN": "remote-" + "r" * 32,
            "VOXBENCH_WEB_AUDIO_SESSION": "true",
            "VOXBENCH_WEB_AUDIO_LOGIN_TOKEN": "login-" + "a" * 32,
            "VOXBENCH_WEB_AUDIO_SESSION_SECRET": "sign-" + "b" * 32,
            "VOXBENCH_WEB_AUDIO_SESSION_TTL_SECONDS": "300",
            "VOXBENCH_WEB_AUDIO_COOKIE_SECURE": "false",
        }
    )
    app = create_app_from_env(
        environ=environment,
        client_factory=CapturingClientFactory(),
    )
    client = TestClient(app)

    readiness = client.get("/storage/readiness")
    session = client.get("/auth/remote-audio/session")

    assert readiness.status_code == 200
    assert readiness.json()["web_audio_session_enabled"] is True
    assert readiness.json()["web_audio_cookie_secure"] is False
    assert readiness.json()["web_audio_session_ttl_seconds"] == 300
    assert session.json() == {
        "enabled": True,
        "authenticated": False,
        "expires_in_seconds": None,
    }
    serialized = readiness.text + session.text + repr(app.state.voxbench)
    assert "remote-" not in serialized
    assert "login-" not in serialized
    assert "sign-" not in serialized


def test_injected_sink_readiness_does_not_inspect_the_sink() -> None:
    response = TestClient(
        create_app(
            recording_sink=MinioRecordingSink(
                client=FakeMinioClient(),
                bucket="voxbench-recordings",
            )
        )
    ).get("/storage/readiness")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "injected",
        "state": "configured",
        "bucket_alias": None,
        "prefix_alias": None,
        "secure": None,
        "reason_alias": "connectivity-not-checked",
        "remote_audio_proxy_enabled": False,
        "web_audio_session_enabled": False,
        "web_audio_cookie_secure": None,
        "web_audio_session_ttl_seconds": None,
    }
