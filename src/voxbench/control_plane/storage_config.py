"""Process-level recording storage configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from typing import Literal, Protocol

from voxbench.engine_harness.storage import (
    MinioClientLike,
    MinioRecordingSink,
    RecordingSink,
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

_DEFAULT_PROBE_TIMEOUT_MS = 2_000
_MIN_PROBE_TIMEOUT_MS = 10
_MAX_PROBE_TIMEOUT_MS = 10_000

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


class ConfiguredMinioClient(MinioClientLike, Protocol):
    def bucket_exists(self, bucket_name: str) -> bool: ...


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

    values = os.environ if environ is None else environ
    mode = values.get(RECORDING_SINK_ENV, "local").strip().lower()
    if mode == "local":
        return None, local_storage_readiness()
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
    _validate_endpoint(endpoint)

    factory = client_factory or _create_minio_client
    try:
        client = factory(
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
    readiness = StorageReadiness(
        mode="minio",
        state=state,
        bucket_alias=bucket,
        prefix_alias=sink.prefix,
        secure=secure,
        reason_alias=reason_alias,
    )
    return sink, readiness


def _parse_boolean(value: str, *, reason_alias: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise StorageConfigurationError(reason_alias)


def _parse_probe_timeout_ms(value: str) -> int:
    normalized = value.strip()
    if not normalized.isdigit():
        raise StorageConfigurationError("minio-probe-timeout-invalid")
    timeout_ms = int(normalized)
    if not _MIN_PROBE_TIMEOUT_MS <= timeout_ms <= _MAX_PROBE_TIMEOUT_MS:
        raise StorageConfigurationError("minio-probe-timeout-invalid")
    return timeout_ms


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
) -> ConfiguredMinioClient:
    try:
        from minio import Minio
    except ImportError as exc:
        raise StorageConfigurationError("minio-dependency-missing") from exc
    return Minio(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )
