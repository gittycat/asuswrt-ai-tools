"""The shared credential-file writer behind `asuswrt setup`."""

from __future__ import annotations

import stat

import pytest

from asuswrt import config_setup
from asuswrt.router import ConfigError, load_config


def _answers(monkeypatch, username: str, password: str) -> None:
    monkeypatch.setattr("builtins.input", lambda *a: username)
    monkeypatch.setattr(config_setup.getpass, "getpass", lambda *a: password)


def test_default_env_path_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(tmp_path / "custom.env"))
    assert config_setup.default_env_path() == tmp_path / "custom.env"


def test_the_credential_file_is_private_from_the_moment_it_exists(tmp_path):
    path = tmp_path / "nested" / ".env"
    config_setup.write_credentials(path, "admin", "secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_credentials_survive_a_round_trip_through_the_loader(monkeypatch, tmp_path):
    """The quoting has to hold for passwords the shell would otherwise eat."""
    path = tmp_path / ".env"
    password = 'a b#"c\\d${HOME}'
    config_setup.write_credentials(path, "me", password)

    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(path))
    for name in ("ROUTER_USER", "ROUTER_PASS", "ROUTER_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("asuswrt.router.detect_gateway", lambda: "10.0.0.1")

    config = load_config()
    assert (config.username, config.password) == ("me", password)


def test_a_nul_in_a_credential_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        config_setup.write_credentials(tmp_path / ".env", "admin", "a\x00b")


def test_setup_refuses_to_clobber_without_force(monkeypatch, tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text("ROUTER_PASS=old\n")
    _answers(monkeypatch, "me", "new")

    assert config_setup.run_setup(path) == 1
    assert path.read_text() == "ROUTER_PASS=old\n"
    assert "--force" in capsys.readouterr().out


def test_setup_replaces_the_file_with_force(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("ROUTER_PASS=old\n")
    _answers(monkeypatch, "me", "new")

    assert config_setup.run_setup(path, force=True) == 0
    assert 'ROUTER_PASS="new"' in path.read_text()


def test_a_blank_username_falls_back_to_admin(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    _answers(monkeypatch, "", "secret")

    assert config_setup.run_setup(path) == 0
    assert 'ROUTER_USER="admin"' in path.read_text()


def test_setup_records_a_host_when_the_gateway_is_not_the_router(
    monkeypatch, tmp_path
):
    path = tmp_path / ".env"
    _answers(monkeypatch, "me", "secret")

    assert config_setup.run_setup(path, host="192.168.2.1") == 0

    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(path))
    for name in ("ROUTER_HOST", "ROUTER_USER", "ROUTER_PASS"):
        monkeypatch.delenv(name, raising=False)
    assert load_config().host == "192.168.2.1"


def test_setup_leaves_the_host_out_when_the_gateway_will_do(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    _answers(monkeypatch, "me", "secret")

    assert config_setup.run_setup(path) == 0
    assert "ROUTER_HOST" not in path.read_text()


def test_ensure_router_config_leaves_an_existing_file_alone(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("ROUTER_PASS=old\n")
    _answers(monkeypatch, "me", "new")

    config_setup.ensure_router_config(path)
    assert path.read_text() == "ROUTER_PASS=old\n"
