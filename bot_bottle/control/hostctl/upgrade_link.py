"""Upgrading bot-bottle on this host from the console (ADR 0022).

The console can ask this host to install a newer bot-bottle release, and
nothing else: no other ref, no code, arguments or environment of its own. Each
upgrade needs a passkey this host trusts (as audit reads do, ADR 0013), signed
over a challenge bound to the version.

- `upgrade_challenge` (`session`, `version`) -> `upgrade_challenge`
  (`challenge`, `rp_id`, `credential_ids`, `expires_in`) or `error`: the
  version must be a release (`X.Y.Z`) newer than this host's.
- `upgrade` (`session`, `assertion`) -> `upgrade_started` (`version`) or
  `error`: the host checks the signature, checks the release's tag exists,
  and runs that release's own `install.sh` in the background. The installer
  restarts the host controller; running bottles are adopted, as on any
  upgrade.
- `upgrade_status` -> `upgrade_status` (`state`: none, running, done or
  failed; `version`, `started`, and the log's last lines when it failed).

A host running bot-bottle from a source checkout refuses: upgrade it there.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ... import resources
from ...engine import install_version
from ...lib import log
from ...paths import bot_bottle_root
from . import audit_trust
from .audit_unlock import UnlockError, challenge_of, verify_passkey
from .webauthn import b64url_encode

REPO = "diderisoft/bot-bottle"
CHALLENGE_SECONDS = 120.0
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
# The installer's own environment knobs, never the console's to set.
_INSTALL_ENV = "BOT_BOTTLE_INSTALL_"
_TAIL_LINES = 30


class UpgradeError(Exception):
    """Why an upgrade was refused; the message reaches the console."""


def _parts(version: str) -> tuple[int, ...] | None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(p) for p in m.groups()) if m else None


def newer(target: str, current: str) -> bool:
    """Whether release `target` (X.Y.Z) is newer than `current`."""
    t, c = _parts(target), _parts(current)
    return bool(t and _VERSION.match(target) and (c is None or t > c))


def tag_exists(version: str, timeout: float = 10.0) -> bool:
    """Whether `v<version>` is a tag of the bot-bottle repo on GitHub."""
    url = f"https://api.github.com/repos/{REPO}/git/ref/tags/v{version}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310: fixed https URL
            return resp.status == 200
    except urllib.error.HTTPError:
        return False


def _dir() -> Path:
    return bot_bottle_root() / "logs"


def _status_file() -> Path:
    return _dir() / "upgrade.json"


def start(version: str) -> dict[str, Any]:
    """Run release `version`'s `install.sh` in its own session, so it outlives
    the host controller it restarts. Its exit status lands next to its log."""
    logs = _dir()
    logs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path, script, code = (logs / f"upgrade-{stamp}.log", logs / f"upgrade-{stamp}.sh",
                              logs / f"upgrade-{stamp}.exit")
    url = f"https://raw.githubusercontent.com/{REPO}/v{version}/install.sh"
    env = {k: v for k, v in os.environ.items() if not k.startswith(_INSTALL_ENV)}
    # The release's own installer, fetched to a file first so a failed fetch
    # isn't an empty script that "succeeds".
    command = ['/bin/sh', '-c',
               'curl -fsSL "$1" -o "$2" && BOT_BOTTLE_INSTALL_REF="$3" sh "$2"; echo $? > "$4"',
               'upgrade', url, str(script), f"v{version}", str(code)]
    with open(log_path, "ab") as out:  # pylint: disable=consider-using-with
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out,  # pylint: disable=consider-using-with
                                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    record = {"version": version, "started": time.time(), "pid": proc.pid,
              "log": str(log_path), "exit": str(code)}
    _status_file().write_text(json.dumps(record), encoding="utf-8")
    return record


def status() -> dict[str, Any]:
    """The last upgrade: none, running, done or failed (with its log's end)."""
    try:
        record = json.loads(_status_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "none"}
    out: dict[str, Any] = {"version": record.get("version", ""),
                           "started": record.get("started", 0)}
    try:
        code = Path(str(record.get("exit", ""))).read_text(encoding="utf-8").strip()
    except OSError:
        code = ""
    if not code:
        out["state"] = "running"
    elif code == "0":
        out["state"] = "done"
    else:
        out["state"] = "failed"
        try:
            text = Path(str(record.get("log", ""))).read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            out["log"] = "\n".join(lines[-_TAIL_LINES:])
        except OSError:
            pass
    return out


class UpgradeLink:
    REQUESTS = frozenset({"upgrade_challenge", "upgrade", "upgrade_status"})

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]], *,
                 db_path: Path | None = None, clock: Callable[[], float] = time.monotonic,
                 starter: Callable[[str], dict[str, Any]] = start,
                 tag_check: Callable[[str], bool] = tag_exists,
                 audit: Callable[[], Any] = lambda: None) -> None:
        self._send = send
        self._audit = audit  # the host's audit journal (HostAudit), if any
        self._db_path = db_path
        self._clock = clock
        self._start = starter
        self._tag_exists = tag_check
        self._lock = threading.Lock()
        # nonce -> (expires, session, version): a signature upgrades only to
        # the version its challenge was issued for.
        self._challenges: dict[str, tuple[float, str, str]] = {}

    def challenge(self, session: str, version: str) -> dict[str, object]:
        if not session:
            raise UpgradeError("no console session")
        if resources.is_source_checkout():
            raise UpgradeError("this host runs bot-bottle from a source checkout; upgrade it there")
        current = install_version()[0]
        if not newer(version, current):
            raise UpgradeError(f"{version or 'that'} isn't a release newer than this host's "
                               f"{current}")
        if status().get("state") == "running":
            raise UpgradeError("an upgrade is already running on this host")
        trust = audit_trust.load(self._db_path)
        if not trust.passkeys:
            raise UpgradeError("this host trusts no passkey yet: run "
                               "`bot-bottle audit passkeys --trust` on it")
        nonce = b64url_encode(secrets.token_bytes(32))
        now = self._clock()
        with self._lock:
            self._challenges = {n: v for n, v in self._challenges.items() if v[0] > now}
            self._challenges[nonce] = (now + CHALLENGE_SECONDS, session, version)
        return {"challenge": nonce, "rp_id": trust.rp_id,
                "credential_ids": [p.id for p in trust.passkeys],
                "expires_in": int(CHALLENGE_SECONDS)}

    def upgrade(self, session: str, assertion: dict[str, object]) -> dict[str, object]:
        response = assertion.get("response")
        client = response.get("clientDataJSON") if isinstance(response, dict) else None
        nonce = challenge_of(str(client or ""))
        now = self._clock()
        with self._lock:
            issued = self._challenges.pop(nonce, None)  # single use, even if it fails below
        if issued is None or issued[0] <= now:
            raise UpgradeError("that challenge expired or was used; try again")
        if issued[1] != session:
            raise UpgradeError("that challenge was issued to another session")
        version, current = issued[2], install_version()[0]
        try:
            credential = verify_passkey(assertion, nonce, self._db_path)
        except UnlockError as e:
            self._journal(version, current, "", str(e))
            raise UpgradeError(str(e)) from e
        if not newer(version, current):
            raise UpgradeError(f"this host already runs {version} or newer")
        if not self._tag_exists(version):
            raise UpgradeError(f"v{version} isn't a bot-bottle release")
        record = self._start(version)
        self._journal(version, current, credential)
        log.info(f"remote: upgrading to {version} (authorized by passkey {credential[:12]}…); "
                 f"log {record.get('log', '')}")
        return {"version": version}

    def _journal(self, version: str, current: str, credential: str, error: str = "") -> None:
        audit = self._audit()
        if audit is not None and hasattr(audit, "host_upgrade"):
            audit.host_upgrade(version=version, current=current, credential_id=credential,
                               error=error)

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        reply: dict[str, Any] = {"type": kind, "request_id": msg.get("request_id")}
        session = str(msg.get("session") or "")
        try:
            if kind == "upgrade_challenge":
                reply.update(await asyncio.to_thread(
                    self.challenge, session, str(msg.get("version") or "")))
            elif kind == "upgrade":
                assertion = msg.get("assertion")
                if not isinstance(assertion, dict):
                    raise UpgradeError("no passkey assertion")
                reply["type"] = "upgrade_started"
                reply.update(await asyncio.to_thread(self.upgrade, session, assertion))
            else:
                reply.update(await asyncio.to_thread(status))
        except UpgradeError as e:
            reply["error"] = str(e)
        except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
            log.warn(f"remote: {kind} failed: {type(e).__name__}: {e}")
            reply["error"] = "the upgrade couldn't start; see the host controller's log"
        await self._send(reply)


__all__ = ["REPO", "UpgradeError", "UpgradeLink", "newer", "start", "status", "tag_exists"]
