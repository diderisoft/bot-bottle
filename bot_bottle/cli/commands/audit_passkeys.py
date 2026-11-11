"""`bot-bottle audit passkeys [--trust]`: the passkeys this host trusts to
unlock reading its audit records from the console (ADR 0013).

With `--trust` it fetches the account's passkeys from the console and trusts
them once you confirm their fingerprints match the ones the console shows
(Settings, Passkeys). `bot-bottle pair` asks the same after pairing. The host
controller keeps them (`AUDIT_PASSKEYS_PATH`).
"""

from __future__ import annotations

import contextlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable

from ...control.hostctl import audit_trust, pairing
from ...control.hostctl.runner import HostControlError
from ...paths import audit_key_path
from ..host_state_client import audit_passkeys, pairing as current_pairing, set_pairing
from ..host_state_client import trust_passkeys


def _ask(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def trust_from_console(console_url: str, access_token: str, *,
                       confirm: Callable[[str], bool] = _ask) -> int:
    """Fetch the account's passkeys and trust them if the user confirms."""
    try:
        fetched = audit_trust.fetch(console_url, access_token)
    except audit_trust.TrustError as e:
        sys.stderr.write(f"bot-bottle: can't fetch passkeys: {e}\n")
        return 1
    if not fetched:
        sys.stderr.write("bot-bottle: the console lists no passkeys for this host's account\n")
        return 1
    sys.stderr.write(
        "Passkeys that may unlock reading this host's audit records from the console:\n"
        + "\n".join(audit_trust.describe(fetched)) + "\n"
        "Check each matches a fingerprint in the console (Settings, Passkeys): a console\n"
        "that doesn't show them could be handing this host its own keys.\n")
    if not confirm("Trust these passkeys? [y/N] "):
        sys.stderr.write("Not trusted. Run `bot-bottle audit passkeys --trust` to try again.\n")
        return 1
    try:
        trust = trust_passkeys(audit_trust.trusted(console_url, fetched))
    except audit_trust.TrustError as e:
        sys.stderr.write(f"bot-bottle: {e}\n")
        return 1
    except HostControlError as e:
        sys.stderr.write(f"bot-bottle: can't trust them: {getattr(e, 'error', '') or e}\n")
        return 1
    sys.stderr.write(f"Trusted {len(trust.passkeys)} passkey(s) for {trust.origin}.\n")
    return 0


def after_pairing(console_url: str, access_token: str) -> None:
    """Pairing's last step: the audit key's whereabouts, and the passkeys."""
    sys.stderr.write(
        f"\nAudit records are encrypted with {audit_key_path()} (created when first needed).\n"
        "Back it up off this host: it's the only way to read them.\n")
    if sys.stdin.isatty():
        trust_from_console(console_url, access_token)
    else:
        sys.stderr.write("Run `bot-bottle audit passkeys --trust` to let your passkey "
                         "unlock reading them from the console.\n")


def fresh_access_token(paired: pairing.Pairing, *, timeout: float = 10.0) -> str:
    """The host's console access token, refreshed first (it's short-lived:
    a stale one gets a 401). The new one is kept as `bot-bottle pair on`
    keeps it; if the refresh fails, the stored token is tried as it is."""
    access, refresh = paired.tokens()
    if refresh:
        req = urllib.request.Request(
            f"{paired.url.rstrip('/')}/api/v1/hosts/token/refresh",
            data=json.dumps({"refresh_token": refresh}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                token = json.load(resp).get("access_token")
        except (urllib.error.URLError, OSError, ValueError, AttributeError):
            token = None
        if isinstance(token, str) and token:
            with contextlib.suppress(HostControlError):
                set_pairing(access_token=token)
            access = token
    return access


def run(trust: bool) -> int:
    if trust:
        try:
            paired = current_pairing()[0].require()
        except (pairing.RemoteError, HostControlError) as e:
            sys.stderr.write(f"bot-bottle: {getattr(e, 'error', '') or e}\n")
            return 1
        if not sys.stdin.isatty():
            sys.stderr.write("bot-bottle: trusting passkeys needs you at a terminal to confirm\n")
            return 1
        return trust_from_console(paired.url, fresh_access_token(paired))
    try:
        current = audit_passkeys()
    except (audit_trust.TrustError, HostControlError) as e:
        sys.stderr.write(f"bot-bottle: {getattr(e, 'error', '') or e}\n")
        return 1
    if not current.passkeys:
        sys.stdout.write("No passkeys trusted; `bot-bottle audit passkeys --trust` adds them.\n")
        return 0
    sys.stdout.write(f"Trusted for {current.origin}:\n"
                     + "\n".join(audit_trust.describe(current.passkeys)) + "\n")
    return 0


__all__ = ["after_pairing", "run", "trust_from_console"]
