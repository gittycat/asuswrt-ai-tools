"""The MCP server: registration per gate, every tool's payload, and the
write contract (preview vs confirm, and every anticipated failure).

`FakeRouter` is patched in for `asuswrt.mcp_server.connect`, the same trick
`helpers.invoke` uses for the CLI. Calling a tool goes through
`MCPServer.call_tool()` directly rather than a real JSON-RPC round trip
(test_mcp_stdio.py covers that layer); a `ToolError` raised there is caught
here the same way the SDK's own request handler catches it — by building the
`CallToolResult(is_error=True)` the client would actually see — so assertions
below check that flag, not merely that Python raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from asusrouter import AsusData, AsusRouterError
from asusrouter.modules.parental_control import AsusParentalControl
from asusrouter.modules.port_forwarding import AsusPortForwarding, PortForwardingRule
from asusrouter.modules.system import AsusSystem
from asusrouter.modules.wlan import AsusWLAN
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from asuswrt import mcp_server
from helpers import FakeRouter, default_data

OFFERED = "3.0.0.4.388.34098_g9b0c9ae"

READ_NAMES = {fn.__name__ for fn, _ in mcp_server.READS}
WRITE_NAMES = {fn.__name__ for fn, _ in mcp_server.WRITES}
DANGEROUS_NAMES = {fn.__name__ for fn, _ in mcp_server.DANGEROUS}


# -- helpers -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """These constants back real `asyncio.sleep` calls (the CPU-usage delta,
    the firmware-check poll). Zero them so the suite doesn't spend whole
    seconds waiting on a fake router, the same way the CLI tests pass
    `--wait 0` / `--cpu-sample 0`."""
    monkeypatch.setattr(mcp_server, "CPU_SAMPLE_SECONDS", 0)
    monkeypatch.setattr(mcp_server, "FIRMWARE_CHECK_SECONDS", 0)


@pytest.fixture
def patched(monkeypatch):
    """Patch `mcp_server.connect` to hand back `router` instead of a real one."""

    def _patch(router: FakeRouter) -> FakeRouter:
        @contextlib.asynccontextmanager
        async def fake_connect(config=None):
            yield router

        monkeypatch.setattr(mcp_server, "connect", fake_connect)
        return router

    return _patch


@pytest.fixture
def server():
    """Every tool registered, so behaviour tests don't repeat gate wiring."""
    return mcp_server.build_server(allow_writes=True, allow_dangerous=True)


def call(server, name: str, arguments: dict | None = None) -> CallToolResult:
    """Call a tool the way the SDK's request handler does.

    `MCPServer.call_tool()` propagates `ToolError` as a Python exception —
    convenient for programmatic use, but it is the request handler
    (`_handle_call_tool`) that turns that into the `is_error=True` result the
    client actually receives. This reproduces exactly that conversion so
    tests assert on the same flag a real client would see.
    """

    async def _call():
        try:
            return await server.call_tool(name, arguments or {})
        except ToolError as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)

    return asyncio.run(_call())


def payload(result: CallToolResult):
    """Decode a successful result's content.

    A dict-returning tool comes back as one content block; the SDK splits a
    list-returning tool (list_clients) into one block per element, so that
    case is reassembled here rather than special-cased at every call site.
    """
    assert not result.is_error, result.content
    if len(result.content) == 1:
        return json.loads(result.content[0].text)
    return [json.loads(block.text) for block in result.content]


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return result.content[0].text


def pf_data(rules: list[PortForwardingRule], state=AsusPortForwarding.ON) -> dict:
    data = default_data()
    data[AsusData.PORT_FORWARDING] = {"state": state, "rules": rules}
    return data


PF_RULES = [
    PortForwardingRule(
        name="Plex", ip_address="192.168.50.20", port="32400",
        protocol="TCP", ip_external="", port_external="32400",
    ),
    PortForwardingRule(
        name="Web", ip_address="192.168.50.30", port="80",
        protocol="TCP", ip_external="", port_external="8080",
    ),
]


@pytest.fixture
def pf_router() -> FakeRouter:
    return FakeRouter(data=pf_data(list(PF_RULES)))


# -- registration per gate ----------------------------------------------------


def _tool_names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


def test_default_gate_registers_only_the_15_read_tools():
    names = _tool_names(mcp_server.build_server())
    assert names == READ_NAMES
    assert len(names) == 15


def test_writes_gate_adds_the_11_write_tools():
    names = _tool_names(mcp_server.build_server(allow_writes=True))
    assert names == READ_NAMES | WRITE_NAMES
    assert len(names) == 26


def test_both_gates_register_all_28_tools():
    names = _tool_names(mcp_server.build_server(allow_writes=True, allow_dangerous=True))
    assert names == READ_NAMES | WRITE_NAMES | DANGEROUS_NAMES
    assert len(names) == 28


def test_dangerous_gate_alone_registers_nothing_extra():
    """Gates hide tools; the dangerous gate needs the writes gate too."""
    names = _tool_names(mcp_server.build_server(allow_writes=False, allow_dangerous=True))
    assert names == READ_NAMES


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_gate_opens_on_an_explicit_yes(monkeypatch, value):
    monkeypatch.setenv("ASUSWRT_MCP_ALLOW_WRITES", value)
    assert mcp_server.gate_open("ASUSWRT_MCP_ALLOW_WRITES") is True


@pytest.mark.parametrize(
    "value",
    ["", "0", "false", "no", "off", "${user_config.allow_writes}", "maybe"],
)
def test_gate_stays_shut_on_anything_else(monkeypatch, value):
    """An unsubstituted bundle template must read as off, not as a non-empty string."""
    monkeypatch.setenv("ASUSWRT_MCP_ALLOW_WRITES", value)
    assert mcp_server.gate_open("ASUSWRT_MCP_ALLOW_WRITES") is False


def test_gate_stays_shut_when_unset(monkeypatch):
    monkeypatch.delenv("ASUSWRT_MCP_ALLOW_WRITES", raising=False)
    assert mcp_server.gate_open("ASUSWRT_MCP_ALLOW_WRITES") is False


def test_every_read_tool_is_read_only_annotated():
    server = mcp_server.build_server()
    for tool in asyncio.run(server.list_tools()):
        assert tool.annotations.read_only_hint is True


@pytest.mark.parametrize(
    "name",
    ["get_overview", "get_firewall_and_filters", "get_nvram"],
)
def test_settled_policy_survives_in_the_tool_descriptions(name):
    """The skill is not always installed - the Claude Desktop extension ships
    the server with no skill mechanism at all, and `claude mcp add` installs
    none either. An agent that reads fw_dos_x=0 or TM_EULA=0 with no other
    context reports them as gaps to close, so the schema has to carry the
    policy itself. See the README, "Two settings this project will not turn
    on"."""
    tools = {t.name: t for t in asyncio.run(mcp_server.build_server().list_tools())}
    description = " ".join(tools[name].description.split()).lower()
    assert "fw_dos_x=0" in description
    assert "trend micro" in description
    assert "never propose enabling either" in description


# -- reads: every one returns JSON and leaves the router untouched -----------


def test_get_overview(server, patched, router):
    patched(router)
    data = payload(call(server, "get_overview"))
    assert set(data) == {
        "system", "health", "wan", "clients", "firewall", "parental",
        "port_forwarding", "guest", "wifi",
    }
    assert not router.touched


def test_get_system(server, patched, router):
    patched(router)
    data = payload(call(server, "get_system"))
    assert data["model"] == "RT-AX59U"
    assert not router.touched


def test_get_health(server, patched, router):
    patched(router)
    data = payload(call(server, "get_health"))
    assert set(data) == {"uptime_hours", "cpu_usage", "cpu_cores", "ram", "wan_link", "wan_ip"}
    assert not router.touched


def test_get_wan(server, patched, router):
    patched(router)
    data = payload(call(server, "get_wan"))
    assert "internet" in data
    assert not router.touched


def test_list_clients(server, patched, router):
    patched(router)
    data = payload(call(server, "list_clients"))
    assert len(data) == 3
    assert not router.touched


def test_list_clients_online_only(server, patched, router):
    patched(router)
    data = payload(call(server, "list_clients", {"online_only": True}))
    assert data and all(r["online"] for r in data)
    assert not router.touched


def test_get_firewall_and_filters(server, patched, router):
    patched(router)
    data = payload(call(server, "get_firewall_and_filters"))
    assert set(data) == {"nvram", "parental_control"}
    assert not router.touched


def test_get_parental_control(server, patched, router):
    patched(router)
    data = payload(call(server, "get_parental_control"))
    assert {"state", "block_all", "rules"} <= set(data)
    assert not router.touched


def test_list_port_forwards(server, patched, pf_router):
    patched(pf_router)
    data = payload(call(server, "list_port_forwards"))
    assert [r["name"] for r in data["rules"]] == ["Plex", "Web"]
    assert not pf_router.touched


def test_list_guest_networks(server, patched, router):
    patched(router)
    data = payload(call(server, "list_guest_networks"))
    assert data
    assert not router.touched


def test_get_wireless(server, patched, router):
    patched(router)
    data = payload(call(server, "get_wireless"))
    assert "wl0_mfp" in data
    assert not router.touched


def test_check_firmware_update(server, patched, router):
    """This one read is not silent — it asks the router to check ASUS,
    which is itself a state change (FIRMWARE_CHECK) — but it writes nothing."""
    patched(router)
    data = payload(call(server, "check_firmware_update"))
    assert data["status"] == "update"
    assert data["latest"] == OFFERED
    assert router.states == [(AsusSystem.FIRMWARE_CHECK, {})]
    assert router.services == []
    assert router.applied_pf_rules is None
    assert router.nvram == router._initial_nvram


def test_get_nvram(server, patched, router):
    patched(router)
    data = payload(call(server, "get_nvram", {"names": ["wl0_mfp", "wl1_mfp"]}))
    assert data == {"wl0_mfp": "0", "wl1_mfp": "0"}
    assert not router.touched


def test_get_nvram_rejects_an_empty_list(server, patched, router):
    patched(router)
    result = call(server, "get_nvram", {"names": []})
    assert result.is_error
    assert not router.touched


# -- writes: preview never touches, confirm matches the CLI's twin -----------


def test_add_port_forward_preview_does_not_touch(server, patched, pf_router):
    patched(pf_router)
    data = payload(call(server, "add_port_forward", {
        "name": "Game", "port": 27015, "to_ip": "192.168.50.40",
    }))
    assert data["status"] == "preview"
    assert data["applied"] is False
    assert not pf_router.touched


def test_add_port_forward_confirm_matches_its_cli_twin(server, patched, pf_router):
    patched(pf_router)
    data = payload(call(server, "add_port_forward", {
        "name": "Game", "port": 27015, "to_ip": "192.168.50.40", "confirm": True,
    }))
    assert data["status"] == "applied"
    assert [r.name for r in pf_router.applied_pf_rules] == ["Plex", "Web", "Game"]
    added = pf_router.applied_pf_rules[-1]
    assert (added.port_external, added.port, added.protocol) == ("27015", "27015", "TCP")


def test_add_port_forward_distinct_internal_port(server, patched, pf_router):
    patched(pf_router)
    call(server, "add_port_forward", {
        "name": "Alt", "port": 8443, "to_ip": "192.168.50.40", "to_port": 443, "confirm": True,
    })
    added = pf_router.applied_pf_rules[-1]
    assert (added.port_external, added.port) == ("8443", "443")


def test_add_port_forward_clash_without_force_is_a_tool_error(server, patched, pf_router):
    patched(pf_router)
    result = call(server, "add_port_forward", {
        "name": "Plex2", "port": 32400, "to_ip": "192.168.50.99",
    })
    assert result.is_error
    assert "already" in error_text(result)
    assert pf_router.applied_pf_rules is None


def test_add_port_forward_force_allows_the_duplicate(server, patched, pf_router):
    patched(pf_router)
    data = payload(call(server, "add_port_forward", {
        "name": "Plex2", "port": 32400, "to_ip": "192.168.50.99", "force": True, "confirm": True,
    }))
    assert data["status"] == "applied"
    assert len(pf_router.applied_pf_rules) == 3


def test_add_port_forward_warns_when_globally_off(server, patched, router):
    patched(router)  # default router has port forwarding OFF
    data = payload(call(server, "add_port_forward", {
        "name": "Plex", "port": 32400, "to_ip": "192.168.50.20",
    }))
    assert data["status"] == "preview"
    assert any("globally OFF" in w for w in data["warnings"])


def test_remove_port_forward_preview_does_not_touch(server, patched, pf_router):
    patched(pf_router)
    data = payload(call(server, "remove_port_forward", {"name": "Plex"}))
    assert data["status"] == "preview"
    assert not pf_router.touched


def test_remove_port_forward_by_name_confirm_matches_its_cli_twin(server, patched, pf_router):
    patched(pf_router)
    data = payload(call(server, "remove_port_forward", {"name": "Plex", "confirm": True}))
    assert data["status"] == "applied"
    assert [r.name for r in pf_router.applied_pf_rules] == ["Web"]


def test_remove_port_forward_by_port_confirm_matches_its_cli_twin(server, patched, pf_router):
    patched(pf_router)
    data = payload(call(server, "remove_port_forward", {"port": 8080, "confirm": True}))
    assert [r.name for r in pf_router.applied_pf_rules] == ["Plex"]


def test_remove_port_forward_preview_lists_every_matching_rule(server, patched):
    """A port can carry both a TCP and a UDP rule; asking by port alone must
    list every rule that would go, not just the first."""
    router = FakeRouter(data=pf_data([
        PortForwardingRule(
            name="Web-TCP", ip_address="1.2.3.4", port="443",
            protocol="TCP", ip_external="", port_external="443",
        ),
        PortForwardingRule(
            name="Web-UDP", ip_address="1.2.3.4", port="443",
            protocol="UDP", ip_external="", port_external="443",
        ),
    ]))
    patched(router)
    data = payload(call(server, "remove_port_forward", {"port": 443}))
    assert len(data["current"]["rules"]) == 2
    assert not router.touched


def test_remove_port_forward_no_match_is_a_tool_error(server, patched, router):
    patched(router)
    result = call(server, "remove_port_forward", {"name": "Nope"})
    assert result.is_error
    assert "No matching rule." in error_text(result)


def test_remove_port_forward_requires_name_or_port(server, patched, router):
    patched(router)
    result = call(server, "remove_port_forward", {})
    assert result.is_error
    assert not router.touched


@pytest.mark.parametrize(
    ("enabled", "expected"), [(True, AsusPortForwarding.ON), (False, AsusPortForwarding.OFF)]
)
def test_set_port_forwarding_enabled_confirm_matches_its_cli_twin(server, patched, router, enabled, expected):
    patched(router)
    data = payload(call(server, "set_port_forwarding_enabled", {"enabled": enabled, "confirm": True}))
    assert data["status"] == "applied"
    assert router.states == [(expected, {})]


def test_set_port_forwarding_enabled_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_port_forwarding_enabled", {"enabled": True}))
    assert data["status"] == "preview"
    assert not router.touched


@pytest.mark.parametrize(
    ("enabled", "expected"), [(True, AsusParentalControl.ON), (False, AsusParentalControl.OFF)]
)
def test_set_parental_control_enabled_confirm_matches_its_cli_twin(server, patched, router, enabled, expected):
    patched(router)
    data = payload(call(server, "set_parental_control_enabled", {"enabled": enabled, "confirm": True}))
    assert data["status"] == "applied"
    assert router.states == [(expected, {})]


def test_set_parental_control_enabled_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_parental_control_enabled", {"enabled": False}))
    assert data["status"] == "preview"
    assert not router.touched


@pytest.mark.parametrize(("band", "guest_id", "api_id"), [("2ghz", 1, "0.1"), ("5ghz", 3, "1.3")])
def test_set_guest_network_enabled_confirm_matches_its_cli_twin(server, patched, router, band, guest_id, api_id):
    patched(router)
    data = payload(call(server, "set_guest_network_enabled", {
        "band": band, "index": guest_id, "enabled": True, "confirm": True,
    }))
    assert data["status"] == "applied"
    state, kwargs = router.states[0]
    assert state is AsusWLAN.ON
    assert kwargs == {"api_type": "gwlan", "api_id": api_id}


def test_set_guest_network_enabled_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_guest_network_enabled", {
        "band": "5ghz", "index": 1, "enabled": True,
    }))
    assert data["status"] == "preview"
    assert not router.touched


def test_set_wps_enabled_disable_confirm_matches_its_cli_twin(server, patched, router):
    patched(router)
    data = payload(call(server, "set_wps_enabled", {"enabled": False, "confirm": True}))
    assert data["status"] == "applied"
    assert router.services == [
        ("restart_wireless", {"wps_enable": "0", "wps_enable_x": "0", "wps_multiband": "0"})
    ]


def test_set_wps_enabled_preview_reads_current_and_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_wps_enabled", {"enabled": False}))
    assert data["status"] == "preview"
    assert data["current"]["wps_enable"] == "1"
    assert not router.touched


def test_set_wps_enabled_that_does_not_stick_is_a_tool_error(server, patched):
    router = FakeRouter(apply_writes=False)
    patched(router)
    result = call(server, "set_wps_enabled", {"enabled": False, "confirm": True})
    assert result.is_error


@pytest.mark.parametrize(
    ("mode", "auth", "mfp"),
    [("wpa2", "psk2", "0"), ("wpa2wpa3", "psk2sae", "1"), ("wpa3", "sae", "2")],
)
def test_set_wifi_security_confirm_matches_its_cli_twin(server, patched, router, mode, auth, mfp):
    patched(router)
    data = payload(call(server, "set_wifi_security", {"mode": mode, "confirm": True}))
    assert data["status"] == "applied"
    service, arguments = router.services[0]
    assert service == "restart_wireless"
    assert arguments == {
        "wl0_auth_mode_x": auth, "wl0_crypto": "aes", "wl0_mfp": mfp,
        "wl1_auth_mode_x": auth, "wl1_crypto": "aes", "wl1_mfp": mfp,
    }


def test_set_wifi_security_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_wifi_security", {"mode": "wpa2wpa3"}))
    assert data["status"] == "preview"
    assert not router.touched


def test_set_wifi_country_confirm_matches_its_cli_twin(server, patched, router):
    patched(router)
    data = payload(call(server, "set_wifi_country", {"band": "5ghz", "code": "AU", "confirm": True}))
    assert data["status"] == "applied"
    assert router.services == [("restart_wireless", {"wl1_country_code": "AU"})]


def test_set_wifi_country_is_upper_cased(server, patched, router):
    patched(router)
    call(server, "set_wifi_country", {"band": "5ghz", "code": "au", "confirm": True})
    assert router.services == [("restart_wireless", {"wl1_country_code": "AU"})]


def test_set_wifi_country_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_wifi_country", {"band": "5ghz", "code": "AU"}))
    assert data["status"] == "preview"
    assert not router.touched


def test_set_wifi_country_that_does_not_stick_is_a_tool_error(server, patched):
    router = FakeRouter(apply_writes=False)
    patched(router)
    result = call(server, "set_wifi_country", {"band": "5ghz", "code": "AU", "confirm": True})
    assert result.is_error
    assert "locked to the hardware SKU" in error_text(result)


def test_reboot_router_confirm_matches_its_cli_twin(server, patched, router):
    patched(router)
    data = payload(call(server, "reboot_router", {"confirm": True}))
    assert data["status"] == "requested"
    assert router.states == [(AsusSystem.REBOOT, {})]


def test_reboot_router_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "reboot_router"))
    assert data["status"] == "preview"
    assert not router.touched


def test_upgrade_firmware_confirm_matches_its_cli_twin(server, patched, router):
    patched(router)
    data = payload(call(server, "upgrade_firmware", {"to": OFFERED, "confirm": True}))
    assert data["status"] == "requested"
    assert (AsusSystem.FIRMWARE_UPGRADE, {}) in router.states


def test_upgrade_firmware_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "upgrade_firmware", {"to": OFFERED}))
    assert data["status"] == "preview"
    assert router.services == []
    assert router.applied_pf_rules is None
    assert AsusSystem.FIRMWARE_UPGRADE not in [s for s, _ in router.states]


def test_upgrade_firmware_mismatched_to_is_a_tool_error(server, patched, router):
    patched(router)
    result = call(server, "upgrade_firmware", {"to": "9.9.9"})
    assert result.is_error
    assert "does not match" in error_text(result)
    assert AsusSystem.FIRMWARE_UPGRADE not in [s for s, _ in router.states]


def test_upgrade_firmware_refuses_when_already_up_to_date(server, patched):
    data = default_data()
    data[AsusData.FIRMWARE] = {**data[AsusData.FIRMWARE], "state": False, "available": None}
    router = FakeRouter(data=data)
    patched(router)
    result = call(server, "upgrade_firmware", {"to": OFFERED})
    assert result.is_error
    assert "up to date" in error_text(result)


# -- refused service calls surface the same way ------------------------------


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("set_port_forwarding_enabled", {"enabled": True, "confirm": True}),
        ("set_parental_control_enabled", {"enabled": True, "confirm": True}),
        ("set_guest_network_enabled", {"band": "2ghz", "index": 1, "enabled": True, "confirm": True}),
        ("reboot_router", {"confirm": True}),
    ],
    ids=["pf-enable", "parental-enable", "guest-enable", "reboot"],
)
def test_a_refused_service_call_is_a_tool_error(server, patched, name, arguments):
    router = FakeRouter(service_ok=False)
    patched(router)
    result = call(server, name, arguments)
    assert result.is_error


def test_a_refused_add_port_forward_is_a_tool_error(server, patched):
    router = FakeRouter(data=pf_data(list(PF_RULES)), service_ok=False)
    patched(router)
    result = call(server, "add_port_forward", {
        "name": "Game", "port": 27015, "to_ip": "192.168.50.40", "confirm": True,
    })
    assert result.is_error


# -- dns and led -------------------------------------------------------------


def test_get_dns_reports_the_unit_and_the_servers(server, patched, router):
    patched(router)
    data = payload(call(server, "get_dns"))
    assert data["unit"] == 0
    assert data["nvram"]["wan0_dns1_x"] == "1.1.1.1"


def test_get_led_reports_led_val(server, patched, router):
    patched(router)
    assert payload(call(server, "get_led"))["led_val"] == "1"


def test_set_wan_dns_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_wan_dns", {"server1": "8.8.8.8", "server2": "8.8.4.4"}))

    assert data["status"] == "preview"
    assert data["current"]["nvram"]["wan0_dns1_x"] == "1.1.1.1"
    assert any("resolves through this" in w for w in data["warnings"])
    assert not router.touched


def test_set_wan_dns_confirm_matches_its_cli_twin(server, patched, router):
    patched(router)
    data = payload(
        call(server, "set_wan_dns", {"server1": "8.8.8.8", "server2": "8.8.4.4", "confirm": True})
    )

    assert data["status"] == "applied"
    assert router.services == [
        (
            "restart_wan_dns 0",
            {"wan0_dnsenable_x": "0", "wan0_dns1_x": "8.8.8.8", "wan0_dns2_x": "8.8.4.4"},
        )
    ]


def test_set_wan_dns_automatic_confirm(server, patched, router):
    patched(router)
    payload(call(server, "set_wan_dns", {"automatic": True, "confirm": True}))
    assert router.services == [("restart_wan_dns 0", {"wan0_dnsenable_x": "1"})]


def test_set_wan_dns_rejects_ipv6(server, patched, router):
    patched(router)
    result = call(server, "set_wan_dns", {"server1": "2001:4860:4860::8888", "confirm": True})
    assert result.is_error
    assert "ipv6_dns1_x" in error_text(result)
    assert not router.touched


def test_set_wan_dns_rejects_no_target(server, patched, router):
    patched(router)
    result = call(server, "set_wan_dns", {"confirm": True})
    assert result.is_error
    assert not router.touched


def test_set_wan_dns_reports_a_write_the_firmware_ignored(server, patched):
    router = FakeRouter(apply_writes=False)
    patched(router)
    result = call(server, "set_wan_dns", {"server1": "8.8.8.8", "confirm": True})
    assert result.is_error
    assert "did not take" in error_text(result)


def test_set_led_enabled_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_led_enabled", {"enabled": False}))
    assert data["status"] == "preview"
    assert data["current"] == {"led_val": "1"}
    assert not router.touched


def test_set_led_enabled_confirm_matches_its_cli_twin(server, patched, router):
    patched(router)
    data = payload(call(server, "set_led_enabled", {"enabled": False, "confirm": True}))
    assert data["status"] == "applied"
    assert router.services == [("start_ctrl_led", {"led_val": "0"})]


def test_set_led_enabled_succeeds_without_a_modify_flag(server, patched):
    """start_ctrl_led reports no `modify`; that must not read as a failure."""
    router = FakeRouter(returns_modify=False)
    patched(router)
    data = payload(call(server, "set_led_enabled", {"enabled": False, "confirm": True}))
    assert data["status"] == "applied"


# -- upnp --------------------------------------------------------------------


def test_get_upnp_summarises_and_shows_every_switch(server, patched, router):
    patched(router)
    data = payload(call(server, "get_upnp"))
    assert data["enabled"] is False
    assert data["nvram"]["upnp_enable"] == "0"


def test_get_upnp_is_on_when_any_switch_is_on(server, patched):
    router = FakeRouter()
    router.nvram["wan0_upnp_enable"] = "1"
    patched(router)
    assert payload(call(server, "get_upnp"))["enabled"] is True


def test_set_upnp_enabled_preview_does_not_touch(server, patched, router):
    patched(router)
    data = payload(call(server, "set_upnp_enabled", {"enabled": True}))

    assert data["status"] == "preview"
    assert any("without asking" in w for w in data["warnings"])
    assert not router.touched


def test_set_upnp_disable_preview_carries_no_warning(server, patched, router):
    """Turning it off is the safe direction; nothing to warn about."""
    patched(router)
    assert payload(call(server, "set_upnp_enabled", {"enabled": False}))["warnings"] == []


def test_set_upnp_enabled_confirm_matches_its_cli_twin(server, patched):
    router = FakeRouter()
    router.nvram.update({"upnp_enable": "1", "wan_upnp_enable": "1", "wan0_upnp_enable": "1"})
    patched(router)

    data = payload(call(server, "set_upnp_enabled", {"enabled": False, "confirm": True}))
    assert data["status"] == "applied"
    assert router.services == [
        ("restart_upnp", {"upnp_enable": "0", "wan_upnp_enable": "0", "wan0_upnp_enable": "0"})
    ]


# -- ConfigError and AsusRouterError -----------------------------------------


def test_config_error_message_has_the_searched_paths_not_the_password(server, monkeypatch, tmp_path):
    env = tmp_path / "empty.env"
    env.write_text("")
    monkeypatch.setenv("ASUSWRT_ENV_FILE", str(env))
    monkeypatch.delenv("ROUTER_PASS", raising=False)
    # No `patched` here: the real connect()/load_config() must run and fail.
    result = call(server, "get_system")
    assert result.is_error
    text = error_text(result)
    assert str(env) in text
    assert "asuswrt setup" in text


def test_router_error_becomes_a_tool_error(server, patched, router, monkeypatch):
    async def boom(*args, **kwargs):
        raise AsusRouterError("boom")

    monkeypatch.setattr(router, "async_get_identity", boom)
    patched(router)
    result = call(server, "get_system")
    assert result.is_error
    assert "Router error:" in error_text(result)
