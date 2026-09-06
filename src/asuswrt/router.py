"""Connection helper shared by the CLI and the probe.

Credentials come from the environment (or a .env file next to the project):

    ROUTER_HOST   default: the current default gateway
    ROUTER_USER   default admin
    ROUTER_PASS   required
    ROUTER_SSL    default false
    ROUTER_PORT   optional
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv

from asusrouter import AsusData, AsusRouter


ROUTER_ENV_NAMES = (
    "ROUTER_HOST",
    "ROUTER_USER",
    "ROUTER_PASS",
    "ROUTER_SSL",
    "ROUTER_PORT",
)


class ConfigError(RuntimeError):
    """Raised when the router credentials are missing or unusable."""


@dataclasses.dataclass(frozen=True)
class RouterConfig:
    """Everything needed to reach the router."""

    host: str
    username: str
    password: str
    use_ssl: bool
    port: int | None

    @property
    def url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        suffix = f":{self.port}" if self.port else ""
        return f"{scheme}://{self.host}{suffix}"


# Each entry is a command that reports the default route and the pattern that
# picks the gateway out of its output. They are tried in order and a command
# that is absent or fails is skipped, so the same list covers macOS, the BSDs
# and Linux without branching on sys.platform.
_GATEWAY_PROBES: list[tuple[list[str], re.Pattern[str]]] = [
    # macOS / BSD: "   gateway: 192.168.50.1"
    (["route", "-n", "get", "default"], re.compile(r"^\s*gateway:\s*(\S+)", re.M)),
    # Linux iproute2: "default via 192.168.1.1 dev eth0 ..."
    (["ip", "-4", "route", "show", "default"], re.compile(r"\bdefault\s+via\s+(\S+)")),
    # BSD netstat fallback: "default   192.168.50.1   UGScg   en0"
    (["netstat", "-rn", "-f", "inet"], re.compile(r"^default\s+(\S+)", re.M)),
]


def detect_gateway() -> str | None:
    """The address of the current default gateway, or None if unknown.

    A home router *is* the default gateway, so this is the address to talk to
    when the user has not named one. There is no portable API for it, hence
    the shell probes above. Nothing is cached: the MCP server is long-lived
    and the gateway changes when the machine moves network or a VPN comes up.

    Returns None rather than falling back to a vendor default. Guessing
    192.168.50.1 on a 192.168.1.x network produces a connection error that
    looks like a broken router instead of an unset ROUTER_HOST.
    """
    for argv, pattern in _GATEWAY_PROBES:
        try:
            output = subprocess.check_output(
                argv, text=True, timeout=5, stderr=subprocess.DEVNULL
            )
        except (OSError, subprocess.SubprocessError):
            continue
        match = pattern.search(output)
        if not match:
            continue
        try:
            # "link#14" appears here for a point-to-point default route.
            return str(ipaddress.ip_address(match.group(1)))
        except ValueError:
            continue
    return None


def config_paths() -> list[Path]:
    """Candidate .env locations, most specific first.

    The CLI is normally installed globally (`uv tool install .`) and then run
    from whatever directory the user or agent happens to be in, so a bare
    load_dotenv() on the working directory is not enough.
    """
    paths = []
    if override := os.getenv("ASUSWRT_ENV_FILE"):
        paths.append(Path(override).expanduser())
    paths.append(Path.cwd() / ".env")
    paths.append(Path.home() / ".config" / "asuswrt" / ".env")
    return paths


def _searched_paths() -> str:
    """The config locations, indented for the body of an error message."""
    return "\n  ".join(str(path) for path in config_paths())


def _drop_blank_env() -> None:
    """Unset any ROUTER_* variable that is present but empty.

    The GUI installers (the Claude Code plugin dialog, the Claude Desktop
    extension) substitute an empty string for a field the user left blank, so
    the variable arrives set-but-empty rather than absent. That matters twice:
    load_dotenv() does not override what is already set, so an empty
    ROUTER_PASS would shadow a working .env; and os.getenv(name, default)
    hands back the empty string instead of the default, so an empty
    ROUTER_USER would be sent to the router in place of "admin".
    """
    for name in ROUTER_ENV_NAMES:
        if not os.environ.get(name, "").strip():
            os.environ.pop(name, None)


def load_env() -> None:
    """Load a .env file into the environment, if one exists.

    Split out of load_config() so a caller that only needs the environment
    populated — the MCP server, deciding its write/dangerous gates before it
    knows whether ROUTER_PASS is even set — can do that without also
    requiring a password.
    """
    _drop_blank_env()
    for path in config_paths():
        if path.is_file():
            load_dotenv(path, interpolate=False)
            break


def load_config() -> RouterConfig:
    """Read the router configuration from the environment."""
    load_env()

    password = os.getenv("ROUTER_PASS")
    if not password:
        raise ConfigError(
            "No router password saved yet. Run:\n\n  asuswrt setup\n\n"
            "It asks for the router login and writes it out. Searched:\n  "
            + _searched_paths()
        )

    host = os.getenv("ROUTER_HOST") or detect_gateway()
    if not host:
        raise ConfigError(
            "The router's address is not set and the default gateway could "
            "not be detected. Run:\n\n  asuswrt setup --host ROUTER_ADDRESS\n\n"
            "Searched:\n  " + _searched_paths()
        )

    port = os.getenv("ROUTER_PORT")
    return RouterConfig(
        host=host,
        username=os.getenv("ROUTER_USER", "admin"),
        password=password,
        use_ssl=os.getenv("ROUTER_SSL", "false").lower() in {"1", "true", "yes"},
        port=int(port) if port else None,
    )


# EHOSTUNREACH on macOS (65) and Linux (113). aiohttp puts the text in the
# message; asusrouter then wraps that message in its own exception, so the
# string is all that survives to the surface.
_UNREACHABLE = ("no route to host", "host is unreachable", "errno 65", "errno 113")


def explain_router_error(err: Exception) -> str:
    """Turn a transport failure into something the reader can act on.

    EHOSTUNREACH is reported the same way whether the router is absent or the
    machine refused to route to it, and the refusal cases return instantly,
    which reads as a dead router. Name the symptom and both known causes
    rather than asserting one: an observed instance of this had curl reaching
    the router while this program could not, and the cause was never pinned
    down. See docs/troubleshooting.md.
    """
    message = f"Router error: {err}"
    if not any(needle in str(err).lower() for needle in _UNREACHABLE):
        return message

    host = os.getenv("ROUTER_HOST") or detect_gateway()
    target = host or "the router"

    causes = [
        "  * A stale ARP entry for a host on your own subnet. The kernel\n"
        "    rejects the route until the neighbour resolves again. Retry, or\n"
        f"    force resolution first:  ping -c 2 {target}",
    ]
    if sys.platform == "darwin":
        causes.append(
            "  * macOS Local Network privacy denying this program:\n"
            "      System Settings > Privacy & Security > Local Network\n"
            "    Enable your terminal, or:\n"
            f"      {sys.executable}"
        )

    parts = [
        message,
        "",
        "The connection was rejected before any packet left this machine "
        "(EHOSTUNREACH), so the router itself may be fine. Known causes:",
        "",
        "\n\n".join(causes),
    ]
    if host:
        parts += [
            "",
            "To tell them apart, run:",
            f"     curl -sS -o /dev/null -w '%{{http_code}}\\n' http://{host}/",
            f"A 200 from curl while asuswrt fails means the block is local to "
            f"this program, not the network. Both failing means {host} really "
            f"is unreachable — check ROUTER_HOST.",
        ]
    return "\n".join(parts)


@asynccontextmanager
async def connect(config: RouterConfig | None = None) -> AsyncIterator[AsusRouter]:
    """Open an authenticated session and always close it again."""
    config = config or load_config()

    async with aiohttp.ClientSession() as session:
        router = AsusRouter(
            hostname=config.host,
            username=config.username,
            password=config.password,
            port=config.port,
            use_ssl=config.use_ssl,
            session=session,
        )
        if not await router.async_connect():
            raise ConfigError(f"Login refused by {config.url}. Check ROUTER_USER/ROUTER_PASS.")
        try:
            yield router
        finally:
            await router.async_disconnect()


async def read_nvram(router: AsusRouter, names: list[str]) -> dict[str, Any]:
    """Read raw nvram variables over the HTTP API.

    This is the same mechanism the library uses to collect device identity
    (see asusrouter/modules/identity.py::collect_identity), exposed here as
    an escape hatch for settings that have no dedicated data type.
    """
    from asusrouter.tools import writers  # local import: internal helper

    request = writers.nvram(names)
    if not request:
        return {}
    return await router.async_api_hook(request)


async def port_forwarding_rules(router: AsusRouter) -> list[Any]:
    """Return the current port forwarding rules.

    The library omits the "rules" key entirely when vts_rulelist is empty
    (asusrouter/modules/endpoint/hook.py::process_port_forwarding), so a
    plain ["rules"] lookup raises KeyError on a router with no rules.
    """
    data = await router.async_get_data(AsusData.PORT_FORWARDING) or {}
    return list(data.get("rules") or [])


def jsonable(value: Any) -> Any:
    """Convert library objects into something json.dumps can handle."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def enum_name(value: Any) -> str:
    """Render an IntEnum as its name rather than a bare number."""
    return value.name if isinstance(value, Enum) else str(value)


async def apply_nvram(
    router: AsusRouter,
    values: dict[str, str],
    service: str,
    expect_modify: bool = True,
) -> dict[str, Any]:
    """Write nvram variables, restart a service, and report what actually stuck.

    This is the same write path the library uses for port forwarding
    (async_apply_port_forwarding_rules -> async_run_service): the arguments
    dict becomes nvram assignments, `apply` adds action_mode=apply, and the
    named service is restarted afterwards.

    Some variables are read-only on stock firmware even though the write is
    accepted — country code is the known case — so the values are read back
    and returned as before/after rather than trusting the success flag.

    `expect_modify` decides what the router's acknowledgement means. With it
    set, `ok` is the router's own `modify` flag (asusrouter
    modules/service.py::async_call_service). A few services never return that
    flag, and for those a successful write would otherwise report as a
    failure — `start_ctrl_led` is the known case, and the library passes
    expect_modify=False for exactly that reason (modules/led.py). Pass False
    for those and let the read-back decide.
    """
    names = list(values)
    before = await read_nvram(router, names)
    ok = await router.async_run_service(
        service=service,
        arguments=dict(values),
        apply=True,
        expect_modify=expect_modify,
    )
    after = await read_nvram(router, names)

    return {
        "ok": ok,
        "before": before,
        "after": after,
        "unchanged": [n for n in names if str(after.get(n)) != str(values[n])],
    }
