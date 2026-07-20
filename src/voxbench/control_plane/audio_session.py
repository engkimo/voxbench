"""Short-lived browser session for authenticated remote recording playback."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

WEB_AUDIO_SESSION_ENV = "VOXBENCH_WEB_AUDIO_SESSION"
WEB_AUDIO_LOGIN_TOKEN_ENV = "VOXBENCH_WEB_AUDIO_LOGIN_TOKEN"
WEB_AUDIO_SESSION_SECRET_ENV = "VOXBENCH_WEB_AUDIO_SESSION_SECRET"
WEB_AUDIO_SESSION_TTL_SECONDS_ENV = "VOXBENCH_WEB_AUDIO_SESSION_TTL_SECONDS"
WEB_AUDIO_COOKIE_SECURE_ENV = "VOXBENCH_WEB_AUDIO_COOKIE_SECURE"

REMOTE_AUDIO_SESSION_COOKIE = "voxbench_remote_audio_session"
_MIN_SECRET_LENGTH = 32
_MAX_SECRET_LENGTH = 256
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 3_600
_DEFAULT_TTL_SECONDS = 900


class AudioSessionConfigurationError(RuntimeError):
    def __init__(
        self,
        reason_alias: str,
        *,
        missing_env_names: tuple[str, ...] = (),
    ) -> None:
        self.reason_alias = reason_alias
        self.missing_env_names = missing_env_names
        message = f"web audio session configuration failed: {reason_alias}"
        if missing_env_names:
            message += f" ({', '.join(missing_env_names)})"
        super().__init__(message)


class AudioSessionLoginError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteAudioSessionAuth:
    login_token: str = field(repr=False)
    signing_secret: bytes = field(repr=False)
    ttl_seconds: int = _DEFAULT_TTL_SECONDS
    cookie_secure: bool = True
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)
    nonce_factory: Callable[[int], str] = field(
        default=secrets.token_urlsafe,
        repr=False,
        compare=False,
    )

    def issue_cookie(self, supplied_login_token: str) -> str:
        if (
            not supplied_login_token.isascii()
            or not hmac.compare_digest(supplied_login_token, self.login_token)
        ):
            raise AudioSessionLoginError("web-audio-login-rejected")
        expires_at = int(self.clock()) + self.ttl_seconds
        payload = f"v1.{expires_at}.{self.nonce_factory(18)}"
        return f"{payload}.{self._signature(payload)}"

    def remaining_seconds(self, cookie: str | None) -> int | None:
        if cookie is None or not cookie.isascii() or len(cookie) > 512:
            return None
        parts = cookie.split(".")
        if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
            return None
        payload = ".".join(parts[:3])
        if not hmac.compare_digest(parts[3], self._signature(payload)):
            return None
        now = int(self.clock())
        expires_at = int(parts[1])
        remaining = expires_at - now
        if remaining <= 0 or remaining > self.ttl_seconds:
            return None
        return remaining

    def _signature(self, payload: str) -> str:
        digest = hmac.new(
            self.signing_secret,
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_remote_audio_session_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    remote_audio_proxy_enabled: bool,
) -> RemoteAudioSessionAuth | None:
    values = os.environ if environ is None else environ
    enabled = _parse_boolean(
        values.get(WEB_AUDIO_SESSION_ENV, "false"),
        reason_alias="web-audio-session-flag-invalid",
    )
    if not enabled:
        return None
    if not remote_audio_proxy_enabled:
        raise AudioSessionConfigurationError("web-audio-session-requires-remote-proxy")

    required_names = (WEB_AUDIO_LOGIN_TOKEN_ENV, WEB_AUDIO_SESSION_SECRET_ENV)
    missing_names = tuple(name for name in required_names if name not in values)
    if missing_names:
        raise AudioSessionConfigurationError(
            "web-audio-session-config-missing",
            missing_env_names=missing_names,
        )
    login_token = values[WEB_AUDIO_LOGIN_TOKEN_ENV]
    signing_secret = values[WEB_AUDIO_SESSION_SECRET_ENV]
    _validate_secret(login_token, reason_alias="web-audio-login-token-invalid")
    _validate_secret(signing_secret, reason_alias="web-audio-session-secret-invalid")
    if hmac.compare_digest(login_token, signing_secret):
        raise AudioSessionConfigurationError("web-audio-secrets-must-differ")

    ttl_seconds = _parse_integer(
        values.get(WEB_AUDIO_SESSION_TTL_SECONDS_ENV, str(_DEFAULT_TTL_SECONDS)),
        minimum=_MIN_TTL_SECONDS,
        maximum=_MAX_TTL_SECONDS,
        reason_alias="web-audio-session-ttl-invalid",
    )
    cookie_secure = _parse_boolean(
        values.get(WEB_AUDIO_COOKIE_SECURE_ENV, "true"),
        reason_alias="web-audio-cookie-secure-invalid",
    )
    return RemoteAudioSessionAuth(
        login_token=login_token,
        signing_secret=signing_secret.encode("ascii"),
        ttl_seconds=ttl_seconds,
        cookie_secure=cookie_secure,
    )


def _validate_secret(value: str, *, reason_alias: str) -> None:
    if (
        not _MIN_SECRET_LENGTH <= len(value) <= _MAX_SECRET_LENGTH
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise AudioSessionConfigurationError(reason_alias)


def _parse_boolean(value: str, *, reason_alias: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise AudioSessionConfigurationError(reason_alias)


def _parse_integer(
    value: str,
    *,
    minimum: int,
    maximum: int,
    reason_alias: str,
) -> int:
    normalized = value.strip()
    if not normalized.isdigit():
        raise AudioSessionConfigurationError(reason_alias)
    parsed = int(normalized)
    if not minimum <= parsed <= maximum:
        raise AudioSessionConfigurationError(reason_alias)
    return parsed
