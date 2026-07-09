"""Preflight host-key population for git-gate upstreams (issue #333).

When a git-gate repo entry lacks a `host_key`, this module either:
- headless: dies with a clear config error.
- interactive: fetches the key via ssh-keyscan, prompts the operator to
  confirm, and optionally persists it to the bottle config file on disk.

Public entry point: `preflight_host_keys(manifest, headless=..., home_md=...)`.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from typing import cast

from .log import die, info
from .manifest import Manifest
from .yaml_subset import YamlSubsetError, parse_frontmatter, serialize_yaml_subset


# Preferred key types, most secure first.
_KEY_TYPE_PREFERENCE = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)


def fetch_host_key(host: str, port: str) -> str:
    """Return an SSH public key for `host`:`port` via ssh-keyscan.

    Returns the key in `<type> <base64-data>` format (the host prefix is
    stripped so the result can be stored in `host_key` and later formatted
    into a known_hosts line by `git_gate_known_hosts_line`).

    Prefers ed25519 > ecdsa > rsa; falls back to the first key type
    returned if none of the preferred types are present.

    Raises `RuntimeError` on subprocess failure, timeout, or no result.
    Uses only the Python stdlib (subprocess)."""
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

    # known_hosts format: "[host]:port type data"  or  "host type data"
    # Strip the host/port prefix; collect "type -> type data" by type.
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

    raise RuntimeError(
        f"ssh-keyscan returned no host key for {host}:{port}."
        + (f" stderr: {result.stderr.strip()!r}" if result.stderr.strip() else "")
    )


# ---------------------------------------------------------------------------
# Frontmatter editing
# ---------------------------------------------------------------------------


def _add_host_key_to_frontmatter(file_text: str, repo_name: str, host_key: str) -> str:
    """Return an updated copy of `file_text` with `host_key` set on the
    named repo entry in the YAML frontmatter.

    Parses the frontmatter into a dict, sets the key, and re-serializes.
    Returns the original text unchanged when: the file has no frontmatter,
    the git-gate.repos.<repo_name> entry is absent or already has a
    host_key, or the frontmatter cannot be parsed."""
    try:
        fm, body = parse_frontmatter(file_text)
    except YamlSubsetError:
        return file_text

    git_gate = fm.get("git-gate")
    if not isinstance(git_gate, dict):
        return file_text
    repos = git_gate.get("repos")
    if not isinstance(repos, dict):
        return file_text
    repo = repos.get(repo_name)
    if not isinstance(repo, dict):
        return file_text
    if repo.get("host_key"):
        return file_text

    cast(dict[str, object], repo)["host_key"] = host_key
    return f"---\n{serialize_yaml_subset(fm)}---\n{body}"


def _find_and_update_bottle_file(
    bottles_dir: Path, repo_name: str, host_key: str,
) -> bool:
    """Scan `bottles_dir/*.md` for the first file that directly declares
    `repo_name` without a `host_key`, update it, and return True.

    Walks files in sorted order so the result is deterministic.  Does NOT
    follow `extends:` chains — only files that explicitly declare the repo
    entry are candidates.  Returns False when no such file is found."""
    if not bottles_dir.is_dir():
        return False

    for path in sorted(bottles_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        updated = _add_host_key_to_frontmatter(text, repo_name, host_key)
        if updated == text:
            continue
        path.write_text(updated, encoding="utf-8")
        info(f"wrote host_key for {repo_name!r} to {path}")
        return True

    return False


# ---------------------------------------------------------------------------
# Interactive prompt helper
# ---------------------------------------------------------------------------


def _prompt_tty(message: str) -> str:
    """Write `message` to stderr and read a line from /dev/tty (or stdin)."""
    sys.stderr.write(message)
    sys.stderr.flush()
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preflight_host_keys(
    manifest: Manifest,
    *,
    headless: bool,
    home_md: Path | None,
) -> Manifest:
    """Ensure every git-gate repo entry has a `host_key` configured.

    For entries whose `KnownHostKey` is empty:
    - headless: calls `die()` with a clear message naming the repos.
    - interactive: fetches the key via ssh-keyscan, shows it to the
      operator, and requests confirmation.  If accepted, optionally
      persists it to the bottle config file on disk; the key is always
      applied in memory for this launch regardless of the persistence
      choice.  Aborted confirmation calls `die()`.

    Returns a (possibly updated) Manifest.  If all entries already have
    host keys the original manifest is returned unchanged."""
    bottle = manifest.bottle
    missing = [e for e in bottle.git if not e.KnownHostKey]
    if not missing:
        return manifest

    if headless:
        names = ", ".join(repr(e.Name) for e in missing)
        die(
            f"git-gate: no host_key configured for repo(s) {names}. "
            f"Add host_key to each bottle git-gate.repos entry, or run "
            f"interactively once to have it fetched and saved automatically."
        )

    bottles_dir = (home_md / "bottles") if home_md is not None else None
    updated_entries = list(bottle.git)

    for entry in missing:
        host = entry.UpstreamHost
        port = entry.UpstreamPort
        label = f"git-gate.repos[{entry.Name!r}]"

        info(f"{label}: no host_key configured; fetching from {host}:{port}")
        try:
            key = fetch_host_key(host, port)
        except RuntimeError as e:
            die(f"git-gate: {label}: {e}")

        sys.stderr.write(f"\ngit-gate: host key for {label}:\n  {key}\n\n")
        confirm = _prompt_tty("Is this host key correct? [y/N] ")
        if confirm.strip().lower() not in ("y", "yes"):
            die(f"git-gate: {label}: host key not confirmed; aborting launch")

        if bottles_dir is not None:
            save = _prompt_tty(
                f"Save host_key for {entry.Name!r} to the bottle config file? [y/N] "
            )
            if save.strip().lower() in ("y", "yes"):
                ok = _find_and_update_bottle_file(bottles_dir, entry.Name, key)
                if not ok:
                    sys.stderr.write(
                        f"git-gate: {label}: bottle file not found or not writable; "
                        f"host_key kept in memory for this session only\n"
                    )

        idx = next(i for i, e in enumerate(updated_entries) if e.Name == entry.Name)
        updated_entries[idx] = dataclasses.replace(entry, KnownHostKey=key)

    updated_bottle = dataclasses.replace(bottle, git=tuple(updated_entries))
    return dataclasses.replace(manifest, bottle=updated_bottle)


__all__ = [
    "fetch_host_key",
    "preflight_host_keys",
    "_add_host_key_to_frontmatter",
    "_find_and_update_bottle_file",
]
