"""Fill in missing git-gate upstream host keys before launch: headless dies;
interactive fetches with ssh-keyscan and asks, for this launch only. A repo's
host key is its forge connection's pin (`forge connect` pins one), and a
bottle file holds no long-form repo to save one in (ADR 0023). Entry point:
`preflight_host_keys`.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Mapping

from ..lib.log import die, info
from ..lib.prompt import current as current_prompter
from ..manifest import Manifest


# Preferred key types, most secure first.
_KEY_TYPE_PREFERENCE = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)


def fetch_host_key(host: str, port: str) -> str:
    """An SSH public key for `host`:`port` from ssh-keyscan, as `<type>
    <data>`. Prefers ed25519, then ecdsa, then rsa, else the first returned.
    Raises `RuntimeError` on failure, timeout or no result."""
    args = ["ssh-keyscan"]
    if port and port != "22":
        args += ["-p", port]
    args.append(host)
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=15, check=False,
        )
    except OSError as e:
        raise RuntimeError(
            f"ssh-keyscan: could not launch for {host}:{port}: {e}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ssh-keyscan timed out for {host}:{port}") from e

    # known_hosts lines are "[host]:port type data" or "host type data"; strip
    # the host and keep "type data" by type.
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] not in found:
            found[parts[1]] = f"{parts[1]} {parts[2]}"

    for preferred in _KEY_TYPE_PREFERENCE:
        if preferred in found:
            return found[preferred]
    if found:
        return next(iter(found.values()))

    # ssh-keyscan repeats the same error once per key type it tries.
    errors = dict.fromkeys(l.strip() for l in result.stderr.splitlines() if l.strip())
    raise RuntimeError(
        f"ssh-keyscan got no host key from {host}:{port}"
        + (f" ({'; '.join(errors)})" if errors else "")
    )


# --- Interactive prompt ---


def prompt_tty(message: str) -> str:
    """Write `message` to stderr and read a line from /dev/tty (or stdin)."""
    sys.stderr.write(message)
    sys.stderr.flush()
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")


def _yes(question: str, context: list[str] | None = None) -> bool:
    """Ask `question` (after `context`): this context's prompter, else the tty."""
    asker = current_prompter()
    if asker is not None:
        return asker.confirm(question, context=context or ())
    if context:
        sys.stderr.write("\n" + "\n".join(context) + "\n\n")
    return prompt_tty(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# --- Public API ---


def with_host_keys(manifest: Manifest, keys: Mapping[str, str]) -> Manifest:
    """`manifest` with host keys from `keys` (repo name -> key) filled in where
    missing."""
    bottle = manifest.bottle
    entries = tuple(
        dataclasses.replace(e, KnownHostKey=keys[e.Name])
        if not e.KnownHostKey and keys.get(e.Name) else e
        for e in bottle.git
    )
    if entries == tuple(bottle.git):
        return manifest
    return dataclasses.replace(manifest, bottle=dataclasses.replace(bottle, git=entries))


def preflight_host_keys(manifest: Manifest, *, headless: bool) -> Manifest:
    """Ensure every git-gate repo has a `host_key`. Missing ones: headless dies
    naming them; interactive fetches, shows and asks (abort dies), applying it
    for this launch. Returns the manifest."""
    bottle = manifest.bottle
    missing = [e for e in bottle.git if not e.KnownHostKey]
    if not missing:
        return manifest

    if headless:
        names = ", ".join(repr(e.Name) for e in missing)
        die(
            f"git-gate: no host key pinned for repo(s) {names}. Connect each one's forge "
            f"again (`bot-bottle forge connect`), which pins its host key."
        )

    updated_entries = list(bottle.git)
    for entry in missing:
        host = entry.UpstreamHost
        port = entry.UpstreamPort
        label = f"git_gate.repos[{entry.Name!r}]"

        info(f"{label}: no host key pinned; fetching from {host}:{port} for this launch "
             f"(connect its forge again to pin one)")
        try:
            key = fetch_host_key(host, port)
        except RuntimeError as e:
            die(f"git-gate: {label}: {e}")

        if not _yes("Is this host key correct?",
                    [f"git-gate: host key for {label}:", f"  {key}"]):
            die(f"git-gate: {label}: host key not confirmed; aborting launch")

        idx = next(i for i, e in enumerate(updated_entries) if e.Name == entry.Name)
        updated_entries[idx] = dataclasses.replace(entry, KnownHostKey=key)

    updated_bottle = dataclasses.replace(bottle, git=tuple(updated_entries))
    return dataclasses.replace(manifest, bottle=updated_bottle)


__all__ = [
    "fetch_host_key",
    "preflight_host_keys",
    "with_host_keys",
    "prompt_tty",
]
