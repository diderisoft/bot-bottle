"""Connecting GitHub through the shared `bot-bottle` GitHub App's user access
tokens (ADR 0012), for `bot-bottle forge connect github` and the console:
GitHub's device flow authorizes this host as the operator, and the token
goes to the host secrets file behind a `github` forge in the `github` bottle.
Per launch the token adds and removes that run's deploy keys, renewed first
when it's about to expire (`contrib/github/user_token.py`).

The console starts a flow with `start_background` and follows it with
`status`; the flow waits for the operator on the host, so the console never
sees the device code's secret half or the token."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .forge_connect import (
    ConnectError, ForgeEntry, bottle_path, default_key, existing_forges, write_forge,
)
from .manifest.util import ManifestError

DEFAULT_BOTTLE = "github"
GITHUB_URL = "https://github.com"
GITHUB_SSH = "ssh://git@github.com"


@dataclass(frozen=True)
class GitHubConnected:
    """GitHub connected: as whom, where it was written, where to install."""

    key: str
    bottle: str
    path: Path
    login: str
    app: str
    install_url: str
    secret_name: str
    expiring: bool


def finish(tokens: Any, config: Any, *, bottle: str = "", cwd: Path,
           host_bottles: Path) -> GitHubConnected:
    """Write the authorized `tokens` (a `TokenSet`) as a `github` forge.
    ConnectError saying what's wrong; the token is never logged."""
    # pylint: disable=import-outside-toplevel
    from .contrib.github.user_token import login as token_login, store
    from .forge_kinds import ForgeApiError
    from .git_gate.host_key import fetch_host_key
    from .manifest.versions import record_or_warn
    from .paths import host_secrets_path
    if bottle.startswith("./"):
        raise ConnectError("GitHub is connected into a host bottle, not a project's")
    try:
        path, _ = bottle_path(bottle or DEFAULT_BOTTLE, host_bottles=host_bottles, cwd=cwd)
        login = token_login(tokens.access)
    except (ManifestError, ForgeApiError) as e:
        raise ConnectError(str(e)) from e
    try:
        host_key = fetch_host_key("github.com", "22")
    except RuntimeError as e:
        raise ConnectError(f"{e}; this host can't reach GitHub over SSH") from e
    key = default_key(GITHUB_URL, login, existing_forges(path))
    entry = ForgeEntry(key=key, kind="github", url=GITHUB_URL, ssh=GITHUB_SSH, account=login,
                       host_key=host_key, app=config.slug)
    store(host_secrets_path(), entry.secret_name, tokens, config.client_id)
    try:
        write_forge(path, entry)
    except ManifestError as e:
        raise ConnectError(f"{path}: {e}") from e
    record_or_warn(f"forge connect: GitHub as {login} via {config.slug} in {path.stem}")
    return GitHubConnected(key=key, bottle=path.stem, path=path, login=login, app=config.slug,
                           install_url=config.install_url, secret_name=entry.secret_name,
                           expiring=bool(tokens.refresh))


def authorize(config: Any, *, on_code: Any, bottle: str = "", cwd: Path,
              host_bottles: Path) -> GitHubConnected:
    """The whole device flow, for the CLI: `on_code(device)` shows the code."""
    # pylint: disable-next=import-outside-toplevel
    from .contrib.github.user_token import start_device, wait_for_token
    from .forge_kinds import ForgeApiError
    try:
        device = start_device(config.client_id)
        on_code(device)
        tokens = wait_for_token(config.client_id, device)
    except ForgeApiError as e:
        raise ConnectError(str(e)) from e
    return finish(tokens, config, bottle=bottle, cwd=cwd, host_bottles=host_bottles)


class _Flow:
    """The console's device flow on this host: one at a time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, object] = {"state": "idle"}
        self.generation = 0


_FLOW = _Flow()


def start_background(*, bottle: str = "", cwd: Path, host_bottles: Path,
                     config: Any = None) -> dict[str, object]:
    """Start a device flow for the console: its `user_code`,
    `verification_uri` and `expires_in`. The host waits for the operator and
    connects; `status` follows it. A new start replaces one in progress."""
    # pylint: disable-next=import-outside-toplevel
    from .contrib.github.user_token import app_config, start_device, wait_for_token
    from .forge_kinds import ForgeApiError
    config = config or app_config()
    try:
        device = start_device(config.client_id)
    except ForgeApiError as e:
        raise ConnectError(str(e)) from e
    with _FLOW.lock:
        _FLOW.generation += 1
        generation = _FLOW.generation
        _FLOW.state = {"state": "waiting", "user_code": device.user_code,
                       "verification_uri": device.verification_uri,
                       "expires_at": int(time.time()) + device.expires_in}

    def superseded() -> bool:
        return _FLOW.generation != generation

    def run() -> None:
        try:
            tokens = wait_for_token(config.client_id, device, cancelled=superseded)
            done = finish(tokens, config, bottle=bottle, cwd=cwd, host_bottles=host_bottles)
            result: dict[str, object] = {"state": "connected", "login": done.login,
                                         "key": done.key, "bottle": done.bottle}
        except (ConnectError, ForgeApiError) as e:
            result = {"state": "error", "error": str(e)}
        except Exception as e:  # pylint: disable=broad-exception-caught  # shown, not raised
            result = {"state": "error", "error": f"connecting failed ({type(e).__name__})"}
        with _FLOW.lock:
            if not superseded():
                _FLOW.state = result

    threading.Thread(target=run, name="github-device-flow", daemon=True).start()
    return {"user_code": device.user_code, "verification_uri": device.verification_uri,
            "expires_in": device.expires_in}


def _forges(host_bottles: Path) -> list[tuple[str, str, dict[str, object]]]:
    """(bottle, forge key, entry) for each GitHub forge the host's bottles
    declare through the app."""
    out: list[tuple[str, str, dict[str, object]]] = []
    for path in sorted(host_bottles.glob("*.md")):
        try:
            forges = existing_forges(path)
        except Exception:  # pylint: disable=broad-exception-caught  # not ours to report
            continue
        for name, entry in forges.items():
            if isinstance(entry, dict) and entry.get("kind") == "github" and entry.get("app"):
                out.append((path.stem, name, entry))
    return out


def status(host_bottles: Path, config: Any = None) -> dict[str, object]:
    """The app this host uses, the console's flow in progress, and who's
    connected with which installations (or why that can't be told)."""
    # pylint: disable=import-outside-toplevel
    from .contrib.github.user_token import app_config, current_token, installations
    from .forge_kinds import ForgeApiError
    from .paths import host_secrets_path
    from .secret_source import FileSecrets
    config = config or app_config()
    with _FLOW.lock:
        flow = dict(_FLOW.state)
    secrets = FileSecrets(host_secrets_path())
    connected: list[dict[str, object]] = []
    for bottle, name, entry in _forges(host_bottles):
        secret_name = _secret_name(name)
        info: dict[str, object] = {"forge": name, "bottle": bottle,
                                   "login": str(entry.get("account", "")),
                                   "app": str(entry.get("app", "")), "installations": []}
        try:
            token = current_token(secret_name, secrets)
            if token is None:
                info["error"] = "its token isn't in the host secrets; connect GitHub again"
            else:
                info["installations"] = installations(token)
        except ForgeApiError as e:
            info["error"] = str(e)
        connected.append(info)
    authorized = [c for c in connected if "error" not in c]
    return {"app": config.slug, "install_url": config.install_url,
            "client_id_set": bool(config.client_id), "flow": flow, "connected": connected,
            "needs_install": bool(authorized) and not any(c["installations"] for c in authorized)}


def _secret_name(forge: str) -> str:
    return ForgeEntry(key=forge, kind="github", url=GITHUB_URL, ssh=GITHUB_SSH, account="",
                      host_key="").secret_name


def connected_token(host_bottles: Path, secret_name: str = "") -> tuple[str, str] | None:
    """(login, token) of the host's GitHub connection through the app, the
    one in `secret_name` when given; None when there's none or its token no
    longer works (renewed first when it's about to expire)."""
    # pylint: disable=import-outside-toplevel
    from .contrib.github.user_token import current_token
    from .forge_kinds import ForgeApiError
    from .paths import host_secrets_path
    from .secret_source import FileSecrets
    secrets = FileSecrets(host_secrets_path())
    for _, name, entry in _forges(host_bottles):
        if secret_name and _secret_name(name) != secret_name:
            continue
        try:
            token = current_token(_secret_name(name), secrets)
        except ForgeApiError:
            continue
        if token:
            return str(entry.get("account", "")), token
    return None


def wait_for_installation(token: str, *, owner: str = "", timeout: float = 600.0,
                          interval: float = 5.0, sleep: Any = time.sleep,
                          clock: Any = time.monotonic) -> list[dict[str, str]] | None:
    """Ask GitHub every `interval` seconds, for up to `timeout`, until the
    app is installed on `owner` (any account or org when none is named): the
    installations then, or None if it never was."""
    # pylint: disable-next=import-outside-toplevel
    from .contrib.github.user_token import installations
    deadline = clock() + timeout
    while True:
        found = installations(token)
        if installed(found, owner):
            return found
        if clock() >= deadline:
            return None
        sleep(interval)


def installed(found: list[dict[str, str]], owner: str = "") -> bool:
    """Whether `found` (installations) covers `owner`, or any account."""
    if not owner:
        return bool(found)
    return owner.lower() in {i.get("account", "").lower() for i in found}


def describe(token: str, inst: dict[str, str]) -> str:
    """An installation as "<account> (all repos)" or "(N selected repos)"."""
    # pylint: disable-next=import-outside-toplevel
    from .contrib.github.user_token import selected_count
    if inst.get("repository_selection") == "all":
        return f"{inst.get('account', '')} (all repos)"
    count = selected_count(token, inst.get("id", "")) if inst.get("id") else None
    plural = "" if count == 1 else "s"
    noun = "selected repos" if count is None else f"{count} selected repo{plural}"
    return f"{inst.get('account', '')} ({noun})"


__all__ = ["DEFAULT_BOTTLE", "GitHubConnected", "authorize", "connected_token", "describe",
           "finish", "installed", "start_background", "status", "wait_for_installation"]
