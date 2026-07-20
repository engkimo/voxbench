"""Process-level recording storage configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread
from typing import Literal, Protocol

from voxbench.engine_harness.storage import (
    MinioClientLike,
    MinioReadClientLike,
    MinioRecordingReader,
    MinioRecordingSink,
    RecordingSink,
    RemoteRecordingReader,
)

StorageMode = Literal["local", "minio", "injected"]
StorageReadinessState = Literal["ready", "configured", "unavailable"]

RECORDING_SINK_ENV = "VOXBENCH_RECORDING_SINK"
MINIO_ENDPOINT_ENV = "VOXBENCH_MINIO_ENDPOINT"
MINIO_ACCESS_KEY_ENV = "VOXBENCH_MINIO_ACCESS_KEY"
MINIO_SECRET_KEY_ENV = "VOXBENCH_MINIO_SECRET_KEY"
MINIO_BUCKET_ENV = "VOXBENCH_MINIO_BUCKET"
MINIO_PREFIX_ENV = "VOXBENCH_MINIO_PREFIX"
MINIO_SECURE_ENV = "VOXBENCH_MINIO_SECURE"
MINIO_PROBE_BUCKET_ENV = "VOXBENCH_MINIO_PROBE_BUCKET"
MINIO_PROBE_TIMEOUT_MS_ENV = "VOXBENCH_MINIO_PROBE_TIMEOUT_MS"
MINIO_IO_TIMEOUT_MS_ENV = "VOXBENCH_MINIO_IO_TIMEOUT_MS"
REMOTE_AUDIO_PROXY_ENV = "VOXBENCH_REMOTE_AUDIO_PROXY"
REMOTE_AUDIO_BEARER_TOKEN_ENV = "VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN"
REMOTE_AUDIO_MAX_BYTES_ENV = "VOXBENCH_REMOTE_AUDIO_MAX_BYTES"
REMOTE_AUDIO_MAX_CONCURRENT_ENV = "VOXBENCH_REMOTE_AUDIO_MAX_CONCURRENT"

_DEFAULT_PROBE_TIMEOUT_MS = 2_000
_MIN_PROBE_TIMEOUT_MS = 10
_MAX_PROBE_TIMEOUT_MS = 10_000
_DEFAULT_IO_TIMEOUT_MS = 5_000
_MIN_IO_TIMEOUT_MS = 100
_MAX_IO_TIMEOUT_MS = 30_000
_DEFAULT_REMOTE_AUDIO_MAX_BYTES = 10 * 1024 * 1024
_MIN_REMOTE_AUDIO_MAX_BYTES = 44
_MAX_REMOTE_AUDIO_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_REMOTE_AUDIO_MAX_CONCURRENT = 2
_MAX_REMOTE_AUDIO_MAX_CONCURRENT = 8
_MAX_REMOTE_AUDIO_INFLIGHT_BYTES = 128 * 1024 * 1024

_REQUIRED_MINIO_ENV_NAMES = (
    MINIO_ENDPOINT_ENV,
    MINIO_ACCESS_KEY_ENV,
    MINIO_SECRET_KEY_ENV,
    MINIO_BUCKET_ENV,
)
_ENDPOINT_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


@dataclass(frozen=True)
class StorageReadiness:
    """Credential-free projection of the configured recording sink."""

    mode: StorageMode
    state: StorageReadinessState
    bucket_alias: str | None = None
    prefix_alias: str | None = None
    secure: bool | None = None
    reason_alias: str | None = None
    remote_audio_proxy_enabled: bool = False


@dataclass(frozen=True)
class RecordingStorageRuntime:
    recording_sink: RecordingSink | None
    readiness: StorageReadiness
    remote_recording_reader: RemoteRecordingReader | None = None
    remote_audio_access_token: str | None = field(default=None, repr=False)


class StorageConfigurationError(RuntimeError):
    """Safe startup error that never includes process environment values."""

    def __init__(
        self,
        reason_alias: str,
        *,
        missing_env_names: tuple[str, ...] = (),
    ) -> None:
        self.reason_alias = reason_alias
        self.missing_env_names = missing_env_names
        message = f"recording storage configuration failed: {reason_alias}"
        if missing_env_names:
            message += f" ({', '.join(missing_env_names)})"
        super().__init__(message)


class ConfiguredMinioClient(MinioClientLike, MinioReadClientLike, Protocol):
    def bucket_exists(self, *, bucket_name: str) -> bool: ...


class MinioClientFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool,
    ) -> ConfiguredMinioClient: ...


def local_storage_readiness() -> StorageReadiness:
    return StorageReadiness(mode="local", state="ready")


def injected_storage_readiness() -> StorageReadiness:
    return StorageReadiness(
        mode="injected",
        state="configured",
        reason_alias="connectivity-not-checked",
    )


def build_recording_sink_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    client_factory: MinioClientFactory | None = None,
) -> tuple[RecordingSink | None, StorageReadiness]:
    """Build a sink only from process configuration, never from a run payload."""

    runtime = build_recording_storage_from_env(
        environ,
        client_factory=client_factory,
    )
    return runtime.recording_sink, runtime.readiness


def build_recording_storage_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    client_factory: MinioClientFactory | None = None,
) -> RecordingStorageRuntime:
    """Build process-only storage runtime services and their safe projection."""

    values = os.environ if environ is None else environ
    mode = values.get(RECORDING_SINK_ENV, "local").strip().lower()
    remote_audio_proxy = _parse_boolean(
        values.get(REMOTE_AUDIO_PROXY_ENV, "false"),
        reason_alias="remote-audio-proxy-flag-invalid",
    )
    if mode == "local":
        if remote_audio_proxy:
            raise StorageConfigurationError("remote-audio-proxy-requires-minio")
        return RecordingStorageRuntime(None, local_storage_readiness())
    if mode != "minio":
        raise StorageConfigurationError("unsupported-recording-sink")

    missing_env_names = tuple(
        name for name in _REQUIRED_MINIO_ENV_NAMES if not values.get(name, "").strip()
    )
    if missing_env_names:
        raise StorageConfigurationError(
            "minio-config-missing",
            missing_env_names=missing_env_names,
        )

    endpoint = values[MINIO_ENDPOINT_ENV].strip()
    access_key = values[MINIO_ACCESS_KEY_ENV]
    secret_key = values[MINIO_SECRET_KEY_ENV]
    bucket = values[MINIO_BUCKET_ENV].strip()
    prefix = values.get(MINIO_PREFIX_ENV, "recordings").strip()
    secure = _parse_boolean(
        values.get(MINIO_SECURE_ENV, "true"),
        reason_alias="minio-secure-invalid",
    )
    probe_bucket = _parse_boolean(
        values.get(MINIO_PROBE_BUCKET_ENV, "false"),
        reason_alias="minio-probe-flag-invalid",
    )
    probe_timeout_ms = _parse_probe_timeout_ms(
        values.get(MINIO_PROBE_TIMEOUT_MS_ENV, str(_DEFAULT_PROBE_TIMEOUT_MS))
    )
    io_timeout_ms = _parse_bounded_integer(
        values.get(MINIO_IO_TIMEOUT_MS_ENV, str(_DEFAULT_IO_TIMEOUT_MS)),
        minimum=_MIN_IO_TIMEOUT_MS,
        maximum=_MAX_IO_TIMEOUT_MS,
        reason_alias="minio-io-timeout-invalid",
    )
    remote_audio_max_bytes = _parse_bounded_integer(
        values.get(
            REMOTE_AUDIO_MAX_BYTES_ENV,
            str(_DEFAULT_REMOTE_AUDIO_MAX_BYTES),
        ),
        minimum=_MIN_REMOTE_AUDIO_MAX_BYTES,
        maximum=_MAX_REMOTE_AUDIO_MAX_BYTES,
        reason_alias="remote-audio-max-bytes-invalid",
    )
    remote_audio_max_concurrent = _parse_bounded_integer(
        values.get(
            REMOTE_AUDIO_MAX_CONCURRENT_ENV,
            str(_DEFAULT_REMOTE_AUDIO_MAX_CONCURRENT),
        ),
        minimum=1,
        maximum=_MAX_REMOTE_AUDIO_MAX_CONCURRENT,
        reason_alias="remote-audio-max-concurrent-invalid",
    )
    remote_audio_access_token: str | None = None
    if remote_audio_proxy:
        remote_audio_access_token = values.get(REMOTE_AUDIO_BEARER_TOKEN_ENV)
        if remote_audio_access_token is None:
            raise StorageConfigurationError("remote-audio-token-missing")
        _validate_remote_audio_token(remote_audio_access_token)
        if (
            remote_audio_max_bytes * remote_audio_max_concurrent
            > _MAX_REMOTE_AUDIO_INFLIGHT_BYTES
        ):
            raise StorageConfigurationError("remote-audio-capacity-invalid")
    _validate_endpoint(endpoint)

    try:
        if client_factory is None:
            client = _create_minio_client(
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
                io_timeout_ms=io_timeout_ms,
                max_connections=remote_audio_max_concurrent,
            )
        else:
            client = client_factory(
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )
    except StorageConfigurationError:
        raise
    except Exception:
        raise StorageConfigurationError("minio-client-construction-failed") from None

    try:
        sink = MinioRecordingSink(client=client, bucket=bucket, prefix=prefix)
    except ValueError:
        raise StorageConfigurationError("minio-object-config-invalid") from None

    if probe_bucket:
        state, reason_alias = _probe_minio_bucket(
            client,
            bucket=bucket,
            timeout_ms=probe_timeout_ms,
        )
    else:
        state, reason_alias = "configured", "connectivity-not-checked"
    remote_recording_reader: RemoteRecordingReader | None = None
    if remote_audio_proxy:
        remote_recording_reader = MinioRecordingReader(
            client=client,
            bucket=bucket,
            prefix=sink.prefix,
            max_bytes=remote_audio_max_bytes,
            timeout_seconds=io_timeout_ms / 1_000,
            max_concurrent_reads=remote_audio_max_concurrent,
        )

    readiness = StorageReadiness(
        mode="minio",
        state=state,
        bucket_alias=bucket,
        prefix_alias=sink.prefix,
        secure=secure,
        reason_alias=reason_alias,
        remote_audio_proxy_enabled=remote_audio_proxy,
    )
    return RecordingStorageRuntime(
        recording_sink=sink,
        readiness=readiness,
        remote_recording_reader=remote_recording_reader,
        remote_audio_access_token=remote_audio_access_token,
    )


def _parse_boolean(value: str, *, reason_alias: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise StorageConfigurationError(reason_alias)


def _parse_probe_timeout_ms(value: str) -> int:
    return _parse_bounded_integer(
        value,
        minimum=_MIN_PROBE_TIMEOUT_MS,
        maximum=_MAX_PROBE_TIMEOUT_MS,
        reason_alias="minio-probe-timeout-invalid",
    )


def _parse_bounded_integer(
    value: str,
    *,
    minimum: int,
    maximum: int,
    reason_alias: str,
) -> int:
    normalized = value.strip()
    if not normalized.isdigit():
        raise StorageConfigurationError(reason_alias)
    parsed = int(normalized)
    if not minimum <= parsed <= maximum:
        raise StorageConfigurationError(reason_alias)
    return parsed


def _validate_remote_audio_token(token: str) -> None:
    if (
        not 32 <= len(token) <= 256
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise StorageConfigurationError("remote-audio-token-invalid")


def _probe_minio_bucket(
    client: ConfiguredMinioClient,
    *,
    bucket: str,
    timeout_ms: int,
) -> tuple[StorageReadinessState, str | None]:
    result: Queue[tuple[StorageReadinessState, str | None]] = Queue(maxsize=1)

    def target() -> None:
        try:
            exists = client.bucket_exists(bucket_name=bucket)
        except Exception:
            result.put(("unavailable", "bucket-probe-failed"))
            return
        if exists:
            result.put(("ready", None))
        else:
            result.put(("unavailable", "bucket-not-found"))

    thread = Thread(target=target, name="voxbench-minio-readiness", daemon=True)
    thread.start()
    thread.join(timeout_ms / 1_000)
    if thread.is_alive():
        return "unavailable", "bucket-probe-timeout"
    try:
        return result.get_nowait()
    except Empty:
        return "unavailable", "bucket-probe-failed"


def _validate_endpoint(endpoint: str) -> None:
    if (
        not endpoint
        or "://" in endpoint
        or "/" in endpoint
        or "@" in endpoint
        or any(character.isspace() for character in endpoint)
        or endpoint.count(":") > 1
    ):
        raise StorageConfigurationError("minio-endpoint-invalid")

    host, separator, port_text = endpoint.rpartition(":")
    if not separator:
        host = endpoint
    if not _ENDPOINT_HOST.fullmatch(host) or ".." in host:
        raise StorageConfigurationError("minio-endpoint-invalid")
    if separator and (not port_text.isdigit() or not 1 <= int(port_text) <= 65_535):
        raise StorageConfigurationError("minio-endpoint-invalid")


def _create_minio_client(
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    secure: bool,
    io_timeout_ms: int,
    max_connections: int,
) -> ConfiguredMinioClient:
    try:
        import certifi
        import urllib3
        from minio import Minio
    except ImportError as exc:
        raise StorageConfigurationError("minio-dependency-missing") from exc
    timeout_seconds = io_timeout_ms / 1_000
    return Minio(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        http_client=urllib3.PoolManager(
            timeout=urllib3.Timeout(
                connect=timeout_seconds,
                read=timeout_seconds,
            ),
            retries=False,
            maxsize=max_connections,
            block=True,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
        ),
    )
