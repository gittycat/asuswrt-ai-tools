"""The router module: config loading, nvram read/write, serialisation."""

from __future__ import annotations

import asyncio

import pytest

from asusrouter.modules.port_forwarding import AsusPortForwarding, PortForwardingRule

from asuswrt import ops
from asuswrt.cli import main as cli, render
from asuswrt.router import (
    ConfigError,
    RouterConfig,
    apply_nvram,
    config_paths,
    detect_gateway,
    enum_name,
    explain_router_error,
    jsonable,
    load_config,
    read_nvram,
)
from helpers import FakeRouter


def run(coro):
    return asyncio.run(coro)


def _empty_env(tmp_path):
    """An env file that exists but sets nothing.

    load_config stops at the first path that exists, so this keeps a real
    ./.env or ~/.config/asuswrt/.env out of the test.
    """
    path = tmp_path / "empty.env"
    path.write_text("")
    return path


# -- configuration ---------------------------------------------------------


def test_url_is_built_from_scheme_host_and_port():
    assert RouterConfig("192.168.50.1", "admin", "x", False, None).url == (
        "http://192.168.50.1"
    )
    assert RouterConfig("10.0.0.1", "admin", "x", True, 8443).url == (
        "https://10.0.0.1:8443"
    )


def test_config_paths_puts_the_override_first(monkeypatch, tmp_path):
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(tmp_path / "custom.env"))
    assert config_paths()[0] == tmp_path / "custom.env"


def test_config_paths_without_an_override_starts_at_the_working_directory(monkeypatch):
    monkeypatch.delenv("ASUSWRT_ENV_FILE", raising=False)
    assert config_paths()[0].name == ".env"


def test_missing_password_names_every_path_it_searched(monkeypatch, tmp_path):
    """Never ask for the password in chat — the error is the instructions."""
    env = tmp_path / "empty.env"
    env.write_text("ROUTER_HOST=192.168.50.1\n")
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(env))
    monkeypatch.delenv("ROUTER_PASS", raising=False)

    with pytest.raises(ConfigError) as exc:
        load_config()

    message = str(exc.value)
    assert "asuswrt setup" in message
    assert str(env) in message


def test_config_defaults_fill_in_around_the_password(monkeypatch, tmp_path):
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(_empty_env(tmp_path)))
    monkeypatch.setenv("ROUTER_PASS", "secret")
    monkeypatch.setattr("asuswrt.router.detect_gateway", lambda: "10.0.0.1")
    for name in ("ROUTER_HOST", "ROUTER_USER", "ROUTER_SSL", "ROUTER_PORT"):
        monkeypatch.delenv(name, raising=False)

    config = load_config()
    assert (config.host, config.username, config.use_ssl, config.port) == (
        "10.0.0.1",
        "admin",
        False,
        None,
    )


def test_blank_installer_fields_fall_back_to_the_defaults(monkeypatch, tmp_path):
    """A GUI installer sends "" for a field left blank, not an absent name."""
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(_empty_env(tmp_path)))
    monkeypatch.setenv("ROUTER_PASS", "secret")
    monkeypatch.setattr("asuswrt.router.detect_gateway", lambda: "10.0.0.1")
    for name in ("ROUTER_HOST", "ROUTER_USER", "ROUTER_SSL", "ROUTER_PORT"):
        monkeypatch.setenv(name, "")

    config = load_config()
    assert (config.host, config.username, config.use_ssl, config.port) == (
        "10.0.0.1",
        "admin",
        False,
        None,
    )


def test_a_blank_installer_password_does_not_shadow_the_env_file(
    monkeypatch, tmp_path
):
    """load_dotenv() does not override, so "" would win over a working file."""
    env = tmp_path / "configured.env"
    env.write_text("ROUTER_HOST=192.168.50.1\nROUTER_USER=me\nROUTER_PASS=secret\n")
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(env))
    monkeypatch.setenv("ROUTER_PASS", "")
    monkeypatch.setenv("ROUTER_USER", "")

    config = load_config()
    assert (config.host, config.username, config.password) == (
        "192.168.50.1",
        "me",
        "secret",
    )


def test_config_treats_dollar_braces_in_password_as_literal(monkeypatch, tmp_path):
    env = tmp_path / "literal.env"
    env.write_text(
        'ROUTER_HOST=192.168.50.1\nROUTER_PASS="literal${HOME}password"\n'
    )
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(env))
    monkeypatch.delenv("ROUTER_PASS", raising=False)

    assert load_config().password == "literal${HOME}password"


def test_router_host_wins_over_the_detected_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(_empty_env(tmp_path)))
    monkeypatch.setenv("ROUTER_PASS", "secret")
    monkeypatch.setenv("ROUTER_HOST", "10.1.1.1")
    monkeypatch.setattr("asuswrt.router.detect_gateway", lambda: "10.0.0.1")

    assert load_config().host == "10.1.1.1"


def test_undetectable_gateway_asks_for_router_host_rather_than_guessing(
    monkeypatch, tmp_path
):
    """No vendor default: a wrong guess reads as a broken router."""
    env = _empty_env(tmp_path)
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(env))
    monkeypatch.setenv("ROUTER_PASS", "secret")
    monkeypatch.delenv("ROUTER_HOST", raising=False)
    monkeypatch.setattr("asuswrt.router.detect_gateway", lambda: None)

    with pytest.raises(ConfigError) as exc:
        load_config()

    message = str(exc.value)
    assert "asuswrt setup --host" in message
    assert str(env) in message
    assert "192.168" not in message


def test_detect_gateway_reads_the_default_route(monkeypatch):
    monkeypatch.setattr(
        "asuswrt.router.subprocess.check_output",
        lambda *a, **k: "   route to: default\n   gateway: 10.9.9.1\n",
    )
    assert detect_gateway() == "10.9.9.1"


def test_detect_gateway_skips_a_probe_that_is_not_installed(monkeypatch):
    """Linux has no `route -n get default`; macOS has no `ip`."""
    calls = []

    def check_output(argv, **kwargs):
        calls.append(argv[0])
        if len(calls) == 1:
            raise FileNotFoundError(argv[0])
        return "default via 10.8.8.1 dev eth0\n"

    monkeypatch.setattr("asuswrt.router.subprocess.check_output", check_output)
    assert detect_gateway() == "10.8.8.1"
    assert len(calls) == 2


def test_detect_gateway_rejects_a_non_address(monkeypatch):
    """A point-to-point default route reports `link#14`, not an address."""
    monkeypatch.setattr(
        "asuswrt.router.subprocess.check_output",
        lambda *a, **k: "   gateway: link#14\n",
    )
    assert detect_gateway() is None


def test_detect_gateway_is_none_when_every_probe_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr("asuswrt.router.subprocess.check_output", boom)
    assert detect_gateway() is None


# -- transport failures ----------------------------------------------------


def test_unreachable_names_the_symptom_and_both_causes(monkeypatch):
    """Neither cause is asserted: an observed instance was never pinned down."""
    monkeypatch.setattr("asuswrt.router.sys.platform", "darwin")
    monkeypatch.setenv("ROUTER_HOST", "10.0.0.1")

    message = explain_router_error(RuntimeError("Cannot connect: No route to host"))

    assert "No route to host" in message           # the original, kept
    assert "EHOSTUNREACH" in message               # the symptom
    assert "ARP" in message                        # cause one
    assert "Local Network" in message              # cause two
    assert "curl" in message                       # how to separate them
    assert "10.0.0.1" in message
    assert "usually means" not in message          # no single cause claimed


def test_other_router_errors_are_left_alone(monkeypatch):
    monkeypatch.setattr("asuswrt.router.sys.platform", "darwin")
    message = explain_router_error(RuntimeError("Login refused"))
    assert message == "Router error: Login refused"


def test_only_the_macos_cause_is_platform_specific(monkeypatch):
    """A stale ARP entry rejects the route on Linux too."""
    monkeypatch.setattr("asuswrt.router.sys.platform", "linux")
    monkeypatch.setenv("ROUTER_HOST", "10.0.0.1")

    message = explain_router_error(RuntimeError("No route to host"))

    assert "ARP" in message
    assert "Local Network" not in message


def test_unreachable_without_a_known_host_skips_the_curl_comparison(monkeypatch):
    monkeypatch.setattr("asuswrt.router.sys.platform", "darwin")
    monkeypatch.delenv("ROUTER_HOST", raising=False)
    monkeypatch.setattr("asuswrt.router.detect_gateway", lambda: None)

    message = explain_router_error(RuntimeError("No route to host"))

    assert "curl" not in message
    assert "the router" in message


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("yes", True),
     ("false", False), ("0", False), ("", False)],
)
def test_router_ssl_parsing(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(_empty_env(tmp_path)))
    monkeypatch.setenv("ROUTER_PASS", "secret")
    monkeypatch.setenv("ROUTER_SSL", value)
    assert load_config().use_ssl is expected


# -- nvram reads -----------------------------------------------------------


def test_read_nvram_returns_only_the_requested_names():
    router = FakeRouter()
    assert run(read_nvram(router, ["wl0_mfp", "wl1_mfp"])) == {
        "wl0_mfp": "0",
        "wl1_mfp": "0",
    }


def test_read_nvram_of_nothing_makes_no_request():
    """writers.nvram([]) is falsy; the request must be skipped, not sent empty."""
    router = FakeRouter()
    calls = []
    router.async_api_hook = lambda request: calls.append(request)  # would fail if awaited
    assert run(read_nvram(router, [])) == {}
    assert calls == []


# -- nvram writes ----------------------------------------------------------


def test_apply_nvram_writes_restarts_and_reads_back():
    router = FakeRouter()
    result = run(apply_nvram(router, {"wl1_mfp": "2"}, "restart_wireless"))

    assert router.services == [("restart_wireless", {"wl1_mfp": "2"})]
    assert result["ok"] is True
    assert result["before"] == {"wl1_mfp": "0"}
    assert result["after"] == {"wl1_mfp": "2"}
    assert result["unchanged"] == []


def test_apply_nvram_flags_a_value_the_firmware_ignored():
    router = FakeRouter(apply_writes=False)
    result = run(apply_nvram(router, {"wl1_country_code": "AU"}, "restart_wireless"))

    assert result["ok"] is True  # the service ran
    assert result["unchanged"] == ["wl1_country_code"]  # but nothing changed


def test_apply_nvram_reads_before_it_writes():
    """The before/after report is worthless if both samples are post-write."""
    router = FakeRouter()
    result = run(apply_nvram(router, {"wps_enable": "0"}, "restart_wireless"))
    assert result["before"]["wps_enable"] == "1"
    assert result["after"]["wps_enable"] == "0"


def test_apply_nvram_reports_a_failed_service():
    router = FakeRouter(service_ok=False, apply_writes=False)
    assert run(apply_nvram(router, {"wps_enable": "0"}, "restart_wireless"))["ok"] is False


# -- serialisation helpers -------------------------------------------------


def test_jsonable_unpacks_a_frozen_dataclass():
    rule = PortForwardingRule(name="Plex", ip_address="10.0.0.1", port="32400")
    assert jsonable(rule)["name"] == "Plex"
    assert jsonable(rule)["ip_address"] == "10.0.0.1"


def test_jsonable_renders_enums_by_value():
    assert jsonable(AsusPortForwarding.ON) == 1


def test_jsonable_recurses_through_containers():
    payload = {"rules": [PortForwardingRule(name="A")], "state": AsusPortForwarding.OFF}
    out = jsonable(payload)
    assert out["rules"][0]["name"] == "A"
    assert out["state"] == 0


def test_jsonable_stringifies_anything_it_does_not_understand():
    assert jsonable(object()).startswith("<object")


def test_jsonable_passes_scalars_through():
    assert jsonable([1, "a", True, None]) == [1, "a", True, None]


def test_enum_name_prefers_the_name_over_the_number():
    assert enum_name(AsusPortForwarding.ON) == "ON"
    assert enum_name(None) == "None"
    assert enum_name("wired") == "wired"


# -- CLI-level helpers -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", "ON"), ("0", "OFF"), (1, "ON"), ("none", "? ('none')"), (None, "? (None)")],
)
def test_onoff_keeps_unexpected_values_visible(value, expected):
    assert render._onoff(value) == expected


@pytest.mark.parametrize(
    ("selection", "expected"),
    [("2ghz", [0]), ("5ghz", [1]), ("both", [0, 1])],
)
def test_bands_maps_to_nvram_indexes(selection, expected):
    assert cli._bands(selection) == expected


def test_split_rulelist_handles_the_escaped_delimiter():
    assert ops._split_rulelist("&#60a&#60b") == ["a", "b"]
    assert ops._split_rulelist("") == []
    assert ops._split_rulelist(None) == []


def test_wpa_modes_and_mfp_tables_agree():
    """Every mode's default mfp must be a value the --mfp flag also accepts."""
    for _mode, (_auth, mfp) in cli.WPA_MODES.items():
        assert mfp in cli.MFP_NAMES
    assert set(cli.MFP_NAMES) == set(cli.MFP_VALUES.values())


def test_wifi_vars_covers_both_bands_and_wps():
    for name in ("wps_enable", "wps_enable_x", "wps_multiband"):
        assert name in cli.WIFI_VARS
    for index in (0, 1):
        for suffix in ("radio", "auth_mode_x", "crypto", "mfp", "country_code"):
            assert f"wl{index}_{suffix}" in cli.WIFI_VARS
