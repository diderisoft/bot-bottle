"""Host Claude auth helpers.

Reads the host's Claude Code credentials and returns only the access
token needed by egress. Does not expose refresh tokens or raw payloads.

Credential storage by platform:
  Linux  — ~/.claude/.credentials.json
  macOS  — macOS Keychain, service "Claude Code-credentials"
            (file path is tried first; Keychain is the fallback)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from ...lib.log import die


_KEYCHAIN_SERVICE = "Claude Code-credentials"


def claude_auth_path(host_env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if host_env is None else host_env
    home = env.get("HOME")
    if home:
        return Path(home) / ".claude" / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def _read_keychain() -> dict[str, object] | None:
    """Try the macOS Keychain. Returns parsed JSON dict or None."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        raw = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def claude_host_access_token(
    host_env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> str:
    path = claude_auth_path(host_env)
    raw: dict[str, object] | None = None

    if path.is_file():
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            die(f"claude host credentials: could not read valid JSON at {path}: {e}")
        if not isinstance(raw, dict):
            die(f"claude host credentials: {path} must contain a JSON object")
    else:
        raw = _read_keychain()
        if raw is None:
            die(
                f"claude host credentials: auth file missing at {path} and "
                f"macOS Keychain lookup for '{_KEYCHAIN_SERVICE}' failed. "
                "Run `claude login` on the host or disable "
                "agent_provider.forward_host_credentials."
            )

    oauth = raw.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        die(
            "claude host credentials: claudeAiOauth is missing from credentials. "
            "Run `claude login` on the host or disable "
            "agent_provider.forward_host_credentials."
        )

    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        die(
            "claude host credentials: claudeAiOauth.accessToken is missing or empty. "
            "Run `claude login` on the host and restart the bottle."
        )

    # expiresAt is in milliseconds
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)):
        check_now = now or datetime.now(timezone.utc)
        exp_dt = datetime.fromtimestamp(float(expires_at) / 1000.0, timezone.utc)
        if exp_dt <= check_now:
            die(
                "claude host credentials: host Claude access token is expired. "
                "Run `claude login` on the host and restart the bottle."
            )

    return access_token


__all__ = [
    "claude_auth_path",
    "claude_host_access_token",
]
