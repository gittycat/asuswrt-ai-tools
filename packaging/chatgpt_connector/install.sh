#!/bin/sh
# Bootstrap the release-bundled ASUSWRT ChatGPT connector for one macOS user.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

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

# A double-clicked installer inherits the bare GUI PATH, not your shell's, so
# look in the usual uv install locations before giving up.
if ! command -v uv >/dev/null 2>&1; then
  for dir in "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin "$HOME/.cargo/bin"; do
    if [ -x "$dir/uv" ]; then
      PATH="$dir:$PATH"
      export PATH
      break
    fi
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'EOF'
uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/
and then run this installer again.

If uv is already installed, this installer could not see it: a double-clicked
installer does not inherit your shell PATH. Run it from a terminal instead:
  sh /path/to/install.sh
EOF
  exit 2
fi

wheel=
for candidate in "$here"/*.whl; do
  if [ -f "$candidate" ]; then
    if [ -n "$wheel" ]; then
      echo "Release contains more than one wheel." >&2
      exit 2
    fi
    wheel=$candidate
  fi
done
if [ -z "$wheel" ]; then
  echo "Release is missing the asuswrt wheel." >&2
  exit 2
fi
if [ ! -x "$here/tunnel-client" ]; then
  echo "Release is missing the tunnel-client executable." >&2
  exit 2
fi

uv tool install --force --with 'mcp>=2,<3' "$wheel"
connector="$(uv tool dir --bin)/asuswrt-chatgpt-connector"
if [ ! -x "$connector" ]; then
  echo "uv installed the package but the connector command was not found." >&2
  exit 2
fi

exec "$connector" install --tunnel-client "$here/tunnel-client" "$@"
