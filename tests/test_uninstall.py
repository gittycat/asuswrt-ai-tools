import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "uninstall.sh"


def run_uninstall(
    tmp_path: Path,
    state: dict,
    *args: str,
    project_mcp: dict | None = None,
    env_extra: dict[str, str] | None = None,
):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".claude.json").write_text(json.dumps(state))
    if project_mcp is not None:
        (tmp_path / ".mcp.json").write_text(json.dumps(project_mcp))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    python_link = bin_dir / "python3"
    if not python_link.exists():
        python_link.symlink_to(sys.executable)
    env = os.environ.copy()
    env.update(HOME=str(home), PATH=f"{bin_dir}:/usr/bin:/bin")
    env.pop("ASUSWRT_ENV_FILE", None)
    env.update(env_extra or {})

    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_uninstall_ignores_non_mcp_asuswrt_metadata(tmp_path):
    result = run_uninstall(
        tmp_path,
        {
            "skillUsage": {"asuswrt": {"usageCount": 5}},
            "projects": {"/example": {"exampleFiles": ["asuswrt.mcpb"]}},
        },
    )

    assert "Nothing to remove." in result.stdout
    assert "Remove by hand:" not in result.stdout


@pytest.mark.parametrize(
    ("state", "project_mcp"),
    [
        ({"mcpServers": {"asuswrt": {}}}, None),
        ({"projects": {"/example": {"mcpServers": {"asuswrt": {}}}}}, None),
        ({}, {"mcpServers": {"asuswrt": {}}}),
    ],
)
def test_uninstall_detects_only_real_mcp_entries(tmp_path, state, project_mcp):
    result = run_uninstall(tmp_path, state, project_mcp=project_mcp)

    # claude is not on PATH here, so the entry is reported rather than removed.
    assert "Remove by hand:" in result.stdout
    assert (
        "~/.claude.json  (asuswrt mcpServers entry — claude is not on PATH)"
        in result.stdout
    )


@pytest.mark.parametrize(
    "args",
    [("--yes",), ("--yes", "--repo-all"), ("--yes", "--repo-all", "--password")],
)
def test_uninstall_removes_the_cli_shims(tmp_path, args):
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    shims = ["asuswrt", "asuswrt-mcp", "asuswrt-probe", "asuswrt-chatgpt-connector"]
    for name in shims:
        (bin_dir / name).touch()

    result = run_uninstall(tmp_path, {}, *args)

    assert f"Removed {len(shims)} items:" in result.stdout
    for name in shims:
        assert f"  ~/.local/bin/{name}" in result.stdout
        assert not (bin_dir / name).exists()


def test_uninstall_abbreviates_home_as_a_bare_tilde(tmp_path):
    # bash 3.2, the bash macOS ships, keeps the backslash of a literal \~.
    executable = tmp_path / "home" / ".local" / "bin" / "asuswrt"
    executable.parent.mkdir(parents=True)
    executable.touch()

    result = run_uninstall(tmp_path, {})

    assert "  ~/.local/bin/asuswrt" in result.stdout
    assert "\\~" not in result.stdout


def test_uninstall_removes_chatgpt_connector_state(tmp_path):
    home = tmp_path / "home"
    label = "io.github.gittycat.asuswrt-chatgpt-connector"
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    state_dir = (
        home
        / "Library"
        / "Application Support"
        / "asuswrt-chatgpt-connector"
    )
    plist.parent.mkdir(parents=True)
    plist.write_text("plist")
    state_dir.mkdir(parents=True)
    (state_dir / "control-plane-api-key").write_text("secret")

    preview = run_uninstall(tmp_path, {})
    assert "2 items would be removed:" in preview.stdout
    assert f"  ~/Library/LaunchAgents/{label}.plist" in preview.stdout
    assert "  ~/Library/Application Support/asuswrt-chatgpt-connector" in preview.stdout
    assert plist.exists()
    assert state_dir.exists()

    run_uninstall(tmp_path, {}, "--yes")
    assert not plist.exists()
    assert not state_dir.exists()


def test_uninstall_reports_gemini_cli_registration(tmp_path):
    settings = tmp_path / "home" / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": {"asuswrt": {"command": "asuswrt-mcp"}}}))

    result = run_uninstall(tmp_path, {})

    assert "Remove by hand:" in result.stdout
    assert (
        "~/.gemini/settings.json  (asuswrt mcpServers entry — gemini is not on PATH)"
        in result.stdout
    )


def test_uninstall_ignores_unrelated_gemini_servers(tmp_path):
    settings = tmp_path / "home" / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": {"other": {"command": "other-mcp"}}}))

    result = run_uninstall(tmp_path, {})

    assert "Nothing to remove." in result.stdout
    assert "Remove by hand:" not in result.stdout


def test_uninstall_password_flag_covers_asuswrt_env_file(tmp_path):
    env_file = tmp_path / "elsewhere" / "router.env"
    env_file.parent.mkdir()
    env_file.write_text('ROUTER_PASS="secret"\n')
    env_extra = {"ASUSWRT_ENV_FILE": str(env_file)}

    kept = run_uninstall(tmp_path, {}, "--yes", env_extra=env_extra)
    assert "Nothing to remove." in kept.stdout
    assert env_file.exists()

    dropped = run_uninstall(tmp_path, {}, "--yes", "--password", env_extra=env_extra)
    assert str(env_file) in dropped.stdout
    assert not env_file.exists()
