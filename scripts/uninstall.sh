#!/usr/bin/env bash
# Remove every installed component of this project from a Mac: the CLI, the
# MCP registrations in Claude Code, Codex and Gemini CLI, the ChatGPT
# connector, the Claude Code plugin and its saved settings, the skill leftovers
# from before v0.8.0, and the Claude Desktop extension along with its
# virtualenv and install record. Safe to run when only some of them are
# present. Tool-owned workspace history is left alone.
#
# Everything that goes is printed as one sorted list, nothing else.
#
# Not touched: ~/.cache/uv. Since v0.8.0 the plugin and the extension resolve
# their dependencies through uv, which caches wheels there — shared with every
# other uv project on the machine, so removing it is never this script's call.
#
# Run from inside a clone and it also clears the build artifacts, caches and
# .venv out of the working tree. Files you added since the clone — notes,
# scratch files, anything git does not ignore — are left where they are.
#
# The router login stays put unless --password is passed. Since v0.8.0 that
# login is written by `asuswrt setup` (and by the ChatGPT connector installer)
# to ~/.config/asuswrt/.env, or to $ASUSWRT_ENV_FILE when that is set; a clone
# may also hold its own .env. A password typed into the Claude Code plugin or
# Claude Desktop extension dialog is not a file at all — it lives in that app's
# secure storage and leaves with the plugin or extension.
#
#   ./scripts/uninstall.sh                     # dry run, changes nothing
#   ./scripts/uninstall.sh --yes               # do it, keep the login and .claude/
#   ./scripts/uninstall.sh --yes --password    # also delete the saved router login
#   ./scripts/uninstall.sh --yes --repo-all    # also delete the clone's .claude/
#   ./scripts/uninstall.sh --yes --quiet       # say nothing unless something failed
#
# Exits 1 when something it found could not be removed, so a test suite can
# tear down with `--yes --quiet` and let a dirty machine fail the run. Point
# HOME at a temporary directory to keep such a sweep off your own install:
# every path here hangs off $HOME.
#
set -uo pipefail

APPLY=0
DROP_PASSWORD=0
DROP_CLAUDE_DIR=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes)      APPLY=1 ;;
    --password)    DROP_PASSWORD=1 ;;
    --repo-all)    DROP_CLAUDE_DIR=1 ;;
    -q|--quiet)    QUIET=1 ;;
    # The whole comment header, however long it grows: every line from the
    # shebang to the first line that is not a comment.
    -h|--help)     sed -n '2,${/^[^#]/q;p;}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Nothing prints while the sweep runs. Every removal is collected here and the
# whole set is sorted and printed once at the end, so the output is a list of
# what goes rather than a running commentary.
ITEMS=()      # one line each, the thing that is removed
MANUAL=()     # found, but only a human can remove it
N_ITEMS=0
N_MANUAL=0
N_FAIL=0
LAST=""

say()    { printf '%s\n' "$*"; }
hit()    { ITEMS[$N_ITEMS]="$1"; N_ITEMS=$((N_ITEMS+1)); LAST="$1"; }
manual() { MANUAL[$N_MANUAL]="$1"; N_MANUAL=$((N_MANUAL+1)); }

# ~ for $HOME, so the list stays narrow enough to scan.
short() { printf '%s' "${1/#$HOME/\~}"; }

# Mark an already-listed line as failed. Defaults to the line just added,
# which is the common case; pass a label when the removal runs later.
fail() {
  local want="${1:-$LAST}" i=0
  while [ "$i" -lt "$N_ITEMS" ]; do
    [ "${ITEMS[$i]}" = "$want" ] && ITEMS[$i]="$want   ! failed"
    i=$((i+1))
  done
  N_FAIL=$((N_FAIL+1))
}

# Run a command only when applying.
do_cmd() {
  [ "$APPLY" -eq 1 ] || return 0
  "$@" >/dev/null 2>&1 || fail
}

# Delete a path only when applying.
do_rm() {
  [ "$APPLY" -eq 1 ] || return 0
  rm -rf "$1" 2>/dev/null || fail "${2:-$LAST}"
}

# ------------------------------------------------------ ChatGPT connector ---
CONNECTOR_LABEL="io.github.gittycat.asuswrt-chatgpt-connector"
CONNECTOR_PLIST="$HOME/Library/LaunchAgents/$CONNECTOR_LABEL.plist"
CONNECTOR_STATE="$HOME/Library/Application Support/asuswrt-chatgpt-connector"
# Stopping the agent is part of removing its plist, not a thing of its own.
if [ "$APPLY" -eq 1 ] && { [ -e "$CONNECTOR_PLIST" ] || [ -d "$CONNECTOR_STATE" ]; }; then
  launchctl bootout "gui/$UID/$CONNECTOR_LABEL" >/dev/null 2>&1 || true
fi
if [ -e "$CONNECTOR_PLIST" ]; then
  hit "$(short "$CONNECTOR_PLIST")"
  do_rm "$CONNECTOR_PLIST"
fi
if [ -d "$CONNECTOR_STATE" ]; then
  hit "$(short "$CONNECTOR_STATE")"
  do_rm "$CONNECTOR_STATE"
fi

# ---------------------------------------------------------------- the CLI ---
# Everything is found before anything is removed, so a dry run and a real run
# list exactly the same paths — `uv tool uninstall` takes most of them with it.
CLI_PATHS=()
CLI_N=0
for f in asuswrt asuswrt-mcp asuswrt-probe asuswrt-chatgpt-connector; do
  if [ -e "$HOME/.local/bin/$f" ] || [ -L "$HOME/.local/bin/$f" ]; then
    CLI_PATHS[$CLI_N]="$HOME/.local/bin/$f"
    CLI_N=$((CLI_N+1))
  fi
done
if [ -d "$HOME/.local/share/uv/tools/asuswrt" ]; then
  CLI_PATHS[$CLI_N]="$HOME/.local/share/uv/tools/asuswrt"
  CLI_N=$((CLI_N+1))
fi
UV_TOOL=0
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^asuswrt'; then
  UV_TOOL=1
fi
if [ "$UV_TOOL" -eq 1 ] || [ "$CLI_N" -gt 0 ]; then
  i=0
  while [ "$i" -lt "$CLI_N" ]; do
    hit "$(short "${CLI_PATHS[$i]}")"
    i=$((i+1))
  done
  [ "$CLI_N" -gt 0 ] || hit "asuswrt  (uv tool)"
  if [ "$APPLY" -eq 1 ]; then
    [ "$UV_TOOL" -eq 1 ] && uv tool uninstall asuswrt >/dev/null 2>&1
    i=0
    while [ "$i" -lt "$CLI_N" ]; do
      p="${CLI_PATHS[$i]}"
      if [ -e "$p" ] || [ -L "$p" ]; then
        rm -rf "$p" 2>/dev/null || fail "$(short "$p")"
      fi
      i=$((i+1))
    done
  fi
fi

# ------------------------------------------------------------- Claude Code ---
# `claude mcp remove` only clears one scope at a time, so try all three: the
# server can be registered in ~/.claude.json (user), per project, or locally.
# Match only an actual mcpServers key. Claude also records unrelated history
# such as skillUsage.asuswrt in ~/.claude.json, which must not count as a
# configured server.
claude_mcp_registered() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$HOME/.claude.json" .mcp.json <<'PY' 2>/dev/null
import json
import sys

for path in sys.argv[1:]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        continue

    if "asuswrt" in (data.get("mcpServers") or {}):
        sys.exit(0)
    for project in (data.get("projects") or {}).values():
        if isinstance(project, dict) and "asuswrt" in (project.get("mcpServers") or {}):
            sys.exit(0)

sys.exit(1)
PY
    return
  fi

  # Claude Code can still perform an exact lookup if Python is unavailable.
  command -v claude >/dev/null 2>&1 && claude mcp get asuswrt >/dev/null 2>&1
}

if claude_mcp_registered; then
  if command -v claude >/dev/null 2>&1; then
    hit "$(short "$HOME/.claude.json")  (asuswrt MCP server, every scope)"
    if [ "$APPLY" -eq 1 ]; then
      for scope in local project user; do
        claude mcp remove asuswrt --scope "$scope" >/dev/null 2>&1
      done
      claude_mcp_registered && fail
    fi
  else
    manual "$(short "$HOME/.claude.json")  (asuswrt mcpServers entry — claude is not on PATH)"
  fi
fi
if command -v claude >/dev/null 2>&1; then
  if claude plugin list 2>/dev/null | grep -q asuswrt; then
    hit "asuswrt Claude Code plugin"
    do_cmd claude plugin uninstall asuswrt@asuswrt
  fi
  if claude plugin marketplace list 2>/dev/null | grep -q asuswrt; then
    hit "asuswrt Claude Code plugin marketplace"
    do_cmd claude plugin marketplace remove asuswrt
  fi
fi
# The skill folder and its symlink; the skill no longer exists as of v0.8.0.
for p in "$HOME/.claude/skills/asuswrt" "$HOME/.agents/skills/asuswrt"; do
  if [ -e "$p" ] || [ -L "$p" ]; then
    hit "$(short "$p")"
    do_rm "$p"
  fi
done
# Since v0.8.0 the plugin carries userConfig switches, and Claude Code keeps
# their values in settings.json under pluginConfigs. Uninstalling the plugin
# does not necessarily clear them, and a stale entry would silently reapply if
# the plugin is ever installed again.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ] && command -v python3 >/dev/null 2>&1; then
  if python3 -c "
import json,sys
try: d=json.load(open('$SETTINGS'))
except Exception: sys.exit(1)
cfg=d.get('pluginConfigs') or {}
sys.exit(0 if any('asuswrt' in k.lower() for k in cfg) else 1)
" 2>/dev/null; then
    hit "$(short "$SETTINGS")  (asuswrt pluginConfigs entry)"
    do_cmd python3 -c "
import json
p='$SETTINGS'
d=json.load(open(p))
cfg=d.get('pluginConfigs') or {}
for k in [k for k in cfg if 'asuswrt' in k.lower()]:
    del cfg[k]
json.dump(d, open(p,'w'), indent=2)
"
  fi
fi

# ------------------------------------------------------------------ Codex ---
CODEX_CONFIG="$HOME/.codex/config.toml"
if grep -q '^\[mcp_servers\.asuswrt\]' "$CODEX_CONFIG" 2>/dev/null; then
  if command -v codex >/dev/null 2>&1; then
    hit "$(short "$CODEX_CONFIG")  ([mcp_servers.asuswrt] block)"
    if [ "$APPLY" -eq 1 ]; then
      codex mcp remove asuswrt >/dev/null 2>&1
      grep -q '^\[mcp_servers\.asuswrt\]' "$CODEX_CONFIG" 2>/dev/null && fail
    fi
  else
    manual "$(short "$CODEX_CONFIG")  ([mcp_servers.asuswrt] block — codex is not on PATH)"
  fi
fi

# ------------------------------------------------------------ Gemini CLI ---
# `gemini mcp add --scope user` writes into ~/.gemini/settings.json; a project
# scope writes .gemini/settings.json beside the checkout. Only a real
# mcpServers entry counts, so parse the file rather than grep for the name.
GEMINI_USER="$HOME/.gemini/settings.json"
gemini_mcp_registered() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$GEMINI_USER" .gemini/settings.json <<'GPY' 2>/dev/null
import json
import sys

for path in sys.argv[1:]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        continue

    if "asuswrt" in (data.get("mcpServers") or {}):
        sys.exit(0)

sys.exit(1)
GPY
    return
  fi

  grep -q '"asuswrt"' "$GEMINI_USER" 2>/dev/null
}

if gemini_mcp_registered; then
  if command -v gemini >/dev/null 2>&1; then
    hit "$(short "$GEMINI_USER")  (asuswrt mcpServers entry, user and project)"
    if [ "$APPLY" -eq 1 ]; then
      for scope in user project; do
        gemini mcp remove asuswrt --scope "$scope" >/dev/null 2>&1
      done
      gemini_mcp_registered && fail
    fi
  else
    manual "$(short "$GEMINI_USER")  (asuswrt mcpServers entry — gemini is not on PATH)"
  fi
fi

# --------------------------------------------------------- Claude Desktop ---
DESKTOP="$HOME/Library/Application Support/Claude"
# Only a real mcpServers entry counts. A bare grep for the name matches this
# repo's own path in unrelated preference keys and cries wolf.
if [ -f "$DESKTOP/claude_desktop_config.json" ] && command -v python3 >/dev/null 2>&1; then
  if python3 -c "
import json,sys
try: d=json.load(open('$DESKTOP/claude_desktop_config.json'))
except Exception: sys.exit(1)
sys.exit(0 if 'asuswrt' in (d.get('mcpServers') or {}) else 1)
" 2>/dev/null; then
    manual "$(short "$DESKTOP/claude_desktop_config.json")  (asuswrt mcpServers entry)"
  fi
fi
# The extension is two paths named for its id (local.mcpb.<author>.<name>): the
# unpacked bundle, which since v0.8.0 also holds the .venv uv builds on first
# launch, and a one-line enabled/disabled file beside it.
while IFS= read -r p; do
  [ -n "$p" ] || continue
  hit "$(short "$p")"
  do_rm "$p"
done < <(find "$DESKTOP/Claude Extensions" "$DESKTOP/Claude Extensions Settings" \
           -maxdepth 1 -iname '*asuswrt*' 2>/dev/null)
# Deleting those two leaves the install register behind, and Desktop still
# believes the extension is installed. Drop just our entry, not the file.
INSTALLS="$DESKTOP/extensions-installations.json"
if [ -f "$INSTALLS" ] && grep -q 'asuswrt' "$INSTALLS" 2>/dev/null; then
  if command -v python3 >/dev/null 2>&1; then
    hit "$(short "$INSTALLS")  (asuswrt entry)"
    do_cmd python3 -c "
import json
p='$INSTALLS'
d=json.load(open(p))
ext=d.get('extensions') or {}
for k in [k for k in ext if 'asuswrt' in k.lower()]:
    del ext[k]
json.dump(d, open(p,'w'), indent=2)
"
  else
    manual "$(short "$INSTALLS")  (asuswrt entry — python3 missing)"
  fi
fi
# A downloaded bundle, if one is still sitting where the README's curl left it.
# Matched on the name this project gives its bundles rather than on a version,
# so the file from any release — including the legacy one — is caught.
while IFS= read -r p; do
  [ -n "$p" ] || continue
  hit "$(short "$p")"
  do_rm "$p"
done < <(find "$HOME" "$HOME/Downloads" -maxdepth 1 -name 'asuswrt*.mcpb' 2>/dev/null)

# ------------------------------------------------------------ credentials ---
# `asuswrt setup` and the ChatGPT connector installer write ~/.config/asuswrt/.env
# with mode 0600. $ASUSWRT_ENV_FILE moves that file elsewhere, so remove what it
# points at too — but only when it is somewhere the directory sweep will miss.
CRED_PATHS=("$HOME/.config/asuswrt")
case "${ASUSWRT_ENV_FILE:-}" in
  ""|"$HOME/.config/asuswrt"/*) ;;
  *) CRED_PATHS+=("${ASUSWRT_ENV_FILE/#\~/$HOME}") ;;
esac
for p in "${CRED_PATHS[@]}"; do
  [ -e "$p" ] || continue
  if [ "$DROP_PASSWORD" -eq 1 ]; then
    hit "$(short "$p")"
    do_rm "$p"
  fi
done

# ------------------------------------------------------------- repo state ---
# Only touches the working directory when it really is a clone of this repo,
# so running this from anywhere else can never delete the wrong thing.
REPO="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO" ] || [ ! -d "$REPO/src/asuswrt" ] ||
   ! grep -q '^name = "asuswrt"' "$REPO/pyproject.toml" 2>/dev/null; then
  : # not a clone of this repo — nothing to say about a working tree
elif [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  : # uncommitted changes to tracked files — leave the working tree alone
else
  # -X, not -x: only files git ignores go — .venv/, __pycache__/, the tooling
  # caches, build/, dist/, *.egg-info. Anything else you dropped into the tree
  # is not this script's to delete.
  #
  # git clean only lists them here; the deletion is done below, one path at a
  # time. Under -X a `-e` pattern makes a path *more* ignored, so it marks a
  # target rather than a keeper — the reverse of what it does under -x, and no
  # way to spare the .env this tree may hold.
  #
  # Never let git clean delete this script while bash is still reading it.
  # Once committed it is tracked and -X skips it anyway; this covers the case
  # where it was dropped into the tree untracked and ignored.
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  SELF_REL=""
  case "$SELF" in
    "$REPO"/*) SELF_REL="${SELF#$REPO/}" ;;
  esac
  while IFS= read -r line; do
    p="${line#Would remove }"
    [ -n "$p" ] || continue
    case "$p" in
      # A clone can hold its own .env, which is read before
      # ~/.config/asuswrt/.env. It is a credential file, so --password governs
      # it, not a plain --yes.
      .env|.env.*) [ "$DROP_PASSWORD" -eq 1 ] || continue ;;
      "$SELF_REL")  continue ;;
    esac
    hit "$p"
    do_rm "$REPO/$p"
  done < <(git -C "$REPO" clean -Xdn)
  # .claude/ is untracked and unignored, so -X never reaches it. It holds your
  # local Claude Code settings, which a fresh clone would not have anyway;
  # only --repo-all asks for it.
  if [ -d "$REPO/.claude" ]; then
    if [ "$DROP_CLAUDE_DIR" -eq 1 ]; then
      hit ".claude/"
      do_rm "$REPO/.claude"
    fi
  fi
fi

# ----------------------------------------------------------------- report ---
# --quiet says nothing when the sweep went as planned. What did not go as
# planned still speaks up, on stderr: a teardown that fails in silence leaves a
# dirty machine behind and the next run pays for it.
if [ "$QUIET" -eq 0 ]; then
  [ "$APPLY" -eq 1 ] || say "Dry run (pass --yes to actually remove)"

  if [ "$N_ITEMS" -eq 0 ]; then
    say "Nothing to remove."
  else
    if [ "$APPLY" -eq 1 ]; then
      if [ "$N_FAIL" -gt 0 ]; then
        say "Removed $((N_ITEMS-N_FAIL)) of $N_ITEMS items:"
      else
        say "Removed $N_ITEMS items:"
      fi
    else
      say "$N_ITEMS items would be removed:"
    fi
    say ""
    printf '%s\n' "${ITEMS[@]}" | sort | sed 's/^/  /'
  fi

  if [ "$N_MANUAL" -gt 0 ]; then
    say ""
    say "Remove by hand:"
    printf '%s\n' "${MANUAL[@]}" | sort | sed 's/^/  /'
  fi
else
  [ "$N_FAIL" -eq 0 ] || printf '%s\n' "${ITEMS[@]}" | grep '! failed' | sort >&2
  [ "$N_MANUAL" -eq 0 ] || printf 'remove by hand: %s\n' "${MANUAL[@]}" | sort >&2
fi

# Non-zero when something that was found could not be removed. Anything left
# for a human is reported but does not fail the run — nothing here can act on
# it, so a caller cannot fix it by retrying.
[ "$N_FAIL" -eq 0 ] || exit 1
exit 0

