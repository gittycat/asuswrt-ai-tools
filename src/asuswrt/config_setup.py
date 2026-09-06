"""Interactive creation of the router credential file.

Shared by `asuswrt setup` and the ChatGPT connector installer so that the two
cannot drift on the thing that matters here: the file is written with 0600
from the start, never chmod-ed afterwards, and the password is never echoed.

The GUI installers (the Claude Code plugin dialog, the Claude Desktop
extension) collect the same credentials through their own `user_config` form
and pass them as environment variables, so their users never reach this code.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path

from asuswrt.router import ConfigError


def default_env_path() -> Path:
    """Where `asuswrt setup` writes, honouring the usual override."""
    if override := os.getenv("ASUSWRT_ENV_FILE"):
        return Path(override).expanduser()
    return Path.home() / ".config" / "asuswrt" / ".env"


def dotenv_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def private_write(path: Path, contents: str | bytes, mode: int = 0o600) -> None:
    """Write with the final permissions already in place.

    os.open() with the mode applies it at creation, so there is no window in
    which a credential file is readable by the rest of the machine.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, mode)
    try:
        if isinstance(contents, str):
            contents = contents.encode()
        os.write(fd, contents)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def prompt_nonempty(prompt: str, *, secret: bool = False) -> str:
    reader = getpass.getpass if secret else input
    while True:
        value = reader(prompt).strip()
        if value:
            return value


def render_env(username: str, password: str, host: str | None = None) -> str:
    values = [username, password, host or ""]
    if any("\x00" in value for value in values):
        raise ConfigError("router credentials cannot contain a NUL character")
    lines = [
        f"ROUTER_USER={dotenv_quote(username)}",
        f"ROUTER_PASS={dotenv_quote(password)}",
        "ROUTER_SSL=false",
    ]
    if host:
        lines.append(f"ROUTER_HOST={dotenv_quote(host)}")
    return "".join(f"{line}\n" for line in lines)


def prompt_for_credentials() -> tuple[str, str]:
    username = input("Router username [admin]: ").strip() or "admin"
    return username, prompt_nonempty("Router password: ", secret=True)


def write_credentials(
    path: Path, username: str, password: str, host: str | None = None
) -> None:
    private_write(path, render_env(username, password, host))


def ensure_router_config(path: Path) -> None:
    """Create the credential file if it is not there yet."""
    if path.is_file():
        return
    print("Router credentials are not configured yet.")
    write_credentials(path, *prompt_for_credentials())


def run_setup(
    path: Path | None = None, *, force: bool = False, host: str | None = None
) -> int:
    """`asuswrt setup`: prompt and write, refusing to clobber by accident.

    The only place in the project that creates the credential file, so that
    nobody is ever told to hand-write one.
    """
    path = path or default_env_path()
    if path.is_file() and not force:
        print(f"{path} already exists. Re-run with --force to replace it.")
        return 1
    write_credentials(path, *prompt_for_credentials(), host)
    print(f"Saved to {path}")
    print("Try it with: asuswrt system")
    return 0
