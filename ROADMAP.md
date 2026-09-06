# Roadmap

Things deliberately left out, and what would have to be true to add them.

## Streamable HTTP transport for the MCP server

The server speaks stdio only. That is the right default here: the router is a
LAN device, and the credential the server holds is the router's **admin
password**. stdio means the server runs as a child of the agent that uses it,
on the same machine, reachable by nothing else.

HTTP would let a hosted agent reach the router — a real use, and the only
reason to want it. It would also raise three questions stdio never asks:

- **Who may connect.** An HTTP endpoint is reachable by everything on the
  network segment it binds to, not just the agent you meant.
- **What authenticates.** MCP's own auth story would have to sit in front of
  admin-password-equivalent access, and a bearer token stored somewhere is a
  second credential to lose.
- **Which interface to bind.** Loopback is safe and pointless for a remote
  agent; anything wider needs the first two answered first.

None of that is hard, and all of it is wasted work without a concrete use
case. When there is one — a specific host that cannot spawn a subprocess —
answer the three questions against *that* host and implement it.

## Writes enabled by default

Today the MCP server registers its 15 read tools always, its 11 write tools
only under `ASUSWRT_MCP_ALLOW_WRITES=1`, and `reboot_router` /
`upgrade_firmware` only when `ASUSWRT_MCP_ALLOW_DANGEROUS=1` is also set. The
default install can look at the router and cannot change it.

Shipping writes on by default would need all of:

- **A host-side approval step that can be relied on.** The MCP spec says tool
  annotations are *hints*; a host may auto-approve `destructive_hint=true`. The
  server's `confirm: bool` two-step exists precisely because the host's dialog
  cannot be assumed. Enabling writes by default means trusting something the
  spec does not promise.
- **A recorded reason to.** The gate costs one environment variable, set once.
  Nobody has yet been slowed down by it.
- **Evidence the preview text is good enough to act on.** The preview is the
  only thing standing between an agent and a config change. It has not been
  exercised against real misuse.

Until then the default stays read-only, which is the setting that cannot go
wrong unattended.

## DDNS

Dynamic DNS keeps a fixed hostname — `something.asuscomm.com` — pointed at a
home connection whose ISP-assigned IP keeps changing. The router re-registers
its current address with the DDNS provider whenever it moves, so anything
published from home stays reachable without paying for a static IP. It is off
here (`ddns_enable_x=0`).

Left out of the round that added `dns` and `led`. Reading it would work today:
`AsusData.DDNS` is wired up normally and the library parses the provider's
status into `DDNSStatusCode`. Writing it needs one thing known first.

**The library has no DDNS setter.** `asusrouter/modules/ddns.py` defines
`AsusDDNS`, `DDNSStatusCode` and `DDNSStatusHint` — three enums and a status
parser, and no `set_state` function. `AsusStateMap[AsusState.DDNS]` is `None`
(`modules/state.py`). So `async_set_state(AsusDDNS.ACTIVE)` does not raise and
does not write: it falls through, logs at `debug`, and returns `False`. See the
warning in `docs/settings.md` — this is a general property
of `async_set_state`, not a DDNS quirk.

A DDNS write therefore has to go the same way `dns` and `led` went: `apply_nvram`
with `ddns_enable_x`, `ddns_server_x` and `ddns_hostname_x`, restarting
`restart_ddns_le` (`AsusSystem.DDNS_RESTART`). What would have to be true to add
it:

- **Somewhere to verify it.** Every write in this tool was confirmed against a
  live RT-AX59U. DDNS is off on the only router available, so enabling it *is*
  the test, and that means registering a real hostname with a real provider.
- **A decision about credentials.** Custom DDNS providers take a username and
  password, which would be a second secret for this tool to hold. ASUS's own
  `asuscomm.com` does not, and is the only case worth supporting first.
