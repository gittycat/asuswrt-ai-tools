#!/usr/bin/env bash
# Remove every installed component of this project from a Mac: the CLI, the
# MCP registrations in Claude Code, Codex and Gemini CLI, the ChatGPT
# connector, the Claude Code plugin and its saved settings, the skill leftovers
# from before v0.8.0, and the Claude Desktop extension along with its
# virtualenv and install record. Safe to run when only some of them are
# present. Tool-owned workspace history is left alone.
#
# Not touched: ~/.cache/uv. Since v0.8.0 the plugin and the extension resolve
# their dependencies through uv, which caches wheels there — shared with every
# other uv project on the machine, so removing it is never this script's call.
#
# Run from inside a clone and it also returns the working tree to the state
# git clone leaves it in, so the next install starts from nothing.
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
#
set -uo pipefail

APPLY=0
DROP_PASSWORD=0
DROP_CLAUDE_DIR=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes)      APPLY=1 ;;
    --password)    DROP_PASSWORD=1 ;;
    --repo-all)    DROP_CLAUDE_DIR=1 ;;
    # The whole comment header, however long it grows: every line from the
    # shebang to the first line that is not a comment.
    -h|--help)     sed -n '2,${/^[^#]/q;p;}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

FOUND=0
DONE=0

say()  { printf '%s\n' "$*"; }
hit()  { FOUND=$((FOUND+1)); printf '  %s\n' "$*"; }
ran()  { DONE=$((DONE+1)); }

# Run a command only when applying; otherwise just report it.
do_cmd() {
  if [ "$APPLY" -eq 1 ]; then
    if "$@" >/dev/null 2>&1; then ran; else say "      ! failed: $*"; fi
  fi
}

# Delete a path only when applying.
do_rm() {
  if [ "$APPLY" -eq 1 ]; then
    if rm -rf "$1" 2>/dev/null; then ran; else say "      ! could not remove $1"; fi
  fi
}

say "asuswrt cleanup"
[ "$APPLY" -eq 1 ] || say "(dry run — pass --yes to actually remove)"
say ""

# ------------------------------------------------------ ChatGPT connector ---
say "ChatGPT connector"
CONNECTOR_LABEL="io.github.gittycat.asuswrt-chatgpt-connector"
CONNECTOR_PLIST="$HOME/Library/LaunchAgents/$CONNECTOR_LABEL.plist"
CONNECTOR_STATE="$HOME/Library/Application Support/asuswrt-chatgpt-connector"
if [ -e "$CONNECTOR_PLIST" ] || [ -d "$CONNECTOR_STATE" ]; then
  hit "stop the asuswrt-chatgpt-connector LaunchAgent"
  if [ "$APPLY" -eq 1 ]; then
    launchctl bootout "gui/$UID/$CONNECTOR_LABEL" >/dev/null 2>&1 || true
    ran
  fi
fi
if [ -e "$CONNECTOR_PLIST" ]; then
  hit "rm ~/Library/LaunchAgents/$CONNECTOR_LABEL.plist"
  do_rm "$CONNECTOR_PLIST"
fi
if [ -d "$CONNECTOR_STATE" ]; then
  hit "rm -rf ~/Library/Application Support/asuswrt-chatgpt-connector"
  do_rm "$CONNECTOR_STATE"
fi
say "  Remote OpenAI tunnel and ChatGPT app are left in place"
say ""

# ---------------------------------------------------------------- the CLI ---
say "CLI and MCP binaries"
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^asuswrt'; then
  hit "uv tool uninstall asuswrt"
  do_cmd uv tool uninstall asuswrt
fi
# uv normally takes these with it; catch a half-removed install too.
for f in asuswrt asuswrt-mcp asuswrt-probe asuswrt-chatgpt-connector; do
  if [ -e "$HOME/.local/bin/$f" ] || [ -L "$HOME/.local/bin/$f" ]; then
    hit "rm ~/.local/bin/$f"
    do_rm "$HOME/.local/bin/$f"
  fi
done
if [ -d "$HOME/.local/share/uv/tools/asuswrt" ]; then
  hit "rm -rf ~/.local/share/uv/tools/asuswrt"
  do_rm "$HOME/.local/share/uv/tools/asuswrt"
fi

# ------------------------------------------------------------- Claude Code ---
say ""
say "Claude Code"
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
    hit "claude mcp remove asuswrt  (local, project and user scopes)"
    if [ "$APPLY" -eq 1 ]; then
      for scope in local project user; do
        claude mcp remove asuswrt --scope "$scope" >/dev/null 2>&1
      done
      if claude_mcp_registered; then
        say "      ! asuswrt MCP server is still registered — remove the mcpServers entry by hand"
      else
        ran
      fi
    fi
  else
    hit "asuswrt MCP server is registered — claude is not on PATH, remove the mcpServers entry by hand"
  fi
fi
if command -v claude >/dev/null 2>&1; then
  if claude plugin list 2>/dev/null | grep -q asuswrt; then
    hit "claude plugin uninstall asuswrt@asuswrt"
    do_cmd claude plugin uninstall asuswrt@asuswrt
  fi
  if claude plugin marketplace list 2>/dev/null | grep -q asuswrt; then
    hit "claude plugin marketplace remove asuswrt"
    do_cmd claude plugin marketplace remove asuswrt
  fi
fi
# The skill folder and its symlink; the skill no longer exists as of v0.8.0.
for p in "$HOME/.claude/skills/asuswrt" "$HOME/.agents/skills/asuswrt"; do
  if [ -e "$p" ] || [ -L "$p" ]; then
    hit "rm -rf ${p/#$HOME/\~}"
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
    hit "remove the asuswrt entry from ~/.claude/settings.json pluginConfigs"
    if [ "$APPLY" -eq 1 ]; then
      if python3 -c "
import json
p='$SETTINGS'
d=json.load(open(p))
cfg=d.get('pluginConfigs') or {}
for k in [k for k in cfg if 'asuswrt' in k.lower()]:
    del cfg[k]
json.dump(d, open(p,'w'), indent=2)
" 2>/dev/null; then ran; else say "      ! could not edit $SETTINGS"; fi
    fi
  fi
fi

# ------------------------------------------------------------------ Codex ---
say ""
say "Codex"
if grep -q '^\[mcp_servers\.asuswrt\]' "$HOME/.codex/config.toml" 2>/dev/null; then
  if command -v codex >/dev/null 2>&1; then
    hit "codex mcp remove asuswrt"
    if [ "$APPLY" -eq 1 ]; then
      codex mcp remove asuswrt >/dev/null 2>&1
      if grep -q '^\[mcp_servers\.asuswrt\]' "$HOME/.codex/config.toml" 2>/dev/null; then
        say "      ! still in ~/.codex/config.toml — delete the [mcp_servers.asuswrt] block by hand"
      else
        ran
      fi
    fi
  else
    hit "[mcp_servers.asuswrt] in ~/.codex/config.toml — codex is not on PATH, delete the block by hand"
  fi
fi

# ------------------------------------------------------------ Gemini CLI ---
say ""
say "Gemini CLI"
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
    hit "gemini mcp remove asuswrt  (user and project scopes)"
    if [ "$APPLY" -eq 1 ]; then
      for scope in user project; do
        gemini mcp remove asuswrt --scope "$scope" >/dev/null 2>&1
      done
      if gemini_mcp_registered; then
        say "      ! still registered — delete the asuswrt mcpServers entry from ~/.gemini/settings.json or .gemini/settings.json"
      else
        ran
      fi
    fi
  else
    hit "asuswrt under mcpServers in ~/.gemini/settings.json or .gemini/settings.json — gemini is not on PATH, remove that entry by hand"
  fi
fi

# --------------------------------------------------------- Claude Desktop ---
say ""
say "Claude Desktop"
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
    hit "\"asuswrt\" under mcpServers in claude_desktop_config.json — remove that entry by hand"
  fi
fi
# The extension is two paths named for its id (local.mcpb.<author>.<name>): the
# unpacked bundle, which since v0.8.0 also holds the .venv uv builds on first
# launch, and a one-line enabled/disabled file beside it.
while IFS= read -r p; do
  [ -n "$p" ] || continue
  hit "rm -rf $p"
  do_rm "$p"
done < <(find "$DESKTOP/Claude Extensions" "$DESKTOP/Claude Extensions Settings" \
           -maxdepth 1 -iname '*asuswrt*' 2>/dev/null)
# Deleting those two leaves the install register behind, and Desktop still
# believes the extension is installed. Drop just our entry, not the file.
INSTALLS="$DESKTOP/extensions-installations.json"
if [ -f "$INSTALLS" ] && grep -q 'asuswrt' "$INSTALLS" 2>/dev/null; then
  if command -v python3 >/dev/null 2>&1; then
    hit "remove the asuswrt entry from extensions-installations.json"
    if [ "$APPLY" -eq 1 ]; then
      if python3 -c "
import json
p='$INSTALLS'
d=json.load(open(p))
ext=d.get('extensions') or {}
for k in [k for k in ext if 'asuswrt' in k.lower()]:
    del ext[k]
json.dump(d, open(p,'w'), indent=2)
" 2>/dev/null; then ran; else say "      ! could not edit $INSTALLS"; fi
    fi
  else
    hit "asuswrt in extensions-installations.json — python3 missing, drop the entry by hand"
  fi
fi
# A downloaded bundle, if one is still sitting where the README's curl left it.
# Matched on the name this project gives its bundles rather than on a version,
# so the file from any release — including the legacy one — is caught.
while IFS= read -r p; do
  [ -n "$p" ] || continue
  hit "rm ${p/#$HOME/\~}"
  do_rm "$p"
done < <(find "$HOME" "$HOME/Downloads" -maxdepth 1 -name 'asuswrt*.mcpb' 2>/dev/null)

# ------------------------------------------------------------ credentials ---
say ""
say "Saved router login"
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
    hit "rm -rf ${p/#$HOME/\~}"
    do_rm "$p"
  else
    say "  ${p/#$HOME/\~} kept (pass --password to delete it too)"
  fi
done
# A password typed into the Claude Code plugin dialog or the Claude Desktop
# extension lives in that app's secure storage, not in a file this script owns.
# Removing the plugin or extension takes it with it; say so rather than leave
# someone believing --password reached every copy.
say "  passwords saved in the Claude Code plugin or Claude Desktop extension go"
say "  with the plugin or extension itself — the sections above remove those"

# ------------------------------------------------------------- repo state ---
# Only touches the working directory when it really is a clone of this repo,
# so running this from anywhere else can never delete the wrong thing.
say ""
say "Repository working tree"
REPO="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO" ]; then
  say "  (not inside a git repository — skipped)"
elif [ ! -d "$REPO/src/asuswrt" ] || ! grep -q '^name = "asuswrt"' "$REPO/pyproject.toml" 2>/dev/null; then
  say "  (not a clone of this repo — skipped)"
elif [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  say "  ! uncommitted changes to tracked files — skipping, commit or stash first:"
  git -C "$REPO" status --short --untracked-files=no | sed 's/^/      /'
else
  # -e .claude keeps your local Claude Code settings (sandbox exclusions and
  # the like), which a fresh clone would not have anyway. --repo-all drops it.
  CLEAN_ARGS=(-xd)
  [ "$DROP_CLAUDE_DIR" -eq 1 ] || CLEAN_ARGS+=(-e .claude)
  # A clone can hold its own .env, which is read before ~/.config/asuswrt/.env.
  # It is a credential file, so --password governs it, not a plain --yes.
  [ "$DROP_PASSWORD" -eq 1 ] || CLEAN_ARGS+=(-e .env -e ".env.*")
  # Never let git clean delete the script while bash is still reading it.
  # Once this file is committed git clean skips it anyway; this covers the
  # case where it was dropped into the tree untracked.
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  case "$SELF" in
    "$REPO"/*)
      SELF_REL="${SELF#$REPO/}"
      git -C "$REPO" ls-files --error-unmatch "$SELF_REL" >/dev/null 2>&1 || CLEAN_ARGS+=(-e "$SELF_REL")
      ;;
  esac
  REPO_ITEMS=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    REPO_ITEMS=$((REPO_ITEMS+1))
    hit "${line/Would remove /rm -rf }"
  done < <(git -C "$REPO" clean "${CLEAN_ARGS[@]}" -n)
  if [ "$APPLY" -eq 1 ] && [ "$REPO_ITEMS" -gt 0 ]; then
    if git -C "$REPO" clean "${CLEAN_ARGS[@]}" -f >/dev/null 2>&1; then
      DONE=$((DONE+REPO_ITEMS))
    else
      say "      ! git clean failed"
    fi
  fi
  [ "$DROP_CLAUDE_DIR" -eq 1 ] || say "  .claude/ kept (pass --repo-all to delete it too)"
  if [ "$DROP_PASSWORD" -eq 0 ] && ls "$REPO"/.env* >/dev/null 2>&1; then
    say "  .env kept (pass --password to delete it too)"
  fi
fi

# ----------------------------------------------------------------- report ---
say ""
if [ "$FOUND" -eq 0 ]; then
  say "Nothing found. This Mac is already clean."
elif [ "$APPLY" -eq 1 ]; then
  say "Removed $DONE of $FOUND items."
  say "Verify: command -v asuswrt        (should print nothing)"
  if [ "$DROP_CLAUDE_DIR" -eq 1 ] && [ "$DROP_PASSWORD" -eq 1 ]; then
    say "        git status --ignored -s   (should print nothing)"
  else
    say "        git status --ignored -s   (only retained files, .claude/ or .env, may appear)"
  fi
else
  say "$FOUND items would be removed. Re-run with --yes."
fi
