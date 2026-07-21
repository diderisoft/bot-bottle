"""bb login — register this host with a bot-bottle console.

Opens a device-authorization flow against the target console, waits for the
operator to approve, then writes access and refresh tokens to
~/.bot-bottle/console.json (or $BOT_BOTTLE_ROOT/console.json).

Usage:
  bb login [--console-url URL] [--label LABEL]

Flags:
  --console-url URL   Target console URL (overrides BB_CONSOLE_URL env var)
  --label LABEL       Host label shown in the console (default: hostname)
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..paths import bot_bottle_root

_CONSOLE_URL_ENV = "BB_CONSOLE_URL"
_POLL_SLEEP = 2  # seconds between polls; matches console's poll_interval default


def _usage() -> None:
    sys.stderr.write(
        "usage: bb login [--console-url URL] [--label LABEL]\n"
        "\n"
        "Options:\n"
        "  --console-url URL  Console base URL (or BB_CONSOLE_URL env var)\n"
        "  --label LABEL      Host label shown in the console (default: hostname)\n"
    )


def _flag(argv: list[str], name: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(f"{name}="):
            return arg[len(name) + 1:]
    return None


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get(url: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def _save_credentials(
    console_url: str, host_id: str, access_token: str, refresh_token: str
) -> Path:
    path = bot_bottle_root() / "console.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(
            {
                "url": console_url,
                "host_id": host_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
            },
            indent=2,
        )
        + "\n"
    )
    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, prefix=".console-")
    tmp = Path(tmp_path_str)
    try:
        tmp.chmod(0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def cmd_login(argv: list[str]) -> int:
    if "--help" in argv or "-h" in argv:
        _usage()
        return 0

    console_url = _flag(argv, "--console-url") or os.environ.get(_CONSOLE_URL_ENV)
    if not console_url:
        sys.stderr.write(
            "bb login: --console-url or BB_CONSOLE_URL is required\n"
        )
        return 1
    console_url = console_url.rstrip("/")

    label = _flag(argv, "--label") or socket.gethostname()

    try:
        resp = _post(f"{console_url}/api/v1/hosts/authorize", {"label": label})
    except Exception as exc:
        sys.stderr.write(f"bb login: failed to start authorization: {exc}\n")
        return 1

    device_code = resp["device_code"]
    user_code = resp["user_code"]
    expires_in = resp.get("expires_in", 300)
    poll_sleep = max(1, min(int(resp.get("poll_interval", _POLL_SLEEP)), 60))

    sys.stderr.write(
        f"\nOpen this URL in your browser to authorize this host:\n\n"
        f"  {console_url}/authorize?code={user_code}\n\n"
        f"Waiting for approval"
    )

    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        sys.stderr.write(".")
        sys.stderr.flush()
        time.sleep(poll_sleep)

        try:
            code, result = _get(
                f"{console_url}/api/v1/hosts/authorize/{device_code}"
            )
        except Exception:
            continue

        if code == 410:
            break

        st = result.get("status")
        if st == "approved":
            sys.stderr.write("\n\nApproved.\n")
            path = _save_credentials(
                console_url,
                result["host_id"],
                result["access_token"],
                result["refresh_token"],
            )
            sys.stderr.write(f"Credentials saved to {path}\n")
            return 0
        if st == "denied":
            sys.stderr.write("\n\nDenied by operator.\n")
            return 1

    sys.stderr.write("\n\nAuthorization timed out.\n")
    return 1
