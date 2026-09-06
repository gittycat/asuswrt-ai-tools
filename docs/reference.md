# Reference

Nothing here is needed to install or use the tool. It is here for edge cases,
and for the agent reading this file. Start with the
[README](../README.md).

## Router credentials

See [Router credentials](credentials.md) for how each installer collects and
stores the password, config precedence, custom addresses, HTTPS, and ports.

## What `uv tool install` puts where

`asuswrt`, `asuswrt-mcp` and `asuswrt-probe` land in `~/.local/bin`. Run
`uv tool update-shell` once if that directory is not on your `PATH`. GUI apps
never inherit your shell `PATH`, which is why a hand-written Claude Desktop
config has to use an absolute path.

Since v0.8.0 this is needed only for the terminal command, for Codex, and for a
hand-registered `claude mcp add`. The Claude Code plugin and the Claude Desktop
extension each carry the server and run it through `uv run`, so neither needs
anything installed globally beyond uv itself.

To install from a local clone instead of GitHub:

```bash
git clone https://github.com/gittycat/asuswrt-ai-tools
uv tool install "./asuswrt-ai-tools[mcp]" --force
```

Update the Claude Code plugin later with
`claude plugin marketplace update asuswrt`.

## Removing it again

`scripts/uninstall.sh` removes every installed component: the command binaries
and uv tool directory, the MCP registration in Claude Code (all three scopes)
and in Codex, the ChatGPT connector LaunchAgent and local state, the plugin, its
marketplace entry and saved `pluginConfigs` switches, the skill leftovers from
before v0.8.0, and the Claude Desktop extension — its unpacked directory, the
virtualenv uv built inside it, its enabled/disabled file, and its row in
`extensions-installations.json`, which is the one that makes Desktop stop
listing it. It skips whatever is not there.

It deliberately leaves tool-owned workspace history and per-project state
alone, including Claude Code transcripts and Codex companion state. It also
leaves `~/.cache/uv`, which is shared with every other uv project on the
machine. Removing the local ChatGPT connector does not delete the remote OpenAI
tunnel or the app in ChatGPT; remove those separately if they are no longer
needed.

```bash
./scripts/uninstall.sh          # dry run, lists what it would remove
./scripts/uninstall.sh --yes
```

Run from inside a clone it also runs `git clean -xd`, returning the working
tree to the state `git clone` leaves it in — useful for testing an install from
scratch. It refuses if tracked files have uncommitted changes.

Two things it keeps unless asked: `~/.config/asuswrt` with your password
(`--password`) and the clone's `.claude/` directory (`--repo-all`).

## When the tools load

The tool names are all prefixed `asuswrt`, so they only come up for a request
about your router. Say **asus** once in the first request; after that the agent
uses them for *who's on my WiFi?* or *what devices are on my home network?* on
its own.

The two settings this project will not turn on
([Trend Micro and DoS](../README.md#two-settings-this-project-will-not-turn-on))
are repeated in the `get_overview`, `get_firewall_and_filters` and `get_nvram`
descriptions, because an agent that reads `fw_dos_x=0` with no other context
reports it as a gap to close. A test pins the wording in all three. The
reasoning and sources are in [settings.md](settings.md).

## The MCP server

It speaks MCP over **stdio**, so the host starts `asuswrt-mcp` as a child
process. There is no URL, no port and no network listener.

Verified against the `mcp` Python SDK 2.1.1 and protocol revision
**2026-07-28**. The `initialize` handshake answers `2025-11-25` on purpose:
2026-07-28 is not reachable through `initialize` at all — it is offered via
`server/discover` — and the SDK handles both ends of that. Nothing here pins a
protocol version.

### What the agent is allowed to do

The server starts read-only. Writes are opt-in, through environment variables
set where you registered the server — or, in Claude Desktop, the two switches in
the extension's settings, which set the same variables:

| What you set | Tools the agent sees |
| --- | --- |
| *(nothing)* | the 15 read tools |
| `ASUSWRT_MCP_ALLOW_WRITES=1` | + 11 tools that change settings |
| both that and `ASUSWRT_MCP_ALLOW_DANGEROUS=1` | + `reboot_router`, `upgrade_firmware` |

`ASUSWRT_MCP_ALLOW_DANGEROUS` on its own does nothing; it needs the writes
variable too. Both open on `1`, `true`, `yes` or `on`, and on nothing else.
Tools you have not enabled are never advertised — the agent cannot see them,
cannot call them, and they cost no context. The variables are read once at
startup, so after changing one, restart the host.

### The tools

```
reads (always)       get_overview  get_system  get_health  get_wan
                     get_dns  get_led  get_upnp  list_clients
                     get_firewall_and_filters  get_parental_control
                     list_port_forwards  list_guest_networks  get_wireless
                     check_firmware_update  get_nvram

writes (opt-in)      add_port_forward  remove_port_forward
                     set_port_forwarding_enabled  set_parental_control_enabled
                     set_guest_network_enabled  set_wps_enabled
                     set_wifi_security  set_wifi_country
                     set_wan_dns  set_led_enabled  set_upnp_enabled

dangerous (both)     reboot_router  upgrade_firmware
```

`get_overview` is the cheap starting point — a summary (client *counts*, no
firmware check, no raw nvram) for one login. `check_firmware_update` makes the
router contact ASUS and takes about 5 seconds.

### Every write is two calls

Write tools take `confirm: bool = False`. Without it they change nothing:

```jsonc
// confirm omitted → preview only
{"status": "preview", "applied": false,
 "change": "…one sentence…", "warnings": ["…"], "current": {…}}

// confirm: true → applied, with before/after for nvram writes
{"status": "applied", "applied": true, …}
```

This mirrors the CLI's dry-run-then-`--yes` rule, and it lives in the server
because it cannot be delegated to the host: the MCP spec treats tool annotations
as *hints*, and some hosts auto-approve. `upgrade_firmware` additionally
requires `to`, the exact version string from `check_firmware_update`, so nothing
ever flashes whatever happened to turn up.

### The Claude Desktop extension

`extension/asuswrt.mcpb` is an [MCP Bundle](https://github.com/modelcontextprotocol/mcpb)
— a zip holding `manifest.json`, an icon, and the server as source. Claude
Desktop reads the manifest, shows the install dialog, and stores the two
switches as `ASUSWRT_MCP_ALLOW_WRITES` and `ASUSWRT_MCP_ALLOW_DANGEROUS` — the
same variables the terminal hosts pass. It stringifies the checkboxes to
`"true"` and `"false"`, which `gate_open` accepts.

**It contains the server.** The manifest declares `server.type: "uv"`
([MCPB manifest 0.4](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md#uv-runtime-v04)),
so the archive ships `pyproject.toml`, `uv.lock` and `src/`, and the host's uv
resolves Python 3.13 and the dependencies on first launch. Nothing is vendored
and nothing is compiled — the spec forbids `server/lib` and `server/venv` in a
uv bundle. Claude Desktop runs `uv sync` first, then the manifest's command:

```
uv run --directory ${__dirname} --locked --no-dev --extra mcp asuswrt-mcp
```

Until v0.8.0 the bundle carried no server at all, only a shell launcher that
located a separately `uv tool install`-ed `asuswrt-mcp`. That existed because
Claude Desktop ships no Python runtime and refused to install a Python bundle
without a system Python; the uv runtime is the supported fix for exactly that
([mcpb#84](https://github.com/modelcontextprotocol/mcpb/issues/84)). The last
launcher-based bundle is kept at
`extension/legacy/asuswrt-legacy.mcpb` for hosts too old to read a 0.4
manifest.

Two things to know when working on it:

- `compatibility.claude_desktop` is `>=1.40609.0`, which is the oldest build
  the bundle has actually been installed and used on, not a number from a
  changelog. Anthropic does not publish which Desktop release started
  understanding `server.type: "uv"`, so the floor is deliberately conservative:
  older builds may well work, and the way to lower it is to install the bundle
  on one and confirm, never to guess. The previous manifest carried an
  unverified `>=0.10.0` that meant nothing.
- There is no `platform_overrides` block, deliberately. `uv run` is identical
  on all three platforms, and some MCPB implementations replace the whole
  environment when an override supplies `env` — which would drop the two write
  gates.

Rebuild it after editing the manifest. The build refuses to run unless
`manifest.json`, `pyproject.toml` and `.claude-plugin/plugin.json` all declare
the same version, and writes every entry with a fixed timestamp so an unchanged
tree rebuilds byte-identically:

```bash
python3 extension/build.py
# or: npx @anthropic-ai/mcpb pack extension extension/asuswrt.mcpb
```

One wart: Claude Desktop's initial `uv sync` installs the `dev` dependency
group, so pytest lands in the extension's virtualenv even though `--no-dev` on
the launch command keeps it off the server's own path. It is wasted disk, not a
correctness problem.

### Claude Desktop, by hand

**Settings → Developer → Edit Config** opens
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows, if you would rather
skip the extension and write the entry yourself:

```json
{
  "mcpServers": {
    "asuswrt": {
      "command": "/Users/you/.local/bin/asuswrt-mcp",
      "env": {
        "ASUSWRT_MCP_ALLOW_WRITES": "1"
      }
    }
  }
}
```

Drop the `env` block for a read-only server. The path must be absolute.

Codex's app and IDE extension have a form for this: **Settings → MCP servers →
Add server** (the gear menu in the IDE extension). Choose **STDIO**, name it
`asuswrt`, and give the absolute path to `asuswrt-mcp`. Or edit
`~/.codex/config.toml`:

```toml
[mcp_servers.asuswrt]
command = "asuswrt-mcp"
env = { ASUSWRT_MCP_ALLOW_WRITES = "1" }
```

## Other agents

Any host implementing the MCP spec works unchanged: the server is
`asuswrt-mcp`, over stdio.

## Adding a setting the tool does not cover

Find the variable name by diffing the router's settings around a single change:

```bash
ssh admin@192.168.50.1 'nvram show 2>/dev/null | sort' > before.txt
# change exactly one setting in the web UI, save
ssh admin@192.168.50.1 'nvram show 2>/dev/null | sort' > after.txt
diff before.txt after.txt
```

The diff names the variable and shows how it is encoded. Add it to
`FIREWALL_VARS` in `src/asuswrt/ops.py` to read it — the CLI and the MCP server
both pick it up from there — and record it in the settings reference. SSH is
enabled in the web UI under *Administration → System → Service → Enable SSH*; it
is not exposed in the mobile app.

