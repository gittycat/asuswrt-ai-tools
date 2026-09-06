#!/bin/sh
# Download, verify and install the ASUSWRT ChatGPT connector on an
# Apple-silicon Mac. It fetches the release archive from GitHub, checks it
# against the .sha256 published beside it, unpacks it to a temporary directory
# and runs the installer inside. The temporary directory goes away again; the
# connector installs everything it keeps under $HOME.
#
# Run it straight from the network:
#
#   /bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/gittycat/asuswrt-ai-tools/main/scripts/install-connector.sh)"
#
# The `sh -c "$(curl ...)"` form matters. Piping into `sh` hands the script's
# stdin to curl, and the installer would then have no terminal to read the
# router password, tunnel ID and runtime key from.
#
# Options are passed through to the bundled installer, so the permission level
# works the same way as it does from an unpacked archive:
#
#   ... install-connector.sh)" -- --permission writes
#   ... install-connector.sh)" -- --permission dangerous
#
# The `--` is what `sh -c` needs to stop treating the next word as $0.
#
# --version picks a release other than the latest, with or without the v:
#
#   ... install-connector.sh)" -- --version 0.8.0
#
# uv is not checked here. The bundled installer already looks for it in the
# places a double-clicked installer cannot see, and one copy of that message is
# enough; the cost of finding out after the download is a few megabytes.
#
set -eu

REPO=gittycat/asuswrt-ai-tools
VERSION=

# Rotate through the arguments once: keep --version and --help, push
# everything else back onto the end for the bundled installer.
remaining=$#
while [ "$remaining" -gt 0 ]; do
  arg=$1
  shift
  remaining=$((remaining - 1))
  case $arg in
    --version)
      if [ "$remaining" -eq 0 ]; then
        echo "--version needs a release number" >&2
        exit 2
      fi
      VERSION=$1
      shift
      remaining=$((remaining - 1))
      ;;
    --version=*)
      VERSION=${arg#--version=}
      ;;
    # The whole comment header, however long it grows: every line from the
    # shebang to the first line that is not a comment.
    -h|--help)
      sed -n '2,${/^[^#]/q;p;}' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      set -- "$@" "$arg"
      ;;
  esac
done

# Checked here as well as in the bundled installer so an unsupported Mac is
# told before it downloads anything.
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "asuswrt-chatgpt-connector supports Apple-silicon macOS only." >&2
  exit 2
fi
major=$(sw_vers -productVersion | cut -d. -f1)
case "$major" in
  ''|*[!0-9]*)
    echo "Could not determine the macOS version." >&2
    exit 2
    ;;
esac
if [ "$major" -lt 27 ]; then
  echo "asuswrt-chatgpt-connector requires macOS 27 or later." >&2
  exit 2
fi

if [ -n "$VERSION" ]; then
  case $VERSION in
    v*) tag=$VERSION ;;
    *)  tag=v$VERSION ;;
  esac
else
  # /releases/latest redirects to /releases/tag/<tag>. Reading the tag out of
  # that redirect needs no JSON parsing and no API rate limit.
  latest=$(curl -fsSL -o /dev/null -w '%{url_effective}' \
    "https://github.com/$REPO/releases/latest") || {
    echo "Could not reach GitHub to find the latest release." >&2
    exit 1
  }
  tag=${latest##*/}
  case $tag in
    v[0-9]*) ;;
    *)
      echo "Could not read a release tag out of $latest" >&2
      exit 1
      ;;
  esac
fi

name=asuswrt-chatgpt-connector-$tag-macos27-arm64
base=https://github.com/$REPO/releases/download/$tag

work=$(mktemp -d "${TMPDIR:-/tmp}/asuswrt-connector.XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM

echo "Downloading $name.tar.gz"
curl -fsSL --proto '=https' --tlsv1.2 -o "$work/$name.tar.gz" "$base/$name.tar.gz" || {
  echo "No connector archive in release $tag." >&2
  exit 1
}
curl -fsSL --proto '=https' --tlsv1.2 -o "$work/$name.tar.gz.sha256" "$base/$name.tar.gz.sha256" || {
  echo "Release $tag has the archive but not its .sha256 checksum." >&2
  exit 1
}

# The checksum file names the archive without a path, so verify from inside
# the download directory.
(cd "$work" && shasum -a 256 -c "$name.tar.gz.sha256" >/dev/null) || {
  echo "Checksum mismatch on $name.tar.gz. The download was not installed." >&2
  exit 1
}

tar -xzf "$work/$name.tar.gz" -C "$work"

status=0
sh "$work/$name/install.sh" "$@" || status=$?
exit "$status"
