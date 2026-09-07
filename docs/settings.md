# AsusWRT settings reference

ASUS publishes no documentation for nvram variables or for the HTTP API. Every
entry below carries how it was established, so you can tell a verified fact
from an educated guess.

**Provenance labels**

| Label | Meaning |
|---|---|
| `library` | Read from the `asusrouter` package source. Definitive for how this tool behaves. |
| `hardware` | Observed on a live RT-AX59U, firmware `3.0.0.4.388.34011_gfae8cb3`. |
| `unverified` | From general AsusWRT knowledge. Confirm with `asuswrt nvram get <var>` before relying on it. |

Anything marked `unverified` can be promoted in one command:

```bash
asuswrt nvram get fw_enable_x fw_dos_x        # empty result = wrong name for this firmware
```

---

## Data types available through the API

Fetched by name via the library's `AsusData` enum. Status is what an RT-AX59U
actually returned.

| Data type | Status | Contents |
|---|---|---|
| `cpu` | `hardware` OK | `total` plus one key per core (4 on RT-AX59U) |
| `ram` | `hardware` OK | `free`, `total`, `used`, `usage` — all in kB |
| `boottime` | `hardware` OK | `datetime`, `uptime` (seconds) |
| `wan` | `hardware` OK | `internet`, `0`, `1`, `aggregation`, `dualwan` |
| `network` | `hardware` OK | traffic counters per interface |
| `clients` | `hardware` OK | dict keyed by MAC |
| `wlan` | `hardware` OK | `2ghz`, `5ghz` |
| `gwlan` | `hardware` OK | `2ghz_1..3`, `5ghz_1..3` |
| `port_forwarding` | `hardware` OK | `state`, and `rules` **only when rules exist** |
| `parental_control` | `hardware` OK | `state`, `block_all`, `rules` |
| `led` | `hardware` OK | `state` |
| `ports` | `hardware` OK | `wan`, `lan`, `usb` |
| `firmware` | `hardware` OK | current/available versions |
| `aimesh` | `hardware` OK | dict keyed by node MAC |
| `system` | `hardware` empty | Not exposed on RT-AX59U stock firmware |
| `temperature` | `hardware` empty | Not exposed on RT-AX59U stock firmware |

### Shapes that are easy to get wrong

`library` + `hardware`, all three confirmed by reading the parsers and then
observing the failure on real data:

- **CPU usage is a delta.** `process_cpu` computes `usage` by differencing the
  current sample against the previous one. A single fetch always yields
  `usage: None`. Two samples are required.
- **WAN is nested.** There is no top-level `status` or `ip`. Use
  `wan["internet"]["link"]` and `wan["internet"]["ip_address"]`; per-port state
  is at `wan[0]` / `wan[1]`, selected by `wan["internet"]["unit"]`.
- **Client online state is not `client.state`.** `ConnectionState` is an
  `IntEnum` (`CONNECTED = 1`), and the reliable flag is
  `client.connection.online`.
- **`port_forwarding["rules"]` may not exist.** `process_port_forwarding` only
  sets the key when `vts_rulelist` is non-empty. Always use `.get("rules") or []`.

---

## nvram variables

### Port forwarding — `library`, `hardware`

| Variable | Meaning |
|---|---|
| `vts_enable_x` | Global switch. `0` = off, `1` = on |
| `vts_rulelist` | All rules, as one string |

Encoding, from `process_port_forwarding` (read) and `compile_port_forwarding`
(write). Records are separated by `<`, fields by `>`; over HTTP they arrive
escaped as `&#60` and `&#62`:

```
<name>ext_port>internal_ip>internal_port>protocol>source_ip>
```

Field order is identical in both directions. `protocol` is `TCP`, `UDP`,
`BOTH` or `OTHER`. Empty fields are written as empty strings, not omitted.

The list is a **single nvram string**, so there is no per-rule API: adding one
rule means reading all of them, appending, and writing the whole list back.
Applying calls the `restart_firewall` service.

### Wireless — `library`

| Variable | Meaning |
|---|---|
| `wl<i>_radio` | Radio on/off for band `i` (`0` = 2.4 GHz, `1` = 5 GHz) |
| `wl<i>.<n>_bss_enabled` | Guest network `n` (1-3) on band `i` |
| `wl<i>.<n>_expire` | Guest network expiry; set to `0` when enabling |

This is why the CLI's `--band 2ghz --id 1` becomes `wl0.1_bss_enabled`.
Applying calls `restart_wireless;restart_firewall`.

### Wireless security — `hardware` for the reads, mixed for the writes

Written by `asuswrt wifi wps|security|country`. All reads below were observed on
an RT-AX59U; the write values are marked separately because accepting a write
is not the same as the value sticking.

| Variable | Meaning | Provenance |
|---|---|---|
| `wps_enable` | WPS runtime flag | `hardware` |
| `wps_enable_x` | WPS as the web UI writes it | `hardware` |
| `wps_multiband` | WPS active on both bands | `hardware` |
| `wl<i>_auth_mode_x` | WPA mode for band `i` | `hardware` (`psk2` observed) |
| `wl<i>_crypto` | Cipher; `aes` for anything modern | `hardware` |
| `wl<i>_mfp` | 802.11w: `0` off, `1` capable, `2` required | `hardware` |
| `wl<i>_country_code` | Regulatory region | `hardware` |

`wps_enable` and `wps_enable_x` are both present and both `1` on a router with
WPS on, so `asuswrt wifi wps` writes both — otherwise the radio and the web UI
disagree about the state.

`auth_mode_x` values, of which only `psk2` is confirmed on this firmware:

| CLI `--mode` | `auth_mode_x` | Default `mfp` | Provenance |
|---|---|---|---|
| `wpa2` | `psk2` | `0` | `hardware` |
| `wpa2wpa3` | `psk2sae` | `1` (capable) | `unverified` |
| `wpa3` | `sae` | `2` (required) | `unverified` |

WPA3 requires management frame protection, which is why the mode carries the
`mfp` default with it. `--mfp` overrides it when a stubborn legacy client will
not associate.

**Country code is frequently locked.** Stock firmware derives the regulatory
region from the hardware SKU, and a write to `wl<i>_country_code` can be
accepted and then silently ignored. `reg_spec` and `location_code` show what
the firmware believes it is. This is why `apply_nvram` reads every variable
back and `_report_apply` fails the command when a value did not change — never
report a wireless write as applied on the strength of the service result
alone.

Applying any of these calls `restart_wireless`, which drops every wireless
client for a few seconds.

### Firmware — `hardware`

`AsusData.FIRMWARE` on an RT-AX59U returns `current`, `available`, `state`
(true when an update exists), the same three for `_beta`, a `webs` sub-dict of
status enums, and `release_note`. `available` is `None` whenever `state` is
false, so test `state` rather than truthiness of the version string.

Both actions go through `AsusSystem`:

| State | Service | Effect |
|---|---|---|
| `FIRMWARE_CHECK` | `firmware_check` | Router queries ASUS; result lands in `AsusData.FIRMWARE` |
| `FIRMWARE_UPGRADE` | `firmware_upgrade` | Downloads, writes flash, reboots |

Both are dispatched with `service=None` and `expect_modify=False`
(`system.py::STATE_MAP`), so `async_set_state` returns True as soon as the
request is sent. **It is not evidence that anything happened.** The check is
asynchronous with no completion signal, which is why `asuswrt firmware`
sleeps before re-reading; the upgrade reports nothing at all, which is why
`asuswrt firmware upgrade` says "requested" and points at `asuswrt system`.

`asuswrt firmware` runs the check every time rather than reporting the
stored value, because `webs_state_info` is refreshed only by the router's own
periodic check and `webs_update_enable` is `0` by default — the stored version
can be arbitrarily old.

Distinguishing "no update" from "could not check" matters and is easy to get
wrong. `webs["available"]` is populated only from an actual reply, so an empty
one means nothing was learned, while a populated one no newer than `current`
means genuinely up to date. `_update_status` returns `unknown` for the first
and `current` for the second; `firmware["state"]` alone cannot tell them
apart.

### Security decision context

The MCP security reads return brief structured advisories alongside the raw
state. They are decision context, not automatic findings: distinguish an ASUS
default from community experience, distinguish a baseline control from an
optional feature, and let the user decide whether an optional trade-off suits
their network. Do not score every off switch as a vulnerability.

**Base firewall — keep it enabled.** ASUS defaults the firewall on and packet
logging to `None`. A typical IPv4 setup with no port forwards and no WAN web
administration already has little unsolicited inbound exposure, but that does
not make the firewall switch redundant: router-local filtering still matters,
and IPv6 does not rely on NAT. Report `fw_enable_x=0` as a security finding;
do not report `fw_log_x=none` as one.

- [ASUS: Introduction of Firewall on ASUS router](https://www.asus.com/us/support/faq/1013630/)
- [ASUS: How to set up IPv6 Firewall](https://www.asus.com/support/faq/1013638/)

**Packet logging — normally leave it at `None`.** Internet-facing addresses
receive continuous unsolicited probes. Logging dropped packets therefore
produces a great deal of routine noise, does not change what the firewall
blocks, and can crowd DHCP, wireless and authentication events out of the
router's finite local log. Enable it temporarily when investigating a defined
network problem, not as a general security upgrade. For longer retention,
ASUS supports sending system logs to a remote server (port 514 by default);
remote storage is useful even when packet-drop logging stays off. Log capacity
varies by model and firmware, so do not repeat a fixed 256 KB or “rotates in
hours” claim without measuring the target router.

- [SNBForums: System log spammed with kernel DROP messages](https://www.snbforums.com/threads/system-log-spammed-with-kernel-drop-messages.42108/)
- [ASUS: How to save System Log locally or on a remote server](https://www.asus.com/au/support/faq/1044954/)

**Trend Micro features — an informed choice.** `bwdpi_db_enable` gates the
Trend Micro engine. On the hardware and firmware documented here, accepting
the bundled EULA covers AiProtection, Traffic Analyzer, Apps Analyzer,
Adaptive QoS, Game Boost and Web History and starts sending browsing-related
data to Trend Micro. The sub-flags `wrs_mals_enable`, `wrs_cc_enable` and
`wrs_vp_enable` can read `1` while `bwdpi_db_enable` is `0`; that combination
means configured but not running.

AiProtection is not worthless: ASUS documents malicious-site reputation and
intrusion/infected-device checks, and community reports include real blocks.
It is also not endpoint malware scanning or a guarantee against novel threats.
Community reports on throughput, RAM use and false positives vary by router
and workload. Present the known-destination protection, data-sharing consent
and possible performance cost in one short trade-off; off is not a missing
baseline and on is the user's decision.

- [Trend Micro features — do you turn them on?](https://www.snbforums.com/threads/asus-router-features-powered-by-trend-micro-do-you-turn-them-on-and-agree-to-have-your-data-collected.82962/)
- [Privacy and TrendMicro](https://www.snbforums.com/threads/privacy-and-trendmicro.55956/)
- [What data is sent to Trend Micro for each feature](https://www.snbforums.com/threads/what-data-is-sent-to-trend-micro-for-each-of-these-features.63471/)
- [ASUS: How AiProtection protects a home network](https://www.asus.com/support/faq/1012070/)
- [SNBForums: Does AiProtection really work?](https://www.snbforums.com/threads/does-aiprotection-really-work.72764/)

**DoS protection — normally leave `fw_dos_x=0`.** ASUS documents this as an
optional control that defaults off and may affect router performance. The
community's practical objection is that router-side rate limits can disrupt
legitimate bursts, while a volumetric flood has already saturated the WAN link
before the router can help. Treat `0` as a normal home-router posture, explain
the trade-off if asked, and leave a different choice with the user rather than
silently enabling it.

- [ASUS: Introduction of Firewall on ASUS router](https://www.asus.com/us/support/faq/1013630/)
- [DoS Protection from Asus Firewall — on or off?](https://www.snbforums.com/threads/dos-protection-from-asus-firewall-on-or-off.41149/)
- [Should I enable ASUS DoS Protection](https://www.snbforums.com/threads/should-i-enable-asus-dos-protection.45641/)
- [DoS protection breaks Cloudflare / Emby](https://www.snbforums.com/threads/firewall-enable-dos-protection-i-have-to-turn-it-off-for-cloudflare-emby-to-work.55058/)

**DNS rebind protection — normally worth turning on.** `dns_norebind` passes
`--stop-dns-rebind` to dnsmasq, which rejects and logs upstream answers in
private space: RFC1918, IPv4-mapped private, and IPv6 link-local and ULA.
`127.0.0.0/8` and `::1` are covered too unless `rebind-localhost-ok` is added,
which the stock UI does not expose. ASUS ships it off — `{ "dns_norebind", "0" }`
in `release/src/router/shared/defaults.c` — so an off reading is the default,
not a changed setting.

The community position is mild but one-directional: no measurable overhead, no
reported problems over years of use, occasional log lines. Nobody argues for
leaving it off. The real cost is false positives on names that legitimately
resolve into private or loopback space — `plex.direct`, `amazonmusiclocal.com`,
`localhost.megasyncloopback.mega.nz` — and on split-horizon DNS, where a public
hostname points at a LAN address. The dnsmasq fix is `rebind-domain-ok`, which
stock firmware has no field for, so on a network running those services the
choice is on-with-breakage or off.

Present it as minor hardening, not a baseline. It filters one step of a
rebinding attack and is documented as bypassable by CNAME or `0.0.0.0`, and
Chrome's Local Network Access permission prompt (Chrome 142 desktop, extended
to WebSocket and WebTransport in 147) blocks the browser path at the source.
There is no write tool: point the user at WAN > Internet Connection.

- [SNBForums: DNS Rebind Protection — on or off?](https://www.snbforums.com/threads/dns-rebind-protection-on-or-off.95088/)
- [SNBForums: Share possible DNS-Rebind logs](https://www.snbforums.com/threads/share-possible-dns-rebind-logs.48595/)
- [dnsmasq man page — `--stop-dns-rebind`, `--rebind-localhost-ok`, `--rebind-domain-ok`](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html)
- [Plex: How to use secure server connections](https://support.plex.tv/articles/206225077-how-to-use-secure-server-connections/)
- [NCC Group Singularity: DNS rebinding protection bypasses](https://github.com/nccgroup/singularity/wiki/Protection-Bypasses)
- [Chrome for Developers: new permission prompt for Local Network Access](https://developer.chrome.com/blog/local-network-access)
- [ASUS: How can I improve router security](https://www.asus.com/support/faq/1039292/)

### DNS — `hardware`

Not covered by any library data type; read straight from nvram. The WAN
settings are keyed by unit, so read `wan["internet"]["unit"]` first — `wan_dns1_x`
with no index is empty on real hardware while `wan0_dns1_x` holds the value, and
the live unit becomes 1 after a dual-WAN failover.

| Variable | Meaning |
|---|---|
| `wan<N>_dnsenable_x` | `1` take DNS from the ISP, `0` use the pair below |
| `wan<N>_dns1_x` / `wan<N>_dns2_x` | the manual servers. IPv4 only — IPv6 lives in `ipv6_dns1_x` |
| `wan<N>_dns` | what is actually in use, space-separated |
| `dhcp_dns1_x` / `dhcp_dns2_x` | what LAN clients are told to use, if not the router |
| `dhcpd_dns_router` | `1` advertise the router itself as the resolver |
| `lan_dnsenable_x` | LAN-side resolver override |
| `dnspriv_enable` | DNS-over-TLS |
| `dnssec_enable` | DNSSEC validation |
| `dns_norebind` | DNS rebind protection |
| `dns_fwd_local` | forward local-domain queries upstream |

**Applied with `restart_wan_dns <unit>`, and the unit is not optional.**
`hardware`, the hard way. Bare `restart_wan_dns` returns success and does
nothing: `wan<N>_dns1_x` reads back as the new server while `wan<N>_dns` — the
pair dnsmasq actually forwards to — still holds the old one, so every lookup
keeps going to the previous resolver. `restart_dnsmasq` does not fix it either;
dnsmasq reads `wan<N>_dns`, and only the WAN script rewrites that. Observed on
RT-AX59U:

| Service called | `wan0_dns` afterwards |
|---|---|
| `restart_wan_dns` | `1.1.1.1 1.0.0.1` — unchanged |
| `restart_dnsmasq` | `1.1.1.1 1.0.0.1` — unchanged |
| `restart_wan_dns 0` | `8.8.8.8 8.8.4.4` |

The library's `AsusSystem.RESTART_WAN_DNS` comment calls the number optional. On
this firmware it is not. `wan<N>_dns` settles a few seconds after the call, so
`apply_nvram`'s immediate read-back cannot confirm it — read `wan<N>_dns`
separately, or run `asuswrt dns` and look at `In use`.

**Choosing a resolver is not cosmetic.** Google, and most large CDNs, pick which
server delivers content from the *resolver's* apparent location, refined by the
EDNS Client Subnet option — the resolver passing along a truncated form of the
client's network. Resolvers that decline to send ECS, which 1.1.1.1 does
deliberately for privacy, leave the CDN guessing.

Measured on this network (`hardware`, 2026-09-05, WAN 115.186.199.235):

| Router forwards to | `redirector.googlevideo.com` resolves to | ping |
|---|---|---|
| 1.1.1.1 | 142.251.119.139 | 207 ms avg, 129–316 |
| 8.8.8.8 | 192.178.187.102 | 11 ms |

`dig +short TXT o-o.myaddr.l.google.com` is the check: through 8.8.8.8 it
answers `edns0-client-subnet 115.186.199.0/24`, through 1.1.1.1 it answers a
Cloudflare address and no subnet. The symptom is a site that loads while its
video or downloads stall — `youtube.com` is anycast and fine, `googlevideo.com`
is unicast and 18x further away. Resolvers that send ECS include `8.8.8.8` /
`8.8.4.4` and `9.9.9.11`; `1.1.1.1` and plain `9.9.9.9` do not.

### DNS Filter — a considered answer, not a settled one

**Default recommendation: do not propose it.** Unlike the Trend Micro and DoS
entries above, this is not a permanent no — there is a specific signal that
makes it the right answer, named at the end. Read this before recommending it
either way; the feature sounds more useful than it is.

| Variable | Live value observed | Meaning |
|---|---|---|
| `dnsfilter_enable_x` | `0` | on/off |
| `dnsfilter_mode` | `0` | global mode |
| `dnsfilter_rulelist` | `` | per-client rules |
| `dnsfilter_custom1..3` | `8.8.8.8` | three custom resolver slots |

Names and values are `hardware`; **whether the feature does anything on stock
firmware is `unverified`.** DNS Filter is primarily an AsusWRT-**Merlin**
feature, and this router is stock (`merlin: false`). The variables exist and
carry sensible defaults, but that is also what vestigial variables from a shared
codebase look like. Confirming it would mean enabling it and watching whether a
device's queries are actually redirected. No command wraps it.

**What it does.** The router advertises itself as the resolver over DHCP, and
devices normally comply — but complying is voluntary. A device can address
`1.1.1.1` directly and ignore the router. DNS Filter adds firewall rules that
intercept outbound port 53 and redirect it to a resolver you choose, globally or
per client by MAC. It converts a suggestion into enforcement.

**Why that is narrower than it sounds — the part to tell the user.** It only
catches plain DNS on port 53, and that is not how bypass happens now:

- **DNS-over-HTTPS** is ordinary HTTPS on port 443. Firefox and Chrome do this
  by default in many configurations. DNS Filter cannot see it, let alone
  redirect it.
- **iCloud Private Relay** tunnels DNS and traffic to Apple. When it is on, both
  DNS Filter and the router's own WAN DNS setting are irrelevant for that device.

So it enforces against the compliant and the old, not against anything modern or
deliberate. Recommending it as a way to "make sure devices use your DNS" would
overstate what it delivers.

**It also breaks things.** VPN clients and corporate split-DNS need their own
resolver. Redirecting those silently produces failures that are hard to trace
back to the router.

**The signal that makes it the right answer:** one device misbehaving while
others are fine — a smart TV or streaming stick that buffers or fails to resolve
while a laptop on the same network is healthy. Plenty of TVs, Chromecasts and IoT
devices ship with a resolver compiled in and ignore DHCP entirely, and those *do*
use plain port 53, so DNS Filter does catch them. That asymmetry between devices
is the evidence. Absent it, changing the WAN DNS (`asuswrt dns set`) already
covers every device that complies, which is nearly all of them.

Recorded 2026-09-05, after WAN DNS was moved off 1.1.1.1 for the ECS reason
above. The question that prompted it — "is DNS Filter needed here?" — was
answered no on this network because the Mac was already resolving through the
router, so there was nothing to enforce.

### UPnP — `hardware` for the names, `unverified` for which one is master

| Variable | Meaning |
|---|---|
| `upnp_enable` | UPnP on/off |
| `wan_upnp_enable` | the same, active-unit alias |
| `wan<N>_upnp_enable` | the same, per unit |
| `upnp_secure` | `1` a device may only map a port to itself |
| `upnp_mnp` | advertise the service on the LAN |
| `upnp_port` | listen port; `0` means auto |

All three switches read `0` on the router this was built against, so there was
no way to observe which one the firmware treats as authoritative. `set_upnp`
therefore writes all three, the way `set_wps` writes both WPS flags — whichever
is the real one, the result is the state that was asked for, and the read-back
covers all three. Note the contrast with DNS: the bare `wan_` alias is populated
here, while its `wan_dns1_x` sibling is empty. Do not generalise either way.

Applied with `restart_upnp`. **The off→on→off round trip has not been run on
hardware** — UPnP was already off and enabling it, even briefly, is the exact
hole the command exists to close. Writing `0` over `0` is confirmed to be
accepted and to read back correctly; a genuine on→off transition is not.

### LEDs — `hardware`

| Variable | Meaning |
|---|---|
| `led_val` | `1` lights on, `0` off. The live variable |
| `led_disable` | empty on RT-AX59U — do not read this one |

Applied with `start_ctrl_led`, which returns no `modify` flag — see the service
call note above.

### Firewall and filtering — `unverified`

Not covered by any library data type; the CLI reads them straight from nvram.

| Variable | Expected meaning |
|---|---|
| `fw_enable_x` | Firewall on/off |
| `fw_dos_x` | DoS protection — leave `0`, see above |
| `fw_log_x` | Firewall logging |
| `misc_http_x` | Web UI reachable from the WAN |
| `url_enable_x` | URL filter on/off |
| `url_rulelist` | URL filter entries, same `<`/`>` encoding |
| `keyword_enable_x` | Keyword filter on/off |
| `keyword_rulelist` | Keyword filter entries |

If `asuswrt firewall` shows `? (None)` for one of these, the name is wrong for
this firmware. Find the real one by changing the setting in the web UI and
diffing `nvram show` over SSH — see the README section on extending coverage.
Promote the entry to `hardware` once you have confirmed it.

---

## Service calls

`library`. Applying a setting means writing the nvram keys and naming a service
to restart. The services this tool uses:

| Service | Restarts |
|---|---|
| `restart_firewall` | Port forwarding, filtering, parental control |
| `restart_wireless` | Radios and guest networks |
| `restart_wan_dns <unit>` | WAN resolvers — the unit is required, see below |
| `start_ctrl_led` | Status lights |
| `restart_upnp` | UPnP daemon |
| `reboot` | The whole router |

An RT-AX59U reports 97 available services (`hardware`). The library's
`AsusSystem` enum lists the ones it knows how to call.

### Not every service reports whether it changed anything

`library`. `async_call_service` returns the router's `modify` flag when
`expect_modify` is set, which is the default. A service that does not send that
flag therefore reports `False` — a successful write that reads as a failure.
`start_ctrl_led` is the known case, and the library passes `expect_modify=False`
for it (`modules/led.py`). `router.apply_nvram` takes the same argument; pass
`expect_modify=False` for such a service and let the read-back decide.

### `async_set_state` returns `False` for a state it cannot set — it does not raise

`library`. This one is worth knowing before wrapping any new `Asus*` enum as a
write. `modules/state.py::set_state` looks the state up in `AsusStateMap`, and
where the entry is `None` — `AsusState.DDNS`, `AsusState.CONNECTION`,
`AsusState.NONE` — or where the module has no `set_state` function, it logs at
`debug` and returns `False`. Nothing is sent to the router.

With library logging off, which is the normal case, that is indistinguishable
from a router that received the request and refused it. `AsusDDNS` is the trap
in practice: the enum exists, `async_set_state(AsusDDNS.ACTIVE)` type-checks and
runs, and it silently does nothing.

**Before wrapping a new state as a write, confirm both:** its module defines
`set_state`, *and* its `AsusStateMap` entry is not `None`. If either fails, use
`apply_nvram` with the right service instead — that is how `dns` and `led` are
implemented.

---

## Reading any variable

The HTTP API exposes a generic nvram read. This is the same mechanism the
library uses to collect device identity, so it is as reliable as the rest:

```bash
asuswrt --json nvram vts_rulelist wl0.1_bss_enabled fw_enable_x
```

`asuswrt nvram` has deliberately no write counterpart. Blind nvram writes are
the fastest way to brick a working configuration, so every write this tool can
perform is exposed as a named command with its own validation — `pf`, `guest`,
`parental` and `wifi`. `router.apply_nvram` is the shared implementation: it
writes the variables, restarts the named service, then reads the variables
back so a write the firmware quietly refused is reported as a failure.
