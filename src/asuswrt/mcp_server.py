"""asuswrt-mcp — a stdio MCP server over the same operations the CLI uses.

Flat module, not a package: `import mcp` here unambiguously means the MCP
Python SDK, not this project.

This module must never import `asuswrt.cli`. `ops.py` is the only thing the
CLI and this server share. stdout is the JSON-RPC channel on stdio — one
stray `print` under a tool call kills the connection, so all logging here
goes to stderr, and only ever names the tool, how long it took, and the
outcome. Never arguments, never config objects, never the password.

Connection model: one login per tool call, same as the CLI. A fresh
`connect()` happens inside `run()` for every call — no session is held open
between calls, and nothing here retries anything automatically. One
`asyncio.Lock` around every call serialises them, so an eager host cannot
open two logins at once.

Gates, read once at startup (changing them means restarting the server):

    ASUSWRT_MCP_ALLOW_WRITES=1      registers the 11 write tools
    ASUSWRT_MCP_ALLOW_DANGEROUS=1   (together with the gate above) registers
                                     reboot_router and upgrade_firmware

`1`, `true`, `yes` and `on` open a gate, case and surrounding space ignored;
everything else leaves it shut. The MCP Bundle passes a checkbox through as
`true`/`false`, and an unsubstituted `${user_config...}` template has to read
as off rather than as a non-empty string.

Every write tool takes confirm: bool = False. Without it, the tool connects,
reads what the change would touch, and returns a preview — it writes
nothing. With confirm=True it applies. See Appendix B of
docs/mcp-server-plan.md for the exact result shapes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from typing import Annotated, Any, Literal

from asusrouter import AsusData, AsusRouterError
from asusrouter.modules.port_forwarding import AsusPortForwarding, PortForwardingRule
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from asuswrt import ops
from asuswrt.ops import (
    CPU_SAMPLE_SECONDS,
    FIRMWARE_CHECK_SECONDS,
    MFP_NAMES,
    MFP_VALUES,
    WPA_MODES,
    _bands,
)
from asuswrt.router import (
    ConfigError,
    connect,
    explain_router_error,
    jsonable,
    load_env,
    port_forwarding_rules,
    read_nvram,
)

logger = logging.getLogger("asuswrt.mcp_server")

# Timeouts, per the plan: reads get 30 s; the firmware check and every write
# (preview or apply — both connect) get 60 s. No automatic retry of anything.
READ_TIMEOUT = 30.0
WRITE_TIMEOUT = 60.0

_lock = asyncio.Lock()

Port = Annotated[int, Field(ge=1, le=65535)]
GuestIndex = Literal[1, 2, 3]
Band = Literal["2ghz", "5ghz"]
AnyBand = Literal["2ghz", "5ghz", "both"]
Proto = Literal["TCP", "UDP", "BOTH", "OTHER"]
CountryCode = Annotated[str, Field(pattern=r"^[A-Za-z]{2}$")]
NvramName = Annotated[str, Field(min_length=1)]

Op = Callable[[Any], Awaitable[Any]]


def _configure_logging() -> None:
    """stderr only. stdout is the JSON-RPC channel."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s asuswrt-mcp %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def run(op: Op, *, name: str, timeout: float, write: bool = False) -> Any:
    """Connect once, run `op`, convert the result, map anticipated failures.

    `ConfigError`, `AsusRouterError` and a domain `ValueError` all become a
    `ToolError` — anything else escapes as a crash, which is what the SDK
    wants for a bug rather than an anticipated refusal. A timeout gets its
    own message: for a write, the change may or may not have landed, so the
    message says to check with the matching read tool rather than implying
    either outcome.
    """
    start = time.monotonic()
    outcome = "error"
    try:
        async with _lock:
            async with asyncio.timeout(timeout):
                try:
                    async with connect() as router:
                        result = jsonable(await op(router))
                    outcome = "ok"
                    return result
                except ConfigError as e:
                    raise ToolError(str(e)) from e
                except AsusRouterError as e:
                    raise ToolError(explain_router_error(e)) from e
                except ValueError as e:
                    raise ToolError(str(e)) from e
    except TimeoutError as e:
        if write:
            raise ToolError(
                f"Timed out waiting for the router ({name}). The change may or may "
                "not have been applied. Call the matching read tool to check."
            ) from e
        raise ToolError(f"Timed out waiting for the router ({name}). Try again.") from e
    finally:
        logger.info("tool=%s duration=%.2fs outcome=%s", name, time.monotonic() - start, outcome)


def _preview(change: str, current: Any, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "preview",
        "applied": False,
        "change": change,
        "warnings": warnings or [],
        "current": current,
    }


# ---------------------------------------------------------------------------
# Reads — always registered
# ---------------------------------------------------------------------------


async def get_router_overview() -> dict:
    """Broad router status in one call: identity, health, WAN, and counts for
    clients, firewall, parental control, port forwarding, guest wifi and
    wireless.

    Use for: how is my network doing, router status, check my router, is
    everything OK at home, give me a summary of my network.

    No firmware check (that costs ~5 s and answers a different question) and
    no raw nvram. Use this first; call a specific tool afterwards only for
    detailed rows (list_network_devices, list_port_forwards, ...).

    Policy: `fw_dos_x=0` and an unaccepted Trend Micro EULA (`TM_EULA=0`,
    `bwdpi_db_enable=0`) are the intended states, not gaps to close. Report
    them as configured and never propose enabling either; the reasons are in
    the get_firewall_and_filters description.
    """
    return await run(lambda router: ops.overview(router, CPU_SAMPLE_SECONDS, False, 0), name="get_router_overview", timeout=READ_TIMEOUT)


async def get_system() -> dict:
    """Router identity: model, firmware version, MAC, AiMesh. None of it
    changes until a reboot or a firmware flash."""
    return await run(ops.system, name="get_system", timeout=READ_TIMEOUT)


async def get_health() -> dict:
    """Live load: uptime, CPU usage (total and per core), RAM, and the
    current WAN link state and IP."""
    return await run(
        lambda router: ops.health(router, CPU_SAMPLE_SECONDS), name="get_health", timeout=READ_TIMEOUT
    )


async def get_internet_connection() -> dict:
    """Internet connection detail: link state, public IP, gateway, DNS.

    Use for: is my internet up, is the internet down, what is my public IP,
    what is my WAN address, is my connection working, what is my ISP giving
    me, why can I not get online."""
    return await run(ops.wan, name="get_internet_connection", timeout=READ_TIMEOUT)


async def list_network_devices(online_only: bool = False) -> dict:
    """Every device on the home network — currently connected and previously
    known — with name, vendor, IP, MAC, wired/wireless connection type and
    online state. The router keeps this list; nothing else on the LAN has it.

    Use for: how many devices are on my network, who is on my wifi, what is
    connected, list the devices, who is using my internet, is there an
    unknown device on my network, what is that machine's IP address, how many
    things are online right now.

    Returns `online_count` and `total_count` alongside the rows, so answer
    "how many" from those rather than counting. Pass online_only=True to drop
    devices that are known but not currently connected."""
    rows = await run(
        lambda router: ops.clients(router, online_only), name="list_network_devices", timeout=READ_TIMEOUT
    )
    # The counts are the answer to "how many devices are on my network"; a bare
    # list makes the model tally rows, which it can get wrong. Counted here and
    # not in ops.clients, which the CLI shares and renders as a table.
    return {
        "online_count": sum(1 for row in rows if row["online"]),
        "total_count": len(rows),
        "devices": rows,
    }


async def get_firewall_and_filters() -> dict:
    """Firewall, URL/keyword filter and parental-control nvram state.

    A null value means the variable does not exist under this firmware, which
    is not the same as the feature being off. Report it as undetermined.

    Two settings here are deliberate policy, not findings to report. DoS
    protection stays off (`fw_dos_x=0`): it only rate-limits new connections
    and ICMP to roughly one per second, which breaks legitimate traffic
    without stopping a real flood. The Trend Micro EULA stays unaccepted
    (`TM_EULA=0`, `bwdpi_db_enable=0`): it is one bundled consent covering
    AiProtection, Traffic Analyzer, Adaptive QoS and Web History, and
    accepting it starts sending browsing data to Trend Micro. Never propose
    enabling either. Sub-flags such as `wrs_mals_enable` may read `1` while
    `bwdpi_db_enable` is `0`: configured but not running, which is the wanted
    end state. Sources for both decisions are in docs/settings.md, under
    "Features with a settled answer".
    """
    return await run(ops.firewall, name="get_firewall_and_filters", timeout=READ_TIMEOUT)


async def get_dns() -> dict:
    """Which resolvers the router forwards to, and what it tells LAN clients.

    Returns the WAN unit the settings belong to (nvram is keyed `wan<N>_*`),
    the manual/automatic flag, both servers, the pair actually in use, and the
    DNS-over-TLS, DNSSEC, rebind-protection and LAN-advertisement settings.

    Worth reading whenever a specific site is slow to start or fails to load
    while the network is otherwise fine: a resolver that strips EDNS Client
    Subnet (1.1.1.1 does, deliberately) hides the client's network from
    authoritative servers, so CDNs that pick a node by resolver location can
    land traffic far away even though the connection itself is healthy.
    """
    return await run(ops.dns, name="get_dns", timeout=READ_TIMEOUT)


async def get_led() -> dict:
    """Whether the router's status lights are on (`led_val`: 1 on, 0 off)."""
    return await run(ops.led, name="get_led", timeout=READ_TIMEOUT)


async def get_upnp() -> dict:
    """Whether UPnP is on — whether devices can open their own inbound ports.

    Returns `enabled` plus the three underlying switches, because any one of
    them being `1` is enough for UPnP to be running; `enabled` is the OR of
    them, not a single variable.

    Policy: `enabled: false` is the intended state, not a gap. UPnP lets any
    program on the LAN ask the router to open a port from the internet to
    itself, with no authentication and nothing the owner is likely to see —
    the same effect as add_port_forward, minus the decision. Report it as
    correctly configured when it is off; if it is on, say plainly that this
    is worth turning off and offer set_upnp_enabled.
    """
    return await run(ops.upnp, name="get_upnp", timeout=READ_TIMEOUT)


async def get_parental_control() -> dict:
    """Parental control on/off state and its rules."""
    return await run(ops.parental, name="get_parental_control", timeout=READ_TIMEOUT)


async def list_port_forwards() -> dict:
    """Port forwarding rules, and whether forwarding is globally enabled. A
    rule does nothing while the global switch is off."""
    return await run(ops.port_forwarding, name="list_port_forwards", timeout=READ_TIMEOUT)


async def list_guest_networks() -> dict:
    """Guest wireless network state per band (SSID, enabled). Networks are
    numbered 1-3 per band; read this first to pick the right index for
    set_guest_network_enabled."""
    return await run(ops.guest, name="list_guest_networks", timeout=READ_TIMEOUT)


async def get_wifi_security() -> dict:
    """Radio, WPA mode, management frame protection, country code and WPS
    state for both bands.

    Use for: is my wifi secure, how safe is my wifi, is my network encrypted,
    what security is my wifi using, is WPS on, is WPA2 or WPA3 in use, can
    someone get onto my wifi.

    Three things decide "is my wifi secure?":
    WPS on (the PIN exchange is brute-forceable — turn it off, nothing modern
    needs it); `psk2` with mfp `disabled` (WPA2 with no management frame
    protection, so deauthentication attacks work freely); and a country code
    that differs between bands, where a band left on `AA` runs the most
    restrictive channel and power set and loses throughput."""
    return await run(ops.wifi, name="get_wifi_security", timeout=READ_TIMEOUT)


async def check_firmware_update() -> dict:
    """Ask the router to check ASUS for a firmware update, then report the
    installed and offered versions. This makes the router contact ASUS's
    servers and takes about 5 seconds — it is read-only (nothing is written)
    but not free, so do not call it as part of a routine status check. Use
    the returned `latest` version with upgrade_firmware's `to`.

    It always asks ASUS. The version cached on the router is refreshed only
    by its own periodic check, which is off by default
    (`webs_update_enable=0`), so the stored number can be arbitrarily old and
    is not used. Report three things and stop — what is installed, what is
    offered, and whether that is an upgrade. If ASUS could not be reached,
    say could not verify; that is not the same answer as up to date."""
    return await run(
        lambda router: ops.firmware(router, FIRMWARE_CHECK_SECONDS),
        name="check_firmware_update",
        timeout=WRITE_TIMEOUT,
    )


async def get_nvram(names: Annotated[list[NvramName], Field(min_length=1)]) -> dict:
    """Read raw nvram variables that have no dedicated tool. Read-only by
    design. Give at least one variable name.

    Policy: `fw_dos_x=0` and an unaccepted Trend Micro EULA (`TM_EULA=0`,
    `bwdpi_db_enable=0`) are the intended states, not gaps to close. Report
    them as configured and never propose enabling either; the reasons are in
    the get_firewall_and_filters description.

    Variable names, encodings, and which of them are verified against real
    hardware are in docs/settings.md. There is deliberately no write
    counterpart: every supported write is a named tool with its own
    validation and read-back. If a setting has no tool, say it needs the web
    UI rather than looking for a way to write it here.
    """
    return await run(lambda router: ops.nvram(router, list(names)), name="get_nvram", timeout=READ_TIMEOUT)


READS: list[tuple[Callable, ToolAnnotations]] = [
    (get_router_overview, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Router overview")),
    (get_system, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Router identity")),
    (get_health, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Router health")),
    (get_internet_connection, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Internet connection")),
    (get_dns, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="DNS settings")),
    (get_led, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Status lights")),
    (get_upnp, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="UPnP state")),
    (list_network_devices, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Network devices")),
    (
        get_firewall_and_filters,
        ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Firewall and filters"),
    ),
    (
        get_parental_control,
        ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Parental control"),
    ),
    (
        list_port_forwards,
        ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Port forwarding rules"),
    ),
    (
        list_guest_networks,
        ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Guest networks"),
    ),
    (get_wifi_security, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Wi-Fi security")),
    (
        check_firmware_update,
        ToolAnnotations(read_only_hint=True, open_world_hint=True, title="Check for firmware update"),
    ),
    (get_nvram, ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Read raw nvram")),
]


# ---------------------------------------------------------------------------
# Writes — registered only when ASUSWRT_MCP_ALLOW_WRITES=1
# ---------------------------------------------------------------------------


async def add_port_forward(
    name: str,
    port: Port,
    to_ip: str,
    to_port: Port | None = None,
    proto: Proto = "TCP",
    from_ip: str = "",
    force: bool = False,
    confirm: bool = False,
) -> dict:
    """Forward an external port to a device on the LAN.

    SAFETY: a forwarded port exposes that device to the internet. Name risky
    ports plainly in `name` (22=SSH, 3389=RDP, 445=SMB, database ports). A
    rule does nothing while port forwarding is globally OFF — check
    `global_state` in the result, or list_port_forwards.

    `to_port` forwards to a different internal port than the external one.
    `from_ip` restricts the rule to a single source address, which is the way
    to expose a risky service (SSH, RDP) without opening it to everyone.

    confirm=False (default) previews the rule and writes nothing. confirm=True
    applies it. A clash with an existing rule on the same external port and
    protocol is refused unless force=True.
    """
    rule = PortForwardingRule(
        name=name,
        ip_address=to_ip,
        port=str(to_port or port),
        protocol=proto,
        ip_external=from_ip,
        port_external=str(port),
    )
    description = f"add rule {name!r}: :{port} -> {to_ip}:{to_port or port} {proto}"

    async def op(router: Any) -> dict:
        if not confirm:
            current = await port_forwarding_rules(router)
            clash = [
                r for r in current if r.port_external == rule.port_external and r.protocol == rule.protocol
            ]
            if clash and not force:
                raise ValueError(
                    f"External port {rule.port_external}/{rule.protocol} is already "
                    f"forwarded to {clash[0].ip_address}:{clash[0].port}. "
                    "Pass force=True to add anyway."
                )
            data = await router.async_get_data(AsusData.PORT_FORWARDING) or {}
            warnings = []
            if data.get("state") == AsusPortForwarding.OFF:
                warnings.append(
                    "Port forwarding is globally OFF; this rule would do nothing until enabled."
                )
            return _preview(description, {"clash": clash, "global_state": data.get("state")}, warnings)

        result = await ops.port_forward_add(router, rule, force=force)
        if not result["applied"]:
            raise ValueError(f"FAILED: {description}")
        return {"status": "applied", "change": description, **result}

    return await run(op, name="add_port_forward", timeout=WRITE_TIMEOUT, write=True)


async def remove_port_forward(
    name: str | None = None,
    port: Port | None = None,
    proto: Proto | None = None,
    confirm: bool = False,
) -> dict:
    """Remove port forwarding rule(s) matching a name and/or external port.

    At least one of `name` or `port` is required. Every matching rule is
    removed — a port can carry both a TCP and a UDP rule, and asking by port
    alone removes both, which is why the preview lists every rule that would
    go.

    confirm=False previews which rules would be removed and writes nothing.
    confirm=True removes them.
    """
    if not name and not port:
        raise ToolError("Give at least one of name or port.")

    async def op(router: Any) -> dict:
        current = await port_forwarding_rules(router)
        doomed = [r for r in current if ops._pf_matches(r, name, port, proto)]
        if not doomed:
            raise ValueError("No matching rule.")

        description = "remove " + ", ".join(
            f"{r.name!r} (:{r.port_external} -> {r.ip_address}:{r.port})" for r in doomed
        )
        if not confirm:
            return _preview(description, {"rules": doomed})

        result = await ops.port_forward_remove(router, name=name, port=port, proto=proto)
        if not result["applied"]:
            raise ValueError(f"FAILED: {description}")
        return {"status": "applied", "change": description, **result}

    return await run(op, name="remove_port_forward", timeout=WRITE_TIMEOUT, write=True)


async def set_port_forwarding_enabled(enabled: bool, confirm: bool = False) -> dict:
    """Turn port forwarding globally on or off. Existing rules are
    unaffected but do nothing while this is off.

    confirm=False previews the current state and writes nothing. confirm=True
    applies it.
    """
    description = f"turn port forwarding globally {'ON' if enabled else 'OFF'}"

    async def op(router: Any) -> dict:
        if not confirm:
            return _preview(description, await ops.port_forwarding(router))
        result = await ops.set_port_forwarding(router, enabled)
        if not result["applied"]:
            raise ValueError(f"FAILED: {description}")
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_port_forwarding_enabled", timeout=WRITE_TIMEOUT, write=True)


async def set_parental_control_enabled(enabled: bool, confirm: bool = False) -> dict:
    """Turn parental control on or off.

    This is the feature switch only. Per-device rules are not settable here —
    adding one needs the router's web UI or the mobile app. Say so rather
    than improvising an nvram write. get_parental_control shows the rules
    that already exist.

    confirm=False previews the current state and writes nothing. confirm=True
    applies it.
    """
    description = f"turn parental control {'ON' if enabled else 'OFF'}"

    async def op(router: Any) -> dict:
        if not confirm:
            return _preview(description, await ops.parental(router))
        result = await ops.set_parental_control(router, enabled)
        if not result["applied"]:
            raise ValueError(f"FAILED: {description}")
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_parental_control_enabled", timeout=WRITE_TIMEOUT, write=True)


async def set_guest_network_enabled(
    band: Band, index: GuestIndex, enabled: bool, confirm: bool = False
) -> dict:
    """Turn one guest wireless network on or off.

    confirm=False previews the current state and writes nothing. confirm=True
    applies it.
    """
    description = f"turn guest network {band}_{index} {'ON' if enabled else 'OFF'}"
    key = f"{band}_{index}"

    async def op(router: Any) -> dict:
        if not confirm:
            data = await router.async_get_data(AsusData.GWLAN) or {}
            return _preview(description, {key: data.get(key)})
        result = await ops.set_guest_network(router, band, index, enabled)
        if not result["applied"]:
            raise ValueError(f"FAILED: {description}")
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_guest_network_enabled", timeout=WRITE_TIMEOUT, write=True)


async def set_wps_enabled(enabled: bool, confirm: bool = False) -> dict:
    """Turn Wi-Fi Protected Setup on or off on both bands.

    SAFETY: the WPS PIN exchange is brute-forceable — enabling it weakens
    the network.

    confirm=False previews the current nvram values and writes nothing.
    confirm=True applies it; the result's before/after/unchanged proves
    whether the firmware actually took the value.
    """
    description = f"turn WPS {'ON' if enabled else 'OFF'} on all bands"

    async def op(router: Any) -> dict:
        if not confirm:
            current = await read_nvram(router, ["wps_enable", "wps_enable_x", "wps_multiband"])
            warnings = ["The WPS PIN exchange is brute-forceable."] if enabled else []
            return _preview(description, current, warnings)
        result = await ops.set_wps(router, enabled)
        if result["unchanged"]:
            raise ValueError(
                f"FAILED: {description}. The firmware did not take: {result['unchanged']}"
            )
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_wps_enabled", timeout=WRITE_TIMEOUT, write=True)


async def set_wifi_security(
    mode: Literal["wpa2", "wpa2wpa3", "wpa3"],
    band: AnyBand = "both",
    mfp: Literal["disabled", "capable", "required"] | None = None,
    confirm: bool = False,
) -> dict:
    """Set the WPA mode and 802.11w management frame protection.

    SAFETY: this restarts both radios — every wireless client on the
    affected band(s) disconnects and reconnects.

    Prefer `wpa2wpa3` over `wpa3` on a household network: mixed mode turns on
    management frame protection while WPA2-only devices — printers, smart
    plugs, older IoT — keep working. If a legacy device will not associate
    afterwards, relax the frame protection (mfp `disabled`) on that band
    rather than dropping the whole network back to WPA2.

    confirm=False previews the current nvram values and writes nothing.
    confirm=True applies it; the result's before/after/unchanged proves
    whether the firmware actually took the value.
    """
    auth, default_mfp = WPA_MODES[mode]
    mfp_value = MFP_VALUES[mfp] if mfp else default_mfp
    description = (
        f"set {band} to {mode} (auth_mode_x={auth}, crypto=aes, mfp={MFP_NAMES[mfp_value]}) "
        "— every wireless client on the affected band(s) reconnects"
    )

    async def op(router: Any) -> dict:
        names = [f"wl{i}_{suffix}" for i in _bands(band) for suffix in ("auth_mode_x", "crypto", "mfp")]
        if not confirm:
            current = await read_nvram(router, names)
            return _preview(
                description, current, ["Every wireless client on the affected band(s) will reconnect."]
            )
        result = await ops.set_wifi_security(router, band, mode, mfp)
        if result["unchanged"]:
            raise ValueError(
                f"FAILED: {description}. The firmware did not take: {result['unchanged']}"
            )
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_wifi_security", timeout=WRITE_TIMEOUT, write=True)


async def set_wifi_country(band: AnyBand, code: CountryCode, confirm: bool = False) -> dict:
    """Set the regulatory country code.

    SAFETY: this restarts both radios — every wireless client on the
    affected band(s) disconnects and reconnects. Country code is usually
    locked to the hardware SKU on stock firmware, so the write may be
    accepted and then silently ignored; the result's before/after/unchanged
    is what actually decides success, not the acknowledgement. When it does
    not stick, say so and point at the web UI rather than retrying.

    Read `reg_spec` and `location_code` with get_nvram first — they say which
    region the firmware believes it is in. Picking the wrong code is a
    regulatory problem, not just a performance one.

    confirm=False previews the current nvram values and writes nothing.
    confirm=True applies it.
    """
    code = code.upper()
    description = f"set the {band} country code to {code}"

    async def op(router: Any) -> dict:
        names = [f"wl{i}_country_code" for i in _bands(band)]
        if not confirm:
            current = await read_nvram(router, names)
            return _preview(
                description,
                current,
                ["Every wireless client on the affected band(s) will reconnect."],
            )
        result = await ops.set_wifi_country(router, band, code)
        if result["unchanged"]:
            raise ValueError(
                f"FAILED: {description}. Country code is usually locked to the hardware "
                f"SKU on stock firmware. Did not take: {result['unchanged']}"
            )
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_wifi_country", timeout=WRITE_TIMEOUT, write=True)


async def set_wan_dns(
    server1: str | None = None,
    server2: str | None = None,
    automatic: bool = False,
    confirm: bool = False,
) -> dict:
    """Set which resolvers the router forwards DNS to, or hand it back to the ISP.

    Pass `automatic=true` for the ISP's own servers, or `server1` (and
    optionally `server2`) for explicit ones. IPv4 only — AsusWRT keeps IPv6
    resolvers in a separate field this tool does not write.

    SAFETY: every device on the network resolves through this. Bad servers
    break name resolution house-wide. It is recoverable — call this again with
    `automatic=true`, which keeps working because this tool reaches the router
    by address and never depended on DNS.

    Choosing a resolver is not cosmetic. A resolver that strips EDNS Client
    Subnet (1.1.1.1 does, deliberately, for privacy) hides the client's
    network from authoritative servers, so a CDN that picks a node by resolver
    location can send video and download traffic to a distant one while the
    connection itself measures fine. Resolvers that send ECS include 8.8.8.8 /
    8.8.4.4 and 9.9.9.11; 1.1.1.1 and plain 9.9.9.9 do not.

    confirm=False previews the current values and writes nothing.
    confirm=True applies it; the result's before/after/unchanged proves
    whether the firmware actually took the value.
    """
    if automatic:
        description = "hand the router's WAN DNS back to the ISP's servers"
    else:
        wanted = " ".join(s for s in (server1, server2) if s)
        description = f"point the router's WAN DNS at {wanted}"

    async def op(router: Any) -> dict:
        if not confirm:
            unit = await ops._wan_unit(router)
            current = await read_nvram(router, [*ops._dns_names(unit), *ops.DNS_VARS])
            return _preview(
                description,
                {"unit": unit, "nvram": current},
                [
                    "Every device on the network resolves through this.",
                    "Recover with set_wan_dns(automatic=true) if resolution breaks.",
                ],
            )
        result = await ops.set_dns(router, server1, server2, automatic)
        if result["unchanged"]:
            raise ValueError(
                f"FAILED: {description}. The firmware did not take: {result['unchanged']}"
            )
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_wan_dns", timeout=WRITE_TIMEOUT, write=True)


async def set_upnp_enabled(enabled: bool, confirm: bool = False) -> dict:
    """Turn UPnP on or off.

    SAFETY: off is the safe state and the one to prefer. Enabling it lets any
    program on the network open an inbound port from the internet to itself,
    without authentication and without a record the owner is likely to
    check — malware and misconfigured software both use it. Never enable it
    unless the user asked for UPnP in those words, and say what it exposes
    before doing so. Existing port forwards are unaffected either way; they
    live in a separate list (list_port_forwards).

    Writes all three UPnP switches, since which one this firmware treats as
    the master is not settled.

    confirm=False previews the current values and writes nothing.
    confirm=True applies it; the result's before/after/unchanged proves
    whether the firmware actually took the value.
    """
    description = f"turn UPnP {'ON' if enabled else 'OFF'}"

    async def op(router: Any) -> dict:
        if not confirm:
            warnings = (
                [
                    "Any program on the network could then open an inbound port "
                    "to itself without asking."
                ]
                if enabled
                else []
            )
            return _preview(description, await ops.upnp(router), warnings)
        result = await ops.set_upnp(router, enabled)
        if result["unchanged"]:
            raise ValueError(
                f"FAILED: {description}. The firmware did not take: {result['unchanged']}"
            )
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_upnp_enabled", timeout=WRITE_TIMEOUT, write=True)


async def set_led_enabled(enabled: bool, confirm: bool = False) -> dict:
    """Turn the router's status lights on or off.

    Cosmetic — it changes nothing about how the router routes. Turning them
    off is a common want for a router in a bedroom or living room.

    confirm=False previews the current value and writes nothing.
    confirm=True applies it; the result's before/after/unchanged proves
    whether the firmware actually took the value.
    """
    description = f"turn the router's status LEDs {'ON' if enabled else 'OFF'}"

    async def op(router: Any) -> dict:
        if not confirm:
            return _preview(description, await read_nvram(router, [ops.LED_VAR]))
        result = await ops.set_led(router, enabled)
        if result["unchanged"]:
            raise ValueError(
                f"FAILED: {description}. The firmware did not take: {result['unchanged']}"
            )
        return {"status": "applied", "change": description, **result}

    return await run(op, name="set_led_enabled", timeout=WRITE_TIMEOUT, write=True)


WRITES: list[tuple[Callable, ToolAnnotations]] = [
    (
        add_port_forward,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=False, open_world_hint=True, title="Add port forward"
        ),
    ),
    (
        remove_port_forward,
        ToolAnnotations(
            destructive_hint=True, idempotent_hint=False, open_world_hint=True, title="Remove port forward"
        ),
    ),
    (
        set_port_forwarding_enabled,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="Port forwarding on/off"
        ),
    ),
    (
        set_parental_control_enabled,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="Parental control on/off"
        ),
    ),
    (
        set_guest_network_enabled,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="Guest network on/off"
        ),
    ),
    (
        set_wps_enabled,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="WPS on/off"
        ),
    ),
    (
        set_wifi_security,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="Set Wi-Fi security"
        ),
    ),
    (
        set_wifi_country,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="Wireless country code"
        ),
    ),
    (
        set_wan_dns,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="WAN DNS servers"
        ),
    ),
    (
        set_upnp_enabled,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=True, title="UPnP on/off"
        ),
    ),
    (
        set_led_enabled,
        ToolAnnotations(
            destructive_hint=False, idempotent_hint=True, open_world_hint=False, title="Status lights on/off"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Dangerous — registered only when ASUSWRT_MCP_ALLOW_WRITES=1 AND
# ASUSWRT_MCP_ALLOW_DANGEROUS=1
# ---------------------------------------------------------------------------


async def reboot_router(confirm: bool = False) -> dict:
    """Reboot the router. Every connection in the house drops for about a
    minute.

    SAFETY: never call this unless the user asked for a reboot in those
    words.

    confirm=False previews and writes nothing. confirm=True requests the
    reboot; the API only acknowledges the request, so call get_system
    afterwards to confirm it came back up.
    """
    description = "REBOOT the router (drops every connection for ~60 s)"

    async def op(router: Any) -> dict:
        if not confirm:
            return _preview(description, {})
        result = await ops.reboot(router)
        if not result["requested"]:
            raise ValueError("FAILED: the router refused the reboot request")
        return {"status": "requested", **result}

    return await run(op, name="reboot_router", timeout=WRITE_TIMEOUT, write=True)


async def upgrade_firmware(to: str, beta: bool = False, confirm: bool = False) -> dict:
    """Download and flash new firmware.

    SAFETY: this writes flash and reboots the router. Every connection in
    the house drops for several minutes, and losing power while flash is
    being written can brick the router. The API only acknowledges the
    request — it reports no progress and nothing about whether the flash
    itself succeeds. Call get_system afterwards to confirm the new version.

    `to` is required and must be the exact version string from the
    check_firmware_update call you just made — never from an earlier run and
    never from memory. This refuses rather than flash a version nobody named,
    and refuses outright when the latest version could not be verified.

    Show the user the offered version and its release note, and tell them the
    house loses connectivity for five to ten minutes, before you pass
    confirm=True.

    confirm=False previews what would be flashed and writes nothing.
    confirm=True applies it.
    """

    async def op(router: Any) -> dict:
        if not confirm:
            # A self-contained read: firmware_upgrade (below) does its own
            # independent fetch, meant for exactly one apply call. Preview
            # needs to describe the change without applying, so it repeats
            # the same fetch-and-resolve rather than reuse that function.
            payload = await ops.firmware(router, FIRMWARE_CHECK_SECONDS)
            current, latest = ops._resolve_upgrade_target(payload, beta)
            if to != latest:
                raise ValueError(
                    "`to` does not match what the router is offering.\n"
                    f"  requested  {to}\n"
                    f"  offered    {latest}"
                )
            description = (
                f"FLASH firmware {latest} over {current}. The router downloads from "
                "ASUS, writes flash, then reboots. Every connection in the house "
                "drops for several minutes. Losing power while flash is being "
                "written can brick the router."
            )
            return _preview(
                description,
                {"current": current, "latest": latest},
                ["Losing power during the flash can brick the router."],
            )

        result = await ops.firmware_upgrade(router, FIRMWARE_CHECK_SECONDS, to, beta)
        return {"status": "requested", **result}

    return await run(op, name="upgrade_firmware", timeout=WRITE_TIMEOUT, write=True)


DANGEROUS: list[tuple[Callable, ToolAnnotations]] = [
    (
        reboot_router,
        ToolAnnotations(destructive_hint=True, idempotent_hint=False, open_world_hint=False, title="Reboot router"),
    ),
    (
        upgrade_firmware,
        ToolAnnotations(
            destructive_hint=True, idempotent_hint=False, open_world_hint=True, title="Upgrade firmware"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Server assembly
# ---------------------------------------------------------------------------


# Server-level instructions. These render into the host's system prompt before
# the model decides how to answer anything, which makes this the only channel
# that reaches it ahead of its choice of mechanism. A model that has already
# committed to "shell in and scan the LAN" never searches for these tools, so
# the text triggers on the subject of the question (the home router and its
# network), not on the mechanism — gating every ping or ssh on a router check
# would tax unrelated work. Kept short: it costs tokens on every turn of every
# conversation for as long as the server is installed.
INSTRUCTIONS = """\
The home router is an ASUS (AsusWRT); send questions about it, or about the \
devices on the home network, to these tools: what is connected and how many, \
wifi security, internet/WAN state, DNS, port forwarding, firewall. The router \
holds the authoritative list — do not derive it with arp, nmap or a ping sweep.

get_router_overview for broad status, a specific tool for detail rows.
"""


def build_server(*, allow_writes: bool = False, allow_dangerous: bool = False) -> MCPServer:
    """Assemble a fresh server with the tool set the two gates allow.

    A fresh instance per call (rather than a shared module-level singleton)
    is what makes this testable without env-var/reload gymnastics: tests
    build a server per gate combination and inspect it directly.
    """
    server = MCPServer("asuswrt", version=version("asuswrt"), instructions=INSTRUCTIONS)
    for fn, annotations in READS:
        server.add_tool(fn, annotations=annotations)
    if allow_writes:
        for fn, annotations in WRITES:
            server.add_tool(fn, annotations=annotations)
        if allow_dangerous:
            for fn, annotations in DANGEROUS:
                server.add_tool(fn, annotations=annotations)
    return server


_GATE_OPEN = frozenset({"1", "true", "yes", "on"})


def gate_open(name: str) -> bool:
    """A gate opens only on an explicit yes. Anything else keeps it shut."""
    return os.getenv(name, "").strip().lower() in _GATE_OPEN


def main() -> None:
    _configure_logging()
    load_env()  # populate the environment before the gates are read
    allow_writes = gate_open("ASUSWRT_MCP_ALLOW_WRITES")
    allow_dangerous = gate_open("ASUSWRT_MCP_ALLOW_DANGEROUS")

    server = build_server(allow_writes=allow_writes, allow_dangerous=allow_dangerous)
    logger.info(
        "starting: reads=%d writes=%s dangerous=%s",
        len(READS),
        allow_writes,
        allow_dangerous,
    )
    server.run()


if __name__ == "__main__":
    main()
