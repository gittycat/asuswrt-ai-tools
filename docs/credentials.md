# Router credentials

The router password is never requested in chat or sent to a model. It reaches
the local server through an environment variable or a local config file.

## How each installation stores it

| Installation | Prompt | Storage |
| --- | --- | --- |
| Claude Desktop | Masked extension install dialog | Host secure storage (macOS Keychain) |
| Claude Code | Masked plugin install dialog | Claude Code secure storage, not `settings.json` |
| Codex, Gemini CLI, or terminal | Hidden terminal prompt from `asuswrt setup` | `~/.config/asuswrt/.env`, mode `0600` |

The Claude dialogs also offer optional username and router address fields. A
blank username means `admin`; a blank address uses the default gateway. Blank
fields are ignored, so an existing config file continues to work.

## Config precedence

Installer-supplied environment values take priority. Otherwise, the first
config file found is used:

1. `$ASUSWRT_ENV_FILE`
2. `.env` in the current directory
3. `~/.config/asuswrt/.env`

The CLI and MCP server use the same order. If no password is found, they stop
and tell you to run `asuswrt setup`; they never open an unexpected prompt.

## `asuswrt setup`

Run:

```bash
asuswrt setup
```

It prompts for the username and password, creates the config file with mode
`0600`, and prints its path. It refuses to replace an existing file unless you
pass `--force`.

The router address normally comes from the current default gateway. If it
cannot be detected, run:

```bash
asuswrt setup --host ROUTER_ADDRESS
```

This command is the only part of the project that creates the config file.

## HTTPS and custom ports

`ROUTER_SSL` and `ROUTER_PORT` are not available in the install dialogs or as
`asuswrt setup` flags. They are only needed when the router web interface uses
HTTPS or a non-default port.

Install the terminal command if needed, run `asuswrt setup`, then update the
generated config:

```dotenv
ROUTER_SSL=true
ROUTER_PORT=8443
```

## Removing credentials

Removing the Claude Code plugin or Claude Desktop extension also removes the
password kept in its secure storage.

The project cleanup script keeps `~/.config/asuswrt/.env` by default. To remove
it too, run from a project clone:

```bash
./scripts/uninstall.sh --yes --password
```


