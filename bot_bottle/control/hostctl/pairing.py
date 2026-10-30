"""Pairing with a console (ADR 0007): what `bot-bottle pair` keeps on disk
under `~/.bot-bottle/`, and this host's iroh identity. `remote.json` (whether
the link runs, and the pinned console), `console.json` (the console's URL and
access token), `remote.key` (the iroh secret key) and `remote.status.json`
(the link's state, for `bot-bottle status`). The link itself is `remote.py`.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...paths import bot_bottle_root

CONSOLE_FILE = "console.json"
REMOTE_FILE = "remote.json"
STATUS_FILE = "remote.status.json"
KEY_FILE = "remote.key"


class RemoteError(Exception):
    """Remote access can't be configured or can't run."""



@dataclass(frozen=True)
class RemoteConfig:
    """`remote.json`: whether the link runs, and the pinned console identity."""

    enabled: bool = False
    console_node_id: str = ""
    console_ticket: str = ""


def _root() -> Path:
    return bot_bottle_root()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_config() -> RemoteConfig:
    try:
        raw = json.loads((_root() / REMOTE_FILE).read_text())
    except (OSError, ValueError):
        return RemoteConfig()
    if not isinstance(raw, dict):
        return RemoteConfig()
    return RemoteConfig(
        enabled=raw.get("enabled") is True,
        console_node_id=str(raw.get("console_node_id", "")),
        console_ticket=str(raw.get("console_ticket", "")),
    )


def save_config(config: RemoteConfig) -> None:
    _write_private(_root() / REMOTE_FILE, json.dumps(asdict(config), indent=2) + "\n")


def load_console() -> dict[str, Any]:
    """`console.json`, written by `bot-bottle pair`."""
    try:
        raw = json.loads((_root() / CONSOLE_FILE).read_text())
    except (OSError, ValueError) as e:
        raise RemoteError("not paired with a console; run `bot-bottle pair` first") from e
    if not isinstance(raw, dict) or not raw.get("url") or not raw.get("access_token"):
        raise RemoteError("console.json is incomplete; run `bot-bottle pair` again")
    return raw


def save_console(console: dict[str, Any]) -> None:
    _write_private(_root() / CONSOLE_FILE, json.dumps(console, indent=2) + "\n")


def secret_key() -> bytes:
    """This host's iroh secret key (32 bytes, 0600), created on first use. Its
    public half is the node id."""
    path = _root() / KEY_FILE
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def import_iroh() -> "Any":
    try:
        import iroh  # pylint: disable=import-outside-toplevel  # pyright: ignore[reportMissingImports]
    except ImportError as e:
        raise RemoteError(
            "remote access needs the iroh bindings: install `bot-bottle[remote]` "
            "(not available on Intel Macs)") from e
    return iroh


def node_id() -> str:
    """This host's iroh node id."""
    iroh = import_iroh()
    return str(iroh.SecretKey.from_bytes(secret_key()).public())


def write_status(**fields: object) -> None:
    """`remote.status.json`, for `bot-bottle status`."""
    try:
        _write_private(_root() / STATUS_FILE,
                       json.dumps({"updated": time.time(), **fields}) + "\n")
    except OSError:
        pass


def read_status() -> dict[str, Any]:
    try:
        raw = json.loads((_root() / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


__all__ = [
    "CONSOLE_FILE", "KEY_FILE", "REMOTE_FILE", "STATUS_FILE", "RemoteConfig", "RemoteError",
    "import_iroh", "load_config", "load_console", "node_id", "read_status", "save_config",
    "save_console", "secret_key", "write_status",
]
