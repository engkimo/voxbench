"""Process-level recording storage configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from voxbench.engine_harness.storage import (
    MinioClientLike,
    MinioRecordingSink,
    RecordingSink,
)

StorageMode = Literal["local", "minio", "injected"]
StorageReadinessState = Literal["ready", "configured"]

RECORDING_SINK_ENV = "VOXBENCH_RECORDING_SINK"
MINIO_ENDPOINT_ENV = "VOXBENCH_MINIO_ENDPOINT"
MINIO_ACCESS_KEY_ENV = "VOXBENCH_MINIO_ACCESS_KEY"
MINIO_SECRET_KEY_ENV = "VOXBENCH_MINIO_SECRET_KEY"
MINIO_BUCKET_ENV = "VOXBENCH_MINIO_BUCKET"
MINIO_PREFIX_ENV = "VOXBENCH_MINIO_PREFIX"
MINIO_SECURE_ENV = "VOXBENCH_MINIO_SECURE"

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


class MinioClientFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool,
    ) -> MinioClientLike: ...


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
    secure = _parse_secure(values.get(MINIO_SECURE_ENV, "true"))
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

    readiness = StorageReadiness(
        mode="minio",
        state="configured",
        bucket_alias=bucket,
        prefix_alias=sink.prefix,
        secure=secure,
        reason_alias="connectivity-not-checked",
    )
    return sink, readiness


def _parse_secure(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise StorageConfigurationError("minio-secure-invalid")


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
) -> MinioClientLike:
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
