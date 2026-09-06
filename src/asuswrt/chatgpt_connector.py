"""Install and manage the local ChatGPT connector on Apple-silicon macOS.

The connector is deliberately only a lifecycle wrapper. OpenAI's
``tunnel-client`` owns the HTTPS tunnel and starts this project's MCP server
over stdio. No router credential or OpenAI key is placed in the LaunchAgent
property list or on a process command line.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from asuswrt import config_setup
from asuswrt.config_setup import (
    dotenv_quote as _dotenv_quote,
    private_write as _private_write,
    prompt_nonempty as _prompt_nonempty,
)
from asuswrt.router import ConfigError


LABEL = "io.github.gittycat.asuswrt-chatgpt-connector"
TUNNEL_ID_RE = re.compile(r"^tunnel_[0-9a-f]{32}$")
PERMISSIONS = ("read-only", "writes", "dangerous")


class ConnectorError(RuntimeError):
    """An actionable setup or lifecycle failure."""


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def state(self) -> Path:
        return self.home / "Library" / "Application Support" / "asuswrt-chatgpt-connector"

    @property
    def bin(self) -> Path:
        return self.state / "bin"

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def tunnel_client(self) -> Path:
        return self.bin / "tunnel-client"

    @property
    def mcp_launcher(self) -> Path:
        return self.bin / "asuswrt-mcp-launcher"

    @property
    def profile(self) -> Path:
        return self.state / "tunnel-client.yaml"

    @property
    def runtime_key(self) -> Path:
        return self.state / "control-plane-api-key"

    @property
    def health_url(self) -> Path:
        return self.state / "health-url"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def router_env(self) -> Path:
        return self.home / ".config" / "asuswrt" / ".env"


def default_paths() -> Paths:
    return Paths(Path.home())


def require_supported_platform() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ConnectorError("asuswrt-chatgpt-connector supports Apple-silicon macOS only")
    release = platform.mac_ver()[0]
    try:
        major = int(release.split(".", 1)[0])
    except (TypeError, ValueError):
        raise ConnectorError("could not determine the macOS version") from None
    if major < 27:
        raise ConnectorError("asuswrt-chatgpt-connector requires macOS 27 or later")


def validate_tunnel_id(value: str) -> str:
    value = value.strip()
    if not TUNNEL_ID_RE.fullmatch(value):
        raise ConnectorError(
            "tunnel ID must be 'tunnel_' followed by 32 lowercase hexadecimal characters"
        )
    return value


def _quote_yaml(value: str | Path) -> str:
    # JSON strings are valid YAML strings and avoid adding a YAML dependency.
    return json.dumps(str(value))


def render_profile(paths: Paths, tunnel_id: str) -> str:
    return f"""config_version: 1
control_plane:
  base_url: "https://api.openai.com"
  tunnel_id: {_quote_yaml(validate_tunnel_id(tunnel_id))}
  api_key: {_quote_yaml(f"file:{paths.runtime_key}")}
health:
  listen_addr: "127.0.0.1:0"
  url_file: {_quote_yaml(paths.health_url)}
admin_ui:
  open_browser: false
log:
  level: info
  format: json
  file: {_quote_yaml(paths.logs / "tunnel-client.ndjson")}
mcp:
  commands:
    - channel: main
      command: {_quote_yaml(paths.mcp_launcher)}
"""


def render_mcp_launcher(paths: Paths, permission: str, python: Path) -> str:
    if permission not in PERMISSIONS:
        raise ConnectorError(f"permission must be one of: {', '.join(PERMISSIONS)}")
    gates = {
        "read-only": ("0", "0"),
        "writes": ("1", "0"),
        "dangerous": ("1", "1"),
    }
    writes, dangerous = gates[permission]
    return f"""#!/bin/sh
export ASUSWRT_ENV_FILE={sh_quote(paths.router_env)}
export ASUSWRT_MCP_ALLOW_WRITES={writes}
export ASUSWRT_MCP_ALLOW_DANGEROUS={dangerous}
exec {sh_quote(python)} -m asuswrt.mcp_server
"""


def sh_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def render_plist(paths: Paths) -> bytes:
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            str(paths.tunnel_client),
            "run",
            "--profile-file",
            str(paths.profile),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"NetworkState": True, "SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(paths.logs / "launch-agent.stdout.log"),
        "StandardErrorPath": str(paths.logs / "launch-agent.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _copy_executable(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ConnectorError(f"tunnel-client not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)


def _launchctl(
    *args: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        check=check,
        text=True,
        capture_output=capture,
    )


def _service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def stop_service(*, ignore_missing: bool = False) -> None:
    result = _launchctl("bootout", _service_target(), check=False, capture=True)
    if result.returncode and not ignore_missing:
        detail = (result.stderr or result.stdout).strip()
        raise ConnectorError(detail or "the connector service is not loaded")


def start_service(paths: Paths) -> None:
    if not paths.plist.is_file():
        raise ConnectorError(f"LaunchAgent is not installed: {paths.plist}")
    stop_service(ignore_missing=True)
    try:
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(paths.plist))
    except subprocess.CalledProcessError as exc:
        raise ConnectorError("launchctl could not start the connector") from exc


def ensure_router_config(paths: Paths) -> None:
    config_setup.ensure_router_config(paths.router_env)


def install(
    *,
    paths: Paths,
    tunnel_client: Path,
    tunnel_id: str,
    runtime_key: str,
    permission: str,
    start: bool = True,
    python: Path | None = None,
) -> None:
    require_supported_platform()
    tunnel_id = validate_tunnel_id(tunnel_id)
    if not runtime_key.strip():
        raise ConnectorError("the tunnel runtime API key cannot be empty")
    ensure_router_config(paths)

    paths.bin.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    _copy_executable(tunnel_client.expanduser().resolve(), paths.tunnel_client)
    _private_write(paths.runtime_key, runtime_key.strip() + "\n")
    _private_write(paths.profile, render_profile(paths, tunnel_id))
    _private_write(
        paths.mcp_launcher,
        render_mcp_launcher(paths, permission, python or Path(sys.executable)),
        0o700,
    )
    _private_write(paths.plist, render_plist(paths))
    if start:
        start_service(paths)


def service_status() -> subprocess.CompletedProcess[str]:
    return _launchctl("print", _service_target(), check=False, capture=True)


def doctor(paths: Paths) -> int:
    require_supported_platform()
    problems: list[str] = []
    required = (
        paths.tunnel_client, paths.mcp_launcher, paths.profile, paths.runtime_key, paths.plist
    )
    for path in required:
        if not path.is_file():
            problems.append(f"missing: {path}")
    if paths.runtime_key.is_file() and stat.S_IMODE(paths.runtime_key.stat().st_mode) & 0o077:
        problems.append(f"runtime key permissions are too broad: {paths.runtime_key}")
    if problems:
        for problem in problems:
            print(f"FAIL  {problem}")
        return 1

    result = subprocess.run(
        [str(paths.tunnel_client), "doctor", "--profile-file", str(paths.profile), "--explain"],
        text=True,
    )
    if result.returncode:
        return result.returncode
    status = service_status()
    if status.returncode:
        print("FAIL  LaunchAgent is installed but not loaded")
        return 1
    print("OK    connector files, tunnel profile, and LaunchAgent")
    return 0


def uninstall(paths: Paths, *, remove_router_config: bool = False) -> None:
    stop_service(ignore_missing=True)
    if paths.plist.exists():
        paths.plist.unlink()
    if paths.state.exists():
        shutil.rmtree(paths.state)
    if remove_router_config and paths.router_env.exists():
        paths.router_env.unlink()
        try:
            paths.router_env.parent.rmdir()
        except OSError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asuswrt-chatgpt-connector",
        description="Manage the private ASUSWRT MCP tunnel used by ChatGPT.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    install_cmd = commands.add_parser("install", help="configure and start the connector")
    install_cmd.add_argument("--tunnel-client", type=Path, required=True)
    install_cmd.add_argument("--tunnel-id")
    install_cmd.add_argument("--permission", choices=PERMISSIONS, default="read-only")
    install_cmd.add_argument("--no-start", action="store_true")

    commands.add_parser("start", help="start or restart the LaunchAgent")
    commands.add_parser("stop", help="stop the LaunchAgent")
    commands.add_parser("restart", help="restart the LaunchAgent")
    commands.add_parser("status", help="show LaunchAgent status")
    commands.add_parser("doctor", help="validate the complete local setup")
    uninstall_cmd = commands.add_parser("uninstall", help="remove the local connector")
    uninstall_cmd.add_argument(
        "--router-config",
        action="store_true",
        help="also remove router credentials",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = default_paths()
    try:
        if args.command == "install":
            tunnel_id = args.tunnel_id or _prompt_nonempty("OpenAI tunnel ID: ")
            runtime_key = _prompt_nonempty("OpenAI tunnel runtime API key: ", secret=True)
            install(
                paths=paths,
                tunnel_client=args.tunnel_client,
                tunnel_id=tunnel_id,
                runtime_key=runtime_key,
                permission=args.permission,
                start=not args.no_start,
            )
            print("Installed asuswrt-chatgpt-connector.")
            print("Create the ChatGPT app with Connection: Tunnel and this tunnel ID:")
            print(validate_tunnel_id(tunnel_id))
            return 0
        if args.command in {"start", "restart"}:
            start_service(paths)
            print("Started asuswrt-chatgpt-connector.")
            return 0
        if args.command == "stop":
            stop_service()
            print("Stopped asuswrt-chatgpt-connector.")
            return 0
        if args.command == "status":
            status = service_status()
            if status.returncode:
                print("stopped")
                return 1
            print(status.stdout, end="")
            return 0
        if args.command == "doctor":
            return doctor(paths)
        if args.command == "uninstall":
            uninstall(paths, remove_router_config=args.router_config)
            print("Removed the local connector. The remote OpenAI tunnel was not deleted.")
            return 0
    except (ConnectorError, ConfigError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")
