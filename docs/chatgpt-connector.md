# ChatGPT connector

asuswrt-chatgpt-connector runs the ASUSWRT MCP server on an Apple-silicon Mac
and connects it to ChatGPT through [OpenAI's Secure MCP
Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
It is intended for developer-mode testing while this project is under active
development and is not published in the MCP Registry.

    ChatGPT -> OpenAI tunnel -> tunnel-client -> asuswrt-mcp -> router

The Mac opens an outbound HTTPS connection to OpenAI. There is no inbound
listener and the router password stays on the Mac.

## Requirements

- Apple-silicon Mac running macOS 27 or later
- [uv](https://docs.astral.sh/uv/)
- An OpenAI tunnel ID and restricted runtime API key
- ChatGPT developer-mode access
- The Mac connected to the same network as the ASUS router

Create the tunnel in
[Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels).
Associate it with the ChatGPT workspace in which the app will be created.
Create a restricted runtime key with **Tunnels Read + Use**. Do not use an
OpenAI admin key as the long-running connector key.

## Install

Run this in a terminal. It downloads the latest release, checks it against the
published SHA-256, and runs the installer:

    /bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/gittycat/asuswrt-ai-tools/main/scripts/install-connector.sh)"

The installer prompts for the router password, tunnel ID, and runtime key. The
key prompts are hidden. Existing router configuration at
~/.config/asuswrt/.env is reused.

Read-only is the default. To expose ordinary write tools, or to additionally
expose reboot and firmware upgrade, pass the permission level after a `--`:

    /bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/gittycat/asuswrt-ai-tools/main/scripts/install-connector.sh)" -- --permission writes
    /bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/gittycat/asuswrt-ai-tools/main/scripts/install-connector.sh)" -- --permission dangerous

The `--` is what `sh -c` needs to stop reading the next word as the script
name. `-- --version 0.8.0` installs a release other than the latest.

Use the `sh -c "$(curl ...)"` form rather than piping into `sh`: a pipe takes
over the script's standard input, and the installer would have no terminal to
read the password and keys from.

## Install from the archive instead

Download the archive and its .sha256 file from the matching project release:

    asuswrt-chatgpt-connector-v<VERSION>-macos27-arm64.tar.gz
    asuswrt-chatgpt-connector-v<VERSION>-macos27-arm64.tar.gz.sha256

Verify, unpack, and install:

    shasum -a 256 -c asuswrt-chatgpt-connector-v<VERSION>-macos27-arm64.tar.gz.sha256
    tar -xzf asuswrt-chatgpt-connector-v<VERSION>-macos27-arm64.tar.gz
    cd asuswrt-chatgpt-connector-v<VERSION>-macos27-arm64
    ./install.sh

The development archive is checksum-protected but is not yet notarized by this
project. A browser download is quarantined by macOS; if it blocks the archive,
review the download and approve it under **System Settings → Privacy &
Security**. The one-line install above avoids this: curl sets no quarantine
flag.

After changing the permission level, refresh the app's tool catalogue in
ChatGPT.

## What protects the download

Three things, and it is worth knowing where each one stops.

The release archive is published with a `.sha256` beside it, and
scripts/install-connector.sh verifies it before unpacking anything. That
catches a corrupted or truncated download and a network attacker who rewrites
the archive in transit. It proves nothing about origin: the checksum lives in
the same GitHub release as the archive, so anyone who can write to that release
replaces both files at once.

OpenAI's tunnel-client binary is pinned by hash in
packaging/chatgpt_connector/build_release.py (`PINNED_SHA256`). The build
cross-checks the upstream checksum file against that committed hash and fails
if they disagree, so a change to the one component this project did not write
cannot land without a human reviewing it. This is the control that matters most
here.

The archive is built deterministically — `deterministic_targz` zeroes mtimes,
uids and gids — so a suspicious user can rebuild it and compare bytes.

### Deliberately not done

**Sigstore build-provenance attestation**
(`actions/attest-build-provenance` in the release workflow). It would bind the
archive's digest to the commit and workflow that produced it, which defends
against someone uploading a hand-built archive to the release page. Anyone able
to do that already holds the GitHub account, and can therefore push to main,
produce a *valid* attestation for a malicious commit, and rewrite
install-connector.sh as well — so it signs the front door while the attacker
owns the house. Attestation earns its cost when a project is consumed by other
software or by enterprises who must answer SLSA questionnaires. Neither applies
to a single-maintainer developer tool whose users can read the source.

**Notarization, and a signed .pkg or .dmg installer.** A DMG is the wrong
container for this: it is the drag-an-.app-to-Applications format, and this
ships a CLI plus a LaunchAgent. Unsigned it is worse than the tarball, because
double-clicking quarantines everything inside and the user still has to open a
terminal. A notarized .pkg is the correct native answer, and costs an Apple
Developer Program membership plus a notarytool step in CI. Revisit only if this
ever targets people who will not use a terminal.

**Pinning the install-connector.sh URL to a release tag.** Every comparable
bootstrap installer — rustup, nvm, Homebrew, uv — serves its script from the
default branch over TLS and verifies nothing about the script itself. Pinning
would leave a stale URL in the README after each release for no practical gain.

## Connect the app in ChatGPT

While the connector is running:

1. Open ChatGPT's plugin/app settings.
2. Create a developer-mode app.
3. Choose **Tunnel** under Connection.
4. Select the tunnel, or paste the tunnel ID printed by the installer.
5. Scan the tools and create the app.

Ask **what model is my ASUS router?** to verify the complete path.

## Manage it

    asuswrt-chatgpt-connector status
    asuswrt-chatgpt-connector doctor
    asuswrt-chatgpt-connector stop
    asuswrt-chatgpt-connector start
    asuswrt-chatgpt-connector restart

The local tunnel UI URL is written to:

    ~/Library/Application Support/asuswrt-chatgpt-connector/health-url

## Remove it

    asuswrt-chatgpt-connector uninstall

This removes the LaunchAgent, tunnel runtime key, tunnel profile, bundled
tunnel client, and connector logs. It keeps the router password and does not
delete the remote OpenAI tunnel. Add --router-config to remove the saved router
credentials too.

The connector relies on OpenAI's developer-mode and Secure MCP Tunnel product
availability. ChatGPT clients that do not expose custom apps cannot use it,
even when the local LaunchAgent is healthy.
