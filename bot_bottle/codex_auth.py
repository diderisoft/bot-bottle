"""Host Codex auth helpers.

Reads the host's Codex ChatGPT/device-login auth state and returns only
the short-lived access token needed by egress. This module deliberately
does not expose refresh tokens or raw auth payloads.
"""

from __future__ import annotations

import base64
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .log import die
from .util import expand_tilde


def codex_auth_path(host_env: dict[str, str] | None = None) -> Path:
    env = os.environ if host_env is None else host_env
    home = env.get("CODEX_HOME")
    if home:
        return Path(expand_tilde(home)) / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def codex_host_access_token(
    host_env: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> str:
    path = codex_auth_path(host_env)
    if not path.is_file():
        die(
            f"codex host credentials: auth file missing at {path}. "
            "Run `codex login --device-auth` on the host or disable "
            "agent_provider.forward_host_credentials."
        )
    raw = _read_auth_object(path)

    auth_mode = raw.get("auth_mode")
    if not isinstance(auth_mode, str) or auth_mode == "api_key":
        die(
            "codex host credentials: host Codex auth is not user/device "
            "auth. Run `codex login --device-auth` on the host."
        )

    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        die(f"codex host credentials: {path} is missing tokens")
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        die(
            f"codex host credentials: {path} is missing tokens.access_token. "
            "Run `codex login --device-auth` on the host."
        )

    exp = _jwt_exp(access)
    if exp is None:
        die("codex host credentials: tokens.access_token is not a JWT with exp")
    check_now = now or datetime.now(timezone.utc)
    if exp <= check_now:
        die(
            "codex host credentials: host Codex access token is expired. "
            "Run `codex login --device-auth` on the host and restart the bottle."
        )
    return access


def codex_dummy_auth_json(
    host_env: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """Return a non-secret `auth.json` that keeps Codex in the host's
    auth branch while egress owns the real bearer token."""
    path = codex_auth_path(host_env)
    codex_host_access_token(host_env, now=now)
    raw = _read_auth_object(path)
    dummy = _redact_codex_auth(deepcopy(raw), now=now)
    return json.dumps(dummy, indent=2, sort_keys=True) + "\n"


def write_codex_dummy_auth_file(
    path: Path,
    host_env: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(codex_dummy_auth_json(host_env, now=now))
    path.chmod(0o600)


def _read_auth_object(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        die(f"codex host credentials: could not read valid JSON at {path}: {e}")
    if not isinstance(raw, dict):
        die(f"codex host credentials: {path} must contain a JSON object")
    return raw


def _dummy_jwt(now: datetime | None = None) -> str:
    check_now = now or datetime.now(timezone.utc)
    exp = int(check_now.timestamp()) + 3600

    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return (
        f"{enc({'alg': 'none', 'typ': 'JWT'})}."
        f"{enc({'exp': exp, 'sub': 'bot-bottle-placeholder'})}."
        "placeholder"
    )


def _redact_codex_auth(value: object, *, now: datetime | None = None) -> object:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, inner in value.items():
            lower = key.lower()
            if lower == "openai_api_key":
                out[key] = None
            elif lower == "tokens":
                out[key] = _redact_codex_auth(inner, now=now)
            elif lower in {"access_token", "id_token"}:
                out[key] = _dummy_jwt(now)
            elif "token" in lower or "secret" in lower or lower.endswith("_key"):
                out[key] = "bot-bottle-placeholder"
            elif lower in {"account_id", "user_id", "email"}:
                out[key] = "bot-bottle-placeholder"
            else:
                out[key] = _redact_codex_auth(inner, now=now)
        return out
    if isinstance(value, list):
        return [_redact_codex_auth(v, now=now) for v in value]
    return value


def _jwt_exp(token: str) -> datetime | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, timezone.utc)


def _b64url_decode(value: str) -> str:
    padded = value + ("=" * (-len(value) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


__all__ = [
    "codex_auth_path",
    "codex_dummy_auth_json",
    "codex_host_access_token",
    "write_codex_dummy_auth_file",
]
