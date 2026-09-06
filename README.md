# ASUS Router MCP Server for AI agents

This project installs a small python program that allows your AI agents to query and modify some of the settings on your Asus WRT router. The program essentially controls what the AI is allowed to do, which is safer than relying on prompt guardrails only.

Once installed, you can ask questions like "What devices are on my network", "Review the security settings" or "relay incoming port 8808 to my laptop at port 88".

It works directly with the following AI agents:

- Claude Code
- Codex
- Claude Desktop
- Gemini CLI
- ChatGPT — requires the macOS connector

## Installation

**Requirements:**
- [uv](https://docs.astral.sh/uv/) — it fetches Python 3.13 itself

Your router password is collected locally, stored in secure
storage or a private config file (~/.config/asuswrt/.env), and never sent to a model.

**Claude Code**

```bash
claude plugin marketplace add gittycat/asuswrt-ai-tools
claude plugin install asuswrt@asuswrt
```

Enter the router password in the masked install dialog. The username and router
address are optional.

**Claude Desktop**

```bash
curl -LO https://raw.githubusercontent.com/gittycat/asuswrt-ai-tools/main/extension/asuswrt.mcpb
open asuswrt.mcpb
```

Enter the router password in the masked install dialog. On Windows, download
the same file and open it from **Settings → Extensions → Advanced settings**.

**Codex**

```bash
uv tool install "asuswrt[mcp] @ git+https://github.com/gittycat/asuswrt-ai-tools"
asuswrt setup
codex mcp add asuswrt -- asuswrt-mcp
```

**Gemini CLI**

```bash
uv tool install "asuswrt[mcp] @ git+https://github.com/gittycat/asuswrt-ai-tools"
asuswrt setup
gemini mcp add --scope user asuswrt asuswrt-mcp
```

`asuswrt setup` asks for the username and password without echoing the password.
It detects the router address from the default gateway.

**ChatGPT**

An extra connector program needs to be installed first for ChatGPT. Download it
from the [latest release](https://github.com/gittycat/asuswrt-ai-tools/releases),
then follow the [ChatGPT connector guide](docs/chatgpt-connector.md). Its
installer asks for the router password without echoing it.

Connections start read-only. See the
[permissions reference](docs/reference.md#what-the-agent-is-allowed-to-do) to
allow changes.

If the router is not your default gateway, enter its address in the Claude
dialog or run `asuswrt setup --host ADDRESS`. `ROUTER_SSL` and `ROUTER_PORT`
are config-file-only settings; the dialogs and `asuswrt setup` do not expose
them. See
[Router credentials](docs/credentials.md) for details.

### Check

Ask:

```text
What model is my ASUS router?
```

If it answers, the connection is working.

## Try it

The word "router" is too generic so you may want to use **ASUS router** in your first request so the tools load. Once loaded, the MCP server will be used for any requests related to your router or network.

Eg: 

```text
What's my Asus router security settings.

Is my router firmware up to date?
The internet feels slow. Check my router's settings.
Open port 32400 for my media server.
Turn on the guest Wi-Fi.
Turn off WPS.
```

Every change to the router settings is previewed and needs your confirmation.

## Two settings this project will not turn on

**AiProtection** — and with it Traffic Analyzer, Adaptive QoS and Web History.
They are all gated behind one Trend Micro EULA that sends browsing data off the
router.

**DoS protection** — it only rate-limits traffic to roughly one packet per
second, which breaks legitimate connections without stopping a real flood.

Both reading as off is the expected state, not a gap to close. The reasoning
and the sources are in [settings.md](docs/settings.md).

## Terminal

Install the command and try it directly:

```bash
uv tool install "asuswrt[mcp] @ git+https://github.com/gittycat/asuswrt-ai-tools"
asuswrt setup
asuswrt system
asuswrt system health
asuswrt clients --online
```

See the [terminal reference](docs/reference.md#using-the-terminal) for all
commands, JSON output, and making changes.

To remove everything again, run `./scripts/uninstall.sh` from a clone — see
[Removing it again](docs/reference.md#removing-it-again).

## Compatibility

Tested on an ASUS RT-AX59U with stock firmware. Other AsusWRT and AsusWRT-Merlin
routers may work.

## Docs

- [Router credentials](docs/credentials.md) — setup, storage, and custom
  connection settings
- [Reference](docs/reference.md) — commands, permissions, safety, and limits
- [Settings](docs/settings.md) — supported router settings and technical notes
- [Troubleshooting](docs/troubleshooting.md) — common connection problems
- [ChatGPT connector](docs/chatgpt-connector.md) — install and manage the
  connector

## Credits

- **[asusrouter](https://github.com/Vaskivskyi/asusrouter)** by
  [Vaskivskyi](https://github.com/Vaskivskyi) (Apache-2.0) — the HTTP API client
  for AsusWRT that does all the protocol work here. Also used by the core Home
  Assistant AsusWRT integration.
- **[mcp](https://github.com/modelcontextprotocol/python-sdk)** — the official
  Python SDK for the Model Context Protocol, which runs the stdio server.
- **[python-dotenv](https://github.com/theskumar/python-dotenv)** — loads the
  router credentials from the `.env` file.

## License

MIT
