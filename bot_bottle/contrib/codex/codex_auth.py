"""Host Codex auth helpers.

Reads the host's Codex ChatGPT/device-login auth state and returns only
the short-lived access token needed by egress. This module deliberately
does not expose refresh tokens or raw auth payloads.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ...lib.log import die
from ...util import expand_tilde


def codex_auth_path(host_env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if host_env is None else host_env
    home = env.get("CODEX_HOME")
    if home:
        return Path(expand_tilde(home)) / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def codex_host_access_token(
    host_env: Mapping[str, str] | None = None,
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
    tokens_typed = cast(dict[str, object], tokens)
    access = tokens_typed.get("access_token")
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
    host_env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """Return a non-secret `auth.json` that keeps Codex in the host's
    auth branch while egress owns the real bearer token.

    The dummy access/id tokens carry the *host* token's real `exp` so
    Codex's proactive refresh lifecycle (it refreshes when its local
    access token is at/past expiry) tracks the real token instead of
    firing after an artificial TTL. Codex cannot refresh inside the
    bottle — the refresh token is a placeholder and the OpenAI token
    endpoint is off-route — so a shorter dummy exp would drop Codex to
    the sign-in screen the moment it lapsed, even while egress still
    holds a valid bearer."""
    path = codex_auth_path(host_env)
    access = codex_host_access_token(host_env, now=now)
    raw = _read_auth_object(path)
    host_exp = _jwt_exp(access)
    exp_ts = int(host_exp.timestamp()) if host_exp is not None else None
    dummy = _redact_codex_auth(deepcopy(raw), now=now, exp_ts=exp_ts)
    return json.dumps(dummy, indent=2, sort_keys=True) + "\n"


def write_codex_dummy_auth_file(
    path: Path,
    host_env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(codex_dummy_auth_json(host_env, now=now))
    path.chmod(0o600)


def _read_auth_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        die(f"codex host credentials: could not read valid JSON at {path}: {e}")
    if not isinstance(raw, dict):
        die(f"codex host credentials: {path} must contain a JSON object")
    return cast(dict[str, object], raw)


def _dummy_exp(now: datetime | None, exp_ts: int | None) -> int:
    if exp_ts is not None:
        return exp_ts
    check_now = now or datetime.now(timezone.utc)
    return int(check_now.timestamp()) + 3600


def _dummy_timestamp(now: datetime | None = None) -> str:
    check_now = now or datetime.now(timezone.utc)
    if check_now.tzinfo is None:
        check_now = check_now.replace(tzinfo=timezone.utc)
    check_now = check_now.astimezone(timezone.utc)
    return check_now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dummy_jwt(now: datetime | None = None, *, exp_ts: int | None = None) -> str:
    return _encode_dummy_jwt({
        "exp": _dummy_exp(now, exp_ts),
        "sub": "bot-bottle-placeholder",
    })


def _dummy_jwt_from_host(
    value: object, *, now: datetime | None = None, exp_ts: int | None = None,
) -> str:
    if not isinstance(value, str):
        return _dummy_jwt(now, exp_ts=exp_ts)
    parts = value.split(".")
    if len(parts) < 2:
        return _dummy_jwt(now, exp_ts=exp_ts)
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError):
        return _dummy_jwt(now, exp_ts=exp_ts)
    if not isinstance(payload, dict):
        return _dummy_jwt(now, exp_ts=exp_ts)
    return _encode_dummy_jwt(_redact_jwt_payload(cast(dict[str, object], payload), now=now, exp_ts=exp_ts))


def _encode_dummy_jwt(payload: dict[str, object]) -> str:
    def enc(obj: dict[str, object]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.placeholder"


def _redact_jwt_payload(
    payload: dict[str, object],
    *,
    now: datetime | None = None,
    exp_ts: int | None = None,
) -> dict[str, object]:
    out = _redact_claims(payload)
    if not isinstance(out, dict):
        out = {}
    out_typed: dict[str, object] = cast(dict[str, object], out)
    out_typed["exp"] = _dummy_exp(now, exp_ts)
    out_typed.setdefault("sub", "bot-bottle-placeholder")
    return out_typed


def _redact_claims(value: object) -> object:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, inner in cast(dict[str, object], value).items():
            lower = key.lower()
            if key == "https://api.openai.com/profile":
                out[key] = _redact_profile_claim(inner)
            elif key == "https://api.openai.com/auth":
                out[key] = _redact_auth_claim(inner)
            elif lower == "email":
                out[key] = "bot-bottle@example.invalid"
            elif lower == "email_verified":
                out[key] = True
            elif lower in {"exp", "iat", "nbf", "auth_time", "pwd_auth_time"}:
                out[key] = inner if isinstance(inner, (int, float)) else 0
            elif lower in {"aud", "scp", "amr"}:
                out[key] = inner if isinstance(inner, list) else []
            elif isinstance(inner, bool):
                out[key] = inner
            elif isinstance(inner, dict):
                out[key] = {}
            elif isinstance(inner, list):
                out[key] = []
            else:
                out[key] = "bot-bottle-placeholder"
        return out
    if isinstance(value, list):
        return []
    return "bot-bottle-placeholder"


def _redact_profile_claim(value: object) -> dict[str, object]:
    profile = cast(dict[str, object], value) if isinstance(value, dict) else {}
    return {
        "email": "bot-bottle@example.invalid",
        "email_verified": bool(profile.get("email_verified", True)),
    }


def _redact_auth_claim(value: object) -> dict[str, object]:
    auth = cast(dict[str, object], value) if isinstance(value, dict) else {}
    out: dict[str, object] = {}
    for key, inner in auth.items():
        lower = key.lower()
        if lower == "chatgpt_plan_type" and isinstance(inner, str) and inner:
            out[key] = inner
        elif lower == "chatgpt_account_id" and isinstance(inner, str) and inner:
            # Current Codex uses the selected account id when building
            # ChatGPT requests. Keep that non-secret identifier aligned
            # with the host while egress owns the real bearer token.
            out[key] = inner
        elif lower == "localhost" and isinstance(inner, bool):
            out[key] = inner
        elif isinstance(inner, bool):
            out[key] = inner
        elif isinstance(inner, list):
            out[key] = []
        elif isinstance(inner, dict):
            out[key] = {}
        else:
            out[key] = "bot-bottle-placeholder"
    out.setdefault("chatgpt_plan_type", "unknown")
    out.setdefault("user_id", "bot-bottle-placeholder")
    out.setdefault("chatgpt_user_id", "bot-bottle-placeholder")
    out.setdefault("chatgpt_account_id", "bot-bottle-placeholder")
    return out


def _redact_codex_auth(
    value: object, *, now: datetime | None = None, exp_ts: int | None = None,
) -> object:
    auth = cast(dict[str, object], value) if isinstance(value, dict) else {}
    out: dict[str, object] = {}
    for key, inner in auth.items():
        lower = key.lower()
        if lower == "auth_mode" and isinstance(inner, str) and inner:
            out[key] = inner
        elif lower == "openai_api_key":
            out[key] = None
        elif lower == "last_refresh":
            # Codex parses this as a timestamp on startup. Keep the
            # schema valid without copying host-side session metadata.
            out[key] = _dummy_timestamp(now)
        elif lower == "tokens":
            out[key] = _redact_token_block(inner, now=now, exp_ts=exp_ts)
        else:
            out[key] = _redact_unknown_auth_value(inner)
    return out


def _redact_token_block(
    value: object, *, now: datetime | None = None, exp_ts: int | None = None,
) -> dict[str, object]:
    tokens = cast(dict[str, object], value) if isinstance(value, dict) else {}
    out: dict[str, object] = {}
    for key, inner in tokens.items():
        lower = key.lower()
        if lower in {"access_token", "id_token"}:
            out[key] = _dummy_jwt_from_host(inner, now=now, exp_ts=exp_ts)
        elif lower == "account_id" and isinstance(inner, str) and inner:
            # Current Codex uses this non-secret selected account id
            # while egress owns the real bearer token.
            out[key] = inner
        else:
            out[key] = _redact_unknown_auth_value(inner)
    return out


def _redact_unknown_auth_value(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    if value is None:
        return None
    return "bot-bottle-placeholder"


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
    exp = cast(dict[str, object], payload).get("exp")
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
