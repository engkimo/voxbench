from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASTERISK_DOCKER = ROOT / "examples" / "asterisk" / "docker"


def test_compose_publishes_telephony_services_on_loopback_only() -> None:
    compose = (ASTERISK_DOCKER / "compose.yaml").read_text()

    assert '"127.0.0.1:${VOXBENCH_SIP_PORT:-5060}:5060/udp"' in compose
    assert '"127.0.0.1:${VOXBENCH_AMI_PORT:-5038}:5038/tcp"' in compose
    assert '"127.0.0.1:10000-10099:10000-10099/udp"' in compose
    assert "host.docker.internal:host-gateway" in compose
    assert "pjsip show endpoint 6001" in compose
    assert "module show like app_audiosocket" in compose
    assert "module show like res_rtp_asterisk" in compose


def test_pjsip_template_matches_printed_telephone_account() -> None:
    pjsip = (ASTERISK_DOCKER / "config" / "pjsip.conf.template").read_text()

    assert "bind=0.0.0.0:5060" in pjsip
    assert "external_signaling_address=127.0.0.1" in pjsip
    assert "external_media_address=127.0.0.1" in pjsip
    assert "local_net=" not in pjsip
    assert "context=voxbench-demo" in pjsip
    assert "allow=ulaw" in pjsip
    assert "username=6001" in pjsip
    assert "password=REPLACE_WITH_LOCAL_SECRET" in pjsip
    assert "direct_media=no" in pjsip
    assert "rtp_symmetric=yes" in pjsip


def test_extension_7000_uses_canonical_uuid_and_host_bridge() -> None:
    extensions = (ASTERISK_DOCKER / "config" / "extensions.conf.template").read_text()

    assert "exten => 7000,1" in extensions
    assert "${FILTER(0-9a-f-,${SHELL(cat /proc/sys/kernel/random/uuid)})}" in extensions
    assert "REPLACE_WITH_AUDIOSOCKET_HOST:REPLACE_WITH_AUDIOSOCKET_PORT" in extensions
    assert "AudioSocket(${CALL_UUID},${VOXBENCH_AUDIOSOCKET})" in extensions


def test_minimal_module_set_includes_pjsip_rtp_engine() -> None:
    modules = (ASTERISK_DOCKER / "config" / "modules.conf").read_text()

    assert "load => res_pjsip_sdp_rtp.so" in modules
    assert "load => res_rtp_asterisk.so" in modules
    assert "load => chan_pjsip.so" in modules


def test_runtime_templates_do_not_contain_default_credentials() -> None:
    pjsip = (ASTERISK_DOCKER / "config" / "pjsip.conf.template").read_text()
    manager = (ASTERISK_DOCKER / "config" / "manager.conf.template").read_text()

    assert "voxbench-6001-local-only" not in pjsip
    assert "voxbench-ami-local-only" not in manager
    assert "REPLACE_WITH_AMI_SECRET" in manager


def test_local_launcher_distinguishes_gemini_from_loopback() -> None:
    launcher = (ROOT / "scripts" / "asterisk-local").read_text()

    assert "gemini   Run the Gemini Live AudioSocket bridge" in launcher
    assert '[[ -z "${GOOGLE_API_KEY:-}" ]]' in launcher
    assert '[[ -z "${GEMINI_API_KEY:-}" ]]' in launcher
    assert "audiosocket-realtime" in launcher
    assert "--provider gemini-live" in launcher
