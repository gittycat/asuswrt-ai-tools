# ASUS Router MCP Server for AI agents

![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

This project installs a small python program that allows your AI agents to query and modify some of the settings on your Asus WRT router. The program essentially controls what the AI is allowed to do, which is safer than relying on prompt guardrails only.

Once installed, you can ask questions like "What devices are on my network", "Review the security settings" or "relay incoming port 8808 to my laptop at port 88".

It works directly with the following AI agents:

- Claude Code
- Codex
- Claude Desktop
- Gemini CLI
- ChatGPT — desktop app only

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

**ChatGPT and Codex**

```bash
uv tool install "asuswrt[mcp] @ git+https://github.com/gittycat/asuswrt-ai-tools"
asuswrt setup
codex mcp add asuswrt -- asuswrt-mcp
```

The ChatGPT desktop app shares its MCP configuration with Codex CLI, so the
command above is the whole setup. Restart the app and `asuswrt` appears under
**Settings → MCP servers**.

This needs the ChatGPT desktop app, not ChatGPT Classic. ChatGPT on the web
and on phones cannot run a local MCP server.

If the router is not your default gateway, enter its address in the Claude
dialog or run `asuswrt setup --host ADDRESS`. `ROUTER_SSL` and `ROUTER_PORT`
are config-file-only settings; the dialogs and `asuswrt setup` do not expose
them. See
[Router credentials](docs/credentials.md) for details.


**Gemini CLI**

```bash
uv tool install "asuswrt[mcp] @ git+https://github.com/gittycat/asuswrt-ai-tools"
asuswrt setup
gemini mcp add --scope user asuswrt asuswrt-mcp
```

`asuswrt setup` asks for the username and password without echoing the password.
It detects the router address from the default gateway.

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

## Opinionated results

These tools do more than report whether every switch is on or off. For settings
whose names can overstate their protection — including firewall packet
logging, DoS protection and Trend Micro AiProtection — results include a brief
recommendation based on ASUS documentation and experienced AsusWRT community
reports, together with the reason and important trade-offs.

Community guidance is clearly labelled as such. Results distinguish security
baselines, such as keeping the firewall enabled, from optional features and
leave those choices with you. A recommendation never changes the router:
every supported change is previewed and still requires your confirmation.

See [settings.md](docs/settings.md#security-decision-context) for the fuller
reasoning, limitations and sources.

## Security

The MCP server starts read-only. Tools that change settings must be explicitly
enabled, while reboot and firmware upgrade require a second dangerous-actions
gate. Even when writes are enabled, every change is returned as a preview
first and requires a separate confirmed call before it is applied.

The server exposes named, validated operations rather than arbitrary router
access. Raw NVRAM values can be read but not written, and there is no generic
command, SSH or router-API passthrough; unsupported changes must be made
manually in the router's web interface.

These limits are enforced by the server's code, not just by instructions in an
AI prompt. Prompt-level guardrails can be misunderstood or ignored, but an
agent cannot call a write tool that was not enabled or a raw mutation endpoint
that does not exist. This keeps the available authority narrow even if the
agent makes a mistake.

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

macOS only — tested on macOS 27. Linux and Windows are untested and unsupported.

Tested on an ASUS RT-AX59U with stock firmware. Other AsusWRT and AsusWRT-Merlin
routers may work.

## Docs

- [Router credentials](docs/credentials.md) — setup, storage, and custom
  connection settings
- [Reference](docs/reference.md) — commands, permissions, safety, and limits
- [Settings](docs/settings.md) — supported router settings and technical notes
- [Troubleshooting](docs/troubleshooting.md) — common connection problems

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
