"""asuswrt — command line control for an ASUS router over its HTTP API.

Read commands print a human-readable summary, or JSON with --json.
Every command that changes the router asks first. --yes skips the asking;
with no terminal to ask at, the command prints what it would do and exits 3.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import sys
from typing import Any

from asusrouter import AsusRouterError
from asusrouter.modules.parental_control import AsusParentalControl
from asusrouter.modules.port_forwarding import AsusPortForwarding, PortForwardingRule
from asusrouter.modules.wlan import AsusWLAN

from asuswrt import config_setup, ops
from asuswrt.cli import render
from asuswrt.ops import (
    CPU_SAMPLE_SECONDS,
    FIRMWARE_CHECK_SECONDS,
    MFP_NAMES,
    MFP_VALUES,
    WIFI_VARS,
    WPA_MODES,
    _bands,
)
from asuswrt.router import (
    ConfigError,
    connect,
    explain_router_error,
    jsonable,
    port_forwarding_rules,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_NEEDS_CONFIRM = 3


def emit(payload: Any, lines: list[str], as_json: bool) -> None:
    """Print either machine-readable JSON or the human summary."""
    if as_json:
        print(json.dumps(jsonable(payload), indent=2, default=str))
    else:
        print("\n".join(lines))


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@contextlib.asynccontextmanager
async def progress(message: str) -> Any:
    """Spin on stderr while something slow runs.

    Silent unless stderr is a terminal: piped and captured output stays byte
    for byte what it would have been, and --json on stdout is unaffected
    either way.
    """
    if not sys.stderr.isatty():
        yield
        return

    async def spin() -> None:
        for i in itertools.count():
            frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
            print(f"\r{frame} {message}", end="", file=sys.stderr, flush=True)
            await asyncio.sleep(0.1)

    task = asyncio.create_task(spin())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        print("\r" + " " * (len(message) + 4) + "\r", end="", file=sys.stderr, flush=True)


def needs_confirm(args: argparse.Namespace, description: str) -> bool:
    """Decide whether a mutation may proceed. Returns True if it was refused.

    --yes acts immediately. Otherwise, at a terminal, ask the person sitting
    there. With no terminal there is nobody to ask, so the command prints what
    it would do and exits EXIT_NEEDS_CONFIRM — silence is never consent, which
    is what makes the bare command safe to run as a dry run.
    """
    if args.yes:
        return False

    print(f"Would {description}", file=sys.stderr)

    if not sys.stdin.isatty():
        print("Re-run with --yes to apply.", file=sys.stderr)
        return True

    print("Proceed? [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = ""
    if answer in ("y", "yes"):
        return False

    print("Cancelled.", file=sys.stderr)
    return True


# --------------------------------------------------------------------------
# Read commands
# --------------------------------------------------------------------------


async def cmd_system_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.system(router)
        emit(payload, render.system(payload), args.json)
    return EXIT_OK


async def cmd_system_health(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.health(router, args.cpu_sample)
        emit(payload, render.health(payload), args.json)
    return EXIT_OK


async def cmd_clients(args: argparse.Namespace) -> int:
    async with connect() as router:
        rows = await ops.clients(router, args.online)
        emit(rows, render.client_lines(rows), args.json)
    return EXIT_OK


async def cmd_wan(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.wan(router)
        emit(payload, render.wan(payload), args.json)
    return EXIT_OK


async def cmd_dns_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.dns(router)
        emit(payload, render.dns(payload), args.json)
    return EXIT_OK


async def cmd_led_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.led(router)
        emit(payload, render.led(payload), args.json)
    return EXIT_OK


async def cmd_upnp_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.upnp(router)
        emit(payload, render.upnp(payload), args.json)
    return EXIT_OK


async def cmd_show(args: argparse.Namespace) -> int:
    """Every read in one connection.

    Each `<noun> show` is a separate process and therefore a separate login to
    the router, so answering "how is the router doing" one noun at a time costs
    a handshake per noun. This walks the same helpers over a single connection.

    The firmware check is left out unless asked for: it makes the router query
    ASUS and adds several seconds, and it answers a question ("should I
    upgrade") that is not part of a status sweep.
    """
    async with connect() as router:
        payloads = await ops.overview(router, args.cpu_sample, False, args.wait)

        sections: list[tuple[str, str, Any, list[str]]] = [
            ("SYSTEM", "system", payloads["system"], render.system(payloads["system"])),
            ("HEALTH", "health", payloads["health"], render.health(payloads["health"])),
            ("INTERNET", "wan", payloads["wan"], render.wan(payloads["wan"])),
            (
                "CLIENTS", "clients", payloads["clients"],
                render.overview_clients(payloads["clients"]),
            ),
            (
                "FIREWALL", "firewall", payloads["firewall"],
                render.firewall(payloads["firewall"]),
            ),
            (
                "PARENTAL CONTROL", "parental", payloads["parental"],
                render.parental(payloads["parental"]),
            ),
            (
                "PORT FORWARDING", "port_forwarding", payloads["port_forwarding"],
                render.port_forwarding(payloads["port_forwarding"]),
            ),
            ("GUEST WIFI", "guest", payloads["guest"], render.guest(payloads["guest"])),
            ("WIRELESS", "wifi", payloads["wifi"], render.wifi(payloads["wifi"])),
        ]

        if args.firmware:
            async with progress("Asking the router to check with ASUS"):
                fw = await ops.firmware(router, args.wait)
            sections.append(("FIRMWARE", "firmware", fw, render.overview_firmware(fw)))

        out = render.overview(sections, firmware_checked=args.firmware)
        emit({key: payload for _, key, payload, _ in sections}, out, args.json)
    return EXIT_OK


# --------------------------------------------------------------------------
# Port forwarding
# --------------------------------------------------------------------------


async def cmd_pf_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.port_forwarding(router)
        emit(payload, render.port_forwarding(payload), args.json)
    return EXIT_OK


async def cmd_pf_add(args: argparse.Namespace) -> int:
    rule = PortForwardingRule(
        name=args.name,
        ip_address=args.to_ip,
        port=str(args.to_port or args.port),
        protocol=args.proto,
        ip_external=args.from_ip or "",
        port_external=str(args.port),
    )
    description = (
        f"add rule {rule.name!r}: :{rule.port_external} -> "
        f"{rule.ip_address}:{rule.port} {rule.protocol}"
    )
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        try:
            result = await ops.port_forward_add(router, rule, force=args.force)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return EXIT_ERROR

        print(f"{'Applied' if result['applied'] else 'FAILED'}: {description}")
        if result["global_state"] == AsusPortForwarding.OFF:
            print("Note: port forwarding is globally OFF. Run: asuswrt pf enable --yes")
        return EXIT_OK if result["applied"] else EXIT_ERROR


async def cmd_pf_remove(args: argparse.Namespace) -> int:
    async with connect() as router:
        current = await port_forwarding_rules(router)
        doomed = [r for r in current if ops._pf_matches(r, args.name, args.port, args.proto)]
        if not doomed:
            print("No matching rule.", file=sys.stderr)
            return EXIT_ERROR

        description = "remove " + ", ".join(
            f"{r.name!r} (:{r.port_external} -> {r.ip_address}:{r.port})" for r in doomed
        )
        if needs_confirm(args, description):
            return EXIT_NEEDS_CONFIRM

        result = await ops.port_forward_remove(
            router, name=args.name, port=args.port, proto=args.proto
        )
        print(f"{'Applied' if result['applied'] else 'FAILED'}: {description}")
        return EXIT_OK if result["applied"] else EXIT_ERROR


async def cmd_pf_toggle(args: argparse.Namespace) -> int:
    target = AsusPortForwarding.ON if args.action == "enable" else AsusPortForwarding.OFF
    if needs_confirm(args, f"turn port forwarding {target.name} globally"):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_port_forwarding(router, args.action == "enable")
        print(f"{'Applied' if result['applied'] else 'FAILED'}: port forwarding {target.name}")
        return EXIT_OK if result["applied"] else EXIT_ERROR


# --------------------------------------------------------------------------
# Firewall / filtering
# --------------------------------------------------------------------------


async def cmd_firewall_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.firewall(router)
        emit(payload, render.firewall(payload), args.json)
    return EXIT_OK


async def cmd_parental_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.parental(router)
        emit(payload, render.parental(payload), args.json)
    return EXIT_OK


async def cmd_parental(args: argparse.Namespace) -> int:
    target = AsusParentalControl.ON if args.action == "enable" else AsusParentalControl.OFF
    if needs_confirm(args, f"turn parental control {target.name}"):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_parental_control(router, args.action == "enable")
        print(f"{'Applied' if result['applied'] else 'FAILED'}: parental control {target.name}")
        return EXIT_OK if result["applied"] else EXIT_ERROR


# --------------------------------------------------------------------------
# Wireless / guest network
# --------------------------------------------------------------------------


async def cmd_guest_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.guest(router)
        emit(payload, render.guest(payload), args.json)
    return EXIT_OK


async def cmd_guest_toggle(args: argparse.Namespace) -> int:
    target = AsusWLAN.ON if args.action == "enable" else AsusWLAN.OFF

    if needs_confirm(args, f"turn guest network {args.band}_{args.id} {target.name}"):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_guest_network(router, args.band, args.id, args.action == "enable")
        print(f"{'Applied' if result['applied'] else 'FAILED'}: guest {args.band}_{args.id} {target.name}")
        return EXIT_OK if result["applied"] else EXIT_ERROR


# --------------------------------------------------------------------------
# Wireless security
# --------------------------------------------------------------------------


def _report_apply(result: dict[str, Any], description: str, as_json: bool) -> int:
    """Print the before/after of an nvram write and pick an exit code.

    async_run_service reports whether the router accepted the request, not
    whether the value stuck, so the read-back is what decides success here —
    and only the read-back. `result["ok"]` is the router's `modify` flag,
    which is false whenever there was nothing to modify, so re-applying a
    setting that is already correct would otherwise exit non-zero.
    """
    emit(result, render.apply_report(result, description), as_json)
    return EXIT_OK if not result["unchanged"] else EXIT_ERROR


async def cmd_wifi_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        payload = await ops.wifi(router)
        emit(payload, render.wifi(payload), args.json)
    return EXIT_OK


async def cmd_wifi_wps(args: argparse.Namespace) -> int:
    description = f"turn WPS {'ON' if args.action == 'enable' else 'OFF'} on all bands"
    if args.action == "enable":
        description += " (the WPS PIN exchange is brute-forceable)"
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_wps(router, args.action == "enable")
        return _report_apply(result, description, args.json)


async def cmd_wifi_security(args: argparse.Namespace) -> int:
    auth, default_mfp = WPA_MODES[args.mode]
    mfp = MFP_VALUES[args.mfp] if args.mfp else default_mfp

    description = (
        f"set {args.band} to {args.mode} (auth_mode_x={auth}, crypto=aes, "
        f"mfp={MFP_NAMES[mfp]}) — every wireless client reconnects"
    )
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_wifi_security(router, args.band, args.mode, args.mfp)
        return _report_apply(result, description, args.json)


async def cmd_wifi_country(args: argparse.Namespace) -> int:
    code = args.code.upper()
    description = f"set the {args.band} country code to {code}"
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_wifi_country(router, args.band, code)
        exit_code = _report_apply(result, description, args.json)
        if result["unchanged"]:
            print(
                "Country code is usually locked to the hardware SKU on stock "
                "firmware. Compare against: asuswrt nvram reg_spec location_code",
                file=sys.stderr,
            )
        return exit_code


# --------------------------------------------------------------------------
# DNS and LEDs
# --------------------------------------------------------------------------

# Printed after a successful `dns set`. Two things the read-back cannot say.
#
# First, that the write stuck in nvram is not that the resolver changed: the
# effective pair lives in wan<N>_dns and takes a few seconds to catch up, so
# `asuswrt dns` is what confirms it and `In use` is the line to read.
#
# Second, the way back. Bad resolvers break name resolution for the whole
# house, and the reader needs to know the recovery command still works — this
# tool reaches the router by address, so it never depended on DNS.
DNS_AFTER_APPLY = (
    "Confirm it took effect with: asuswrt dns   (the 'In use' line, a few seconds from now)\n"
    "Restore the ISP's DNS with:  asuswrt dns auto --yes"
)


async def cmd_dns_set(args: argparse.Namespace) -> int:
    # Validate before the dry-run message, not after: a mistyped address
    # should be refused without printing a confirmation for a change that
    # could never be applied.
    try:
        first = ops._dns_server(args.server1, "The first DNS server")
        second = ops._dns_server(args.server2, "The second DNS server") if args.server2 else ""
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR

    servers = " ".join(s for s in (first, second) if s)
    description = (
        f"point the router's WAN DNS at {servers} "
        "— every device on the network resolves through it"
    )
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        try:
            result = await ops.set_dns(router, args.server1, args.server2)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return EXIT_ERROR

        exit_code = _report_apply(result, description, args.json)
        if exit_code == EXIT_OK and not args.json:
            print(f"\n{DNS_AFTER_APPLY}")
        return exit_code


async def cmd_dns_auto(args: argparse.Namespace) -> int:
    description = "hand the router's WAN DNS back to the ISP's servers"
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        try:
            result = await ops.set_dns(router, automatic=True)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return EXIT_ERROR
        return _report_apply(result, description, args.json)


async def cmd_upnp(args: argparse.Namespace) -> int:
    enabled = args.action == "enable"
    description = f"turn UPnP {'ON' if enabled else 'OFF'}"
    if enabled:
        description += (
            " — any program on the network could then open an inbound port "
            "to itself without asking"
        )
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_upnp(router, enabled)
        return _report_apply(result, description, args.json)


async def cmd_led(args: argparse.Namespace) -> int:
    enabled = args.action == "on"
    description = f"turn the router's status LEDs {'ON' if enabled else 'OFF'}"
    if needs_confirm(args, description):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.set_led(router, enabled)
        return _report_apply(result, description, args.json)


# --------------------------------------------------------------------------
# Firmware
# --------------------------------------------------------------------------


async def cmd_firmware_show(args: argparse.Namespace) -> int:
    async with connect() as router:
        async with progress("Asking the router to check with ASUS"):
            payload = await ops.firmware(router, args.wait)
        emit(payload, render.firmware(payload, notes=args.notes), args.json)
    return EXIT_OK


async def cmd_firmware_upgrade(args: argparse.Namespace) -> int:
    """Download and flash a new firmware.

    Unlike the other mutations this connects before it can honour the dry run,
    because the version on offer has to be read before it can be named. Reading
    firmware state has no side effects. It only reads it once — the same
    payload backs both the pre-flight checks below and, once confirmed, the
    flash itself — so this does not call `ops.firmware_upgrade` (which does
    its own independent read, meant for a single self-contained call such as
    a future MCP tool invocation).
    """
    async with connect() as router:
        async with progress("Asking the router to check with ASUS"):
            payload = await ops.firmware(router, args.wait)

        try:
            current, latest = ops._resolve_upgrade_target(payload, args.beta)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return EXIT_ERROR

        # With a terminal the version is shown and confirmed interactively.
        # Without one there is nobody to read it, so --yes has to name the
        # version explicitly rather than flash whatever turned up.
        if args.yes and not sys.stdin.isatty():
            if not args.to:
                print(
                    f"Refusing to flash unattended without --to.\n"
                    f"  offered  {latest}\n"
                    "Pass --to with that version to confirm it is the intended one.",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            if args.to != latest:
                print(
                    "--to does not match what the router is offering.\n"
                    f"  requested  {args.to}\n"
                    f"  offered    {latest}",
                    file=sys.stderr,
                )
                return EXIT_ERROR

        description = (
            f"FLASH firmware {latest} over {current}.\n"
            "  The router downloads from ASUS, writes flash, then reboots.\n"
            "  Every connection in the house drops for several minutes.\n"
            "  Losing power while flash is being written can brick the router."
        )
        if needs_confirm(args, description):
            return EXIT_NEEDS_CONFIRM

        try:
            await ops._apply_firmware_upgrade(router)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return EXIT_ERROR

        # The API acknowledges the request; it reports nothing about the
        # download or the flash. Do not call this a completed upgrade.
        print(
            f"Upgrade to {latest} requested.\n"
            "The router reports no progress over this API. Expect 5-10 minutes "
            "of downtime, then confirm the new version with: asuswrt system show"
        )
        return EXIT_OK


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


async def cmd_setup(args: argparse.Namespace) -> int:
    """Prompt for the router login and write it out.

    Async only because main() runs every command through asyncio.run(); the
    prompting itself is ordinary blocking input, which is what a terminal
    wants here.
    """
    return config_setup.run_setup(force=args.force, host=args.host)


# --------------------------------------------------------------------------
# Raw access / system
# --------------------------------------------------------------------------


async def cmd_nvram(args: argparse.Namespace) -> int:
    """Read arbitrary nvram variables. Read-only by design.

    `nvram get a b` and `nvram a b` are the same command: `get` is the regular
    verb, but no nvram variable is called that, so a leading one is the verb.
    """
    names = args.names[1:] if args.names[:1] == ["get"] else args.names
    if not names:
        print("Give at least one variable name.", file=sys.stderr)
        return EXIT_ERROR

    async with connect() as router:
        raw = await ops.nvram(router, names)
        emit(raw, [f"{k:<24} {v!r}" for k, v in raw.items()], args.json)
    return EXIT_OK


async def cmd_reboot(args: argparse.Namespace) -> int:
    if needs_confirm(args, "REBOOT the router (drops every connection for ~60 s)"):
        return EXIT_NEEDS_CONFIRM

    async with connect() as router:
        result = await ops.reboot(router)
        print(f"{'Reboot requested' if result['requested'] else 'FAILED'}")
        return EXIT_OK if result["requested"] else EXIT_ERROR


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the command tree.

    The shape is noun -> verb, and it is regular on purpose: every noun has a
    `show`, `show` is the only read verb, and a bare noun means `show`. An
    agent that knows the nouns can therefore reach any reading without being
    told which command happens to hold it.

    Names that existed before the tree was regularised still work but are
    hidden from --help: `info`, `status`, `pf`, `list`, `firmware info`,
    `wifi security`, `wifi country`. Nothing is ever removed, because a name
    an agent has already learned is a contract.
    """
    parser = argparse.ArgumentParser(
        prog="asuswrt", description="Control an ASUS router over its HTTP API."
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def mutation(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument(
            "--yes", "-y", action="store_true", dest="yes",
            help="apply without asking; without it you are prompted, or the "
                 "command exits 3 when there is no terminal to ask at",
        )
        # Former name, kept working but no longer advertised.
        sp.add_argument("--confirm", action="store_true", dest="yes",
                        help=argparse.SUPPRESS)
        return sp

    def flags(*adders: Any) -> argparse.ArgumentParser:
        """A reusable set of options, so a noun and its `show` both accept them."""
        parent = argparse.ArgumentParser(add_help=False)
        for add in adders:
            add(parent)
        return parent

    def cpu_sample(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--cpu-sample", type=float, default=CPU_SAMPLE_SECONDS,
                        help="seconds between the two CPU samples")

    def online(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--online", action="store_true",
                        help="only currently online devices")

    def fw_read(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--notes", action="store_true", help="show the release note")
        sp.add_argument("--wait", type=float, default=FIRMWARE_CHECK_SECONDS,
                        help="seconds to wait for the router's reply from ASUS")

    def noun(
        name: str,
        help_text: str,
        read: Any,
        *,
        aliases: list[str] | None = None,
        read_aliases: list[str] | None = None,
        opts: argparse.ArgumentParser | None = None,
    ) -> Any:
        """Add a noun whose bare form reads it, and return its verb table."""
        parents = [opts] if opts else []
        np = sub.add_parser(name, help=help_text, aliases=aliases or [], parents=parents)
        np.set_defaults(func=read)
        verbs = np.add_subparsers(dest=f"{name}_command", required=False)
        sp = verbs.add_parser(
            "show", help=help_text, aliases=read_aliases or [], parents=parents
        )
        sp.set_defaults(func=read)
        return verbs

    # -- credentials -------------------------------------------------------
    p = sub.add_parser(
        "setup", help="save the router login to ~/.config/asuswrt/.env"
    )
    p.add_argument("--force", action="store_true",
                   help="replace an existing credential file")
    p.add_argument("--host", metavar="ADDRESS",
                   help="the router's address; only needed when the default "
                        "gateway is not it")
    p.set_defaults(func=cmd_setup)

    # -- everything at once ------------------------------------------------
    p = sub.add_parser(
        "show", help="every reading in one connection", parents=[flags(cpu_sample)]
    )
    p.add_argument("--firmware", action="store_true",
                   help="also check ASUS for a firmware update (adds ~7 s)")
    p.add_argument("--wait", type=float, default=FIRMWARE_CHECK_SECONDS,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_show)

    # -- system ------------------------------------------------------------
    system = noun("system", "model, firmware, MAC, AiMesh", cmd_system_show)
    p = system.add_parser(
        "health", help="uptime, CPU, RAM, WAN", parents=[flags(cpu_sample)]
    )
    p.set_defaults(func=cmd_system_health)

    # -- network -----------------------------------------------------------
    noun("wan", "internet connection detail", cmd_wan)
    noun("clients", "connected and known devices", cmd_clients, opts=flags(online))

    # -- dns ---------------------------------------------------------------
    dns = noun("dns", "WAN resolvers and what LAN clients are told", cmd_dns_show)

    p = mutation(dns.add_parser("set", help="set the WAN DNS servers"))
    p.add_argument("--server1", required=True, help="first resolver, e.g. 8.8.8.8")
    p.add_argument("--server2", help="second resolver, e.g. 8.8.4.4")
    p.set_defaults(func=cmd_dns_set)

    p = mutation(dns.add_parser("auto", help="use whatever DNS the ISP hands out"))
    p.set_defaults(func=cmd_dns_auto)

    # -- upnp --------------------------------------------------------------
    upnp = noun("upnp", "automatic inbound port opening", cmd_upnp_show)
    for action in ("enable", "disable"):
        p = mutation(upnp.add_parser(action, help=f"{action} UPnP"))
        p.set_defaults(func=cmd_upnp, action=action)

    # -- leds --------------------------------------------------------------
    led = noun("led", "router status lights", cmd_led_show)
    for action in ("on", "off"):
        p = mutation(led.add_parser(action, help=f"turn the status LEDs {action}"))
        p.set_defaults(func=cmd_led, action=action)

    # -- firewall and parental control -------------------------------------
    noun("firewall", "firewall, filters and parental control state", cmd_firewall_show)

    parental = noun("parental", "parental control", cmd_parental_show)
    for action in ("enable", "disable"):
        p = mutation(parental.add_parser(action, help=f"{action} parental control"))
        p.set_defaults(func=cmd_parental, action=action)

    # -- port forwarding ---------------------------------------------------
    pf = noun(
        "portforward", "port forwarding", cmd_pf_show,
        aliases=["pf"], read_aliases=["list"],
    )

    p = mutation(pf.add_parser("add", help="add a rule"))
    p.add_argument("--name", required=True, help="label for the rule")
    p.add_argument("--port", required=True, type=int, help="external port")
    p.add_argument("--to-ip", required=True, help="internal IP to forward to")
    p.add_argument("--to-port", type=int, help="internal port (default: same as --port)")
    p.add_argument("--proto", default="TCP", choices=["TCP", "UDP", "BOTH", "OTHER"])
    p.add_argument("--from-ip", default="", help="restrict to this source IP")
    p.add_argument("--force", action="store_true", help="allow a duplicate external port")
    p.set_defaults(func=cmd_pf_add)

    p = mutation(pf.add_parser("remove", help="remove rules by name and/or external port"))
    p.add_argument("--name")
    p.add_argument("--port", type=int)
    p.add_argument("--proto", choices=["TCP", "UDP", "BOTH", "OTHER"])
    p.set_defaults(func=cmd_pf_remove)

    for action in ("enable", "disable"):
        p = mutation(pf.add_parser(action, help=f"{action} port forwarding globally"))
        p.set_defaults(func=cmd_pf_toggle, action=action)

    # -- guest wifi --------------------------------------------------------
    guest = noun(
        "guest", "guest wireless networks", cmd_guest_show, read_aliases=["list"]
    )
    for action in ("enable", "disable"):
        p = mutation(guest.add_parser(action, help=f"{action} a guest network"))
        p.add_argument("--band", required=True, choices=["2ghz", "5ghz"])
        p.add_argument("--id", required=True, type=int, choices=[1, 2, 3])
        p.set_defaults(func=cmd_guest_toggle, action=action)

    # -- wireless security -------------------------------------------------
    wifi = noun(
        "wifi", "radio, WPA mode, MFP, country code, WPS", cmd_wifi_show
    )

    wps = wifi.add_parser("wps", help="Wi-Fi Protected Setup").add_subparsers(
        dest="wps_command", required=True
    )
    for action in ("enable", "disable"):
        p = mutation(wps.add_parser(action, help=f"{action} WPS on all bands"))
        p.set_defaults(func=cmd_wifi_wps, action=action)

    p = mutation(wifi.add_parser(
        "set-security", help="WPA mode and frame protection", aliases=["security"]
    ))
    p.add_argument("--band", default="both", choices=["2ghz", "5ghz", "both"])
    p.add_argument("--mode", required=True, choices=sorted(WPA_MODES))
    p.add_argument(
        "--mfp",
        choices=sorted(MFP_VALUES),
        help="802.11w level; defaults to what the mode needs",
    )
    p.set_defaults(func=cmd_wifi_security)

    p = mutation(wifi.add_parser(
        "set-country", help="regulatory country code", aliases=["country"]
    ))
    p.add_argument("--band", default="both", choices=["2ghz", "5ghz", "both"])
    p.add_argument("--code", required=True, help="two-letter code, e.g. AU")
    p.set_defaults(func=cmd_wifi_country)

    # -- firmware ----------------------------------------------------------
    firmware = noun(
        "firmware", "installed version and what ASUS is offering",
        cmd_firmware_show, read_aliases=["info"], opts=flags(fw_read),
    )

    p = mutation(firmware.add_parser("upgrade", help="download and flash firmware"))
    p.add_argument("--to", help="version to install; required with --yes when "
                               "there is no terminal to confirm at")
    p.add_argument("--beta", action="store_true", help="target the beta channel")
    p.add_argument("--wait", type=float, default=FIRMWARE_CHECK_SECONDS)
    p.set_defaults(func=cmd_firmware_upgrade)

    # -- raw / system ------------------------------------------------------
    p = sub.add_parser("nvram", help="read raw nvram variables (read-only)")
    p.add_argument("names", nargs="+", metavar="get NAME [NAME ...]",
                   help="variable names, optionally after the verb `get`")
    p.set_defaults(func=cmd_nvram)

    p = mutation(sub.add_parser("reboot", help="reboot the router"))
    p.set_defaults(func=cmd_reboot)

    # -- names from before the tree was regularised ------------------------
    p = sub.add_parser("info")
    p.set_defaults(func=cmd_system_show)

    p = sub.add_parser("status", parents=[flags(cpu_sample)])
    p.set_defaults(func=cmd_system_health)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not hasattr(args, "yes"):
        args.yes = True  # read-only commands never need confirmation

    try:
        sys.exit(asyncio.run(args.func(args)))
    except ConfigError as err:
        print(str(err), file=sys.stderr)
        sys.exit(EXIT_CONFIG)
    except AsusRouterError as err:
        print(explain_router_error(err), file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except KeyboardInterrupt:
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
