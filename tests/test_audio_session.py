from __future__ import annotations

import pytest

from voxbench.control_plane.audio_session import (
    AudioSessionConfigurationError,
    AudioSessionLoginError,
    RemoteAudioSessionAuth,
    build_remote_audio_session_from_env,
)


def _auth(clock_value: list[float]) -> RemoteAudioSessionAuth:
    return RemoteAudioSessionAuth(
        login_token="login-" + "a" * 32,
        signing_secret=("sign-" + "b" * 32).encode(),
        ttl_seconds=120,
        cookie_secure=False,
        clock=lambda: clock_value[0],
        nonce_factory=lambda _: "fixed-nonce",
    )


def test_audio_session_cookie_is_signed_short_lived_and_secret_free() -> None:
    clock_value = [1_000.0]
    auth = _auth(clock_value)

    cookie = auth.issue_cookie("login-" + "a" * 32)

    assert cookie.startswith("v1.1120.fixed-nonce.")
    assert "login-" not in cookie
    assert "sign-" not in cookie
    assert "login-" not in repr(auth)
    assert "sign-" not in repr(auth)
    assert auth.remaining_seconds(cookie) == 120
    assert auth.remaining_seconds(cookie + "tampered") is None
    clock_value[0] = 1_120.0
    assert auth.remaining_seconds(cookie) is None


def test_audio_session_rejects_wrong_login_without_echo() -> None:
    auth = _auth([1_000.0])

    with pytest.raises(AudioSessionLoginError) as caught:
        auth.issue_cookie("wrong-" + "c" * 32)

    assert str(caught.value) == "web-audio-login-rejected"
    assert "wrong" not in str(caught.value)


def test_audio_session_is_disabled_by_default() -> None:
    assert build_remote_audio_session_from_env({}, remote_audio_proxy_enabled=False) is None


def test_audio_session_environment_builds_safe_runtime() -> None:
    auth = build_remote_audio_session_from_env(
        {
            "VOXBENCH_WEB_AUDIO_SESSION": "true",
            "VOXBENCH_WEB_AUDIO_LOGIN_TOKEN": "login-" + "a" * 32,
            "VOXBENCH_WEB_AUDIO_SESSION_SECRET": "sign-" + "b" * 32,
            "VOXBENCH_WEB_AUDIO_SESSION_TTL_SECONDS": "300",
            "VOXBENCH_WEB_AUDIO_COOKIE_SECURE": "false",
        },
        remote_audio_proxy_enabled=True,
    )

    assert auth is not None
    assert auth.ttl_seconds == 300
    assert auth.cookie_secure is False
    assert "login-" not in repr(auth)
    assert "sign-" not in repr(auth)


def test_audio_session_cookie_is_secure_by_default() -> None:
    auth = build_remote_audio_session_from_env(
        {
            "VOXBENCH_WEB_AUDIO_SESSION": "true",
            "VOXBENCH_WEB_AUDIO_LOGIN_TOKEN": "login-" + "a" * 32,
            "VOXBENCH_WEB_AUDIO_SESSION_SECRET": "sign-" + "b" * 32,
        },
        remote_audio_proxy_enabled=True,
    )

    assert auth is not None and auth.cookie_secure is True


def test_audio_session_requires_remote_proxy_before_reading_secrets() -> None:
    with pytest.raises(AudioSessionConfigurationError) as caught:
        build_remote_audio_session_from_env(
            {"VOXBENCH_WEB_AUDIO_SESSION": "true"},
            remote_audio_proxy_enabled=False,
        )

    assert caught.value.reason_alias == "web-audio-session-requires-remote-proxy"


def test_audio_session_reports_only_missing_environment_names() -> None:
    with pytest.raises(AudioSessionConfigurationError) as caught:
        build_remote_audio_session_from_env(
            {"VOXBENCH_WEB_AUDIO_SESSION": "true"},
            remote_audio_proxy_enabled=True,
        )

    assert caught.value.reason_alias == "web-audio-session-config-missing"
    assert caught.value.missing_env_names == (
        "VOXBENCH_WEB_AUDIO_LOGIN_TOKEN",
        "VOXBENCH_WEB_AUDIO_SESSION_SECRET",
    )


@pytest.mark.parametrize(
    ("name", "value", "reason_alias"),
    [
        ("VOXBENCH_WEB_AUDIO_SESSION", "unsafe", "web-audio-session-flag-invalid"),
        ("VOXBENCH_WEB_AUDIO_LOGIN_TOKEN", "short", "web-audio-login-token-invalid"),
        (
            "VOXBENCH_WEB_AUDIO_SESSION_SECRET",
            "short",
            "web-audio-session-secret-invalid",
        ),
        ("VOXBENCH_WEB_AUDIO_SESSION_TTL_SECONDS", "59", "web-audio-session-ttl-invalid"),
        ("VOXBENCH_WEB_AUDIO_SESSION_TTL_SECONDS", "3601", "web-audio-session-ttl-invalid"),
        (
            "VOXBENCH_WEB_AUDIO_COOKIE_SECURE",
            "unsafe",
            "web-audio-cookie-secure-invalid",
        ),
    ],
)
def test_audio_session_invalid_config_uses_safe_alias(
    name: str,
    value: str,
    reason_alias: str,
) -> None:
    environment = {
        "VOXBENCH_WEB_AUDIO_SESSION": "true",
        "VOXBENCH_WEB_AUDIO_LOGIN_TOKEN": "login-" + "a" * 32,
        "VOXBENCH_WEB_AUDIO_SESSION_SECRET": "sign-" + "b" * 32,
    }
    environment[name] = value

    with pytest.raises(AudioSessionConfigurationError) as caught:
        build_remote_audio_session_from_env(
            environment,
            remote_audio_proxy_enabled=True,
        )

    assert caught.value.reason_alias == reason_alias
    assert value not in str(caught.value)


def test_audio_session_requires_distinct_login_and_signing_secrets() -> None:
    shared = "shared-" + "a" * 32

    with pytest.raises(AudioSessionConfigurationError) as caught:
        build_remote_audio_session_from_env(
            {
                "VOXBENCH_WEB_AUDIO_SESSION": "true",
                "VOXBENCH_WEB_AUDIO_LOGIN_TOKEN": shared,
                "VOXBENCH_WEB_AUDIO_SESSION_SECRET": shared,
            },
            remote_audio_proxy_enabled=True,
        )

    assert caught.value.reason_alias == "web-audio-secrets-must-differ"
