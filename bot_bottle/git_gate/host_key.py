"""Fill in missing git-gate upstream host keys before launch: headless dies;
interactive fetches with ssh-keyscan, asks, and can save it to the bottle file.
Entry point: `preflight_host_keys`.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from ..lib.log import die, info
from ..manifest import Manifest
from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter, serialize_yaml_subset


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


# --- Frontmatter editing ---


def add_host_key_to_frontmatter(file_text: str, repo_name: str, host_key: str) -> str:
    """`file_text` with `host_key` set on `git-gate.repos.<repo_name>`.
    Unchanged when there's no frontmatter, no such entry, an existing key, or a
    parse failure."""
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


def find_repo_bottle_file(bottles_dir: Path, repo_name: str) -> Path | None:
    """The first `bottles_dir/*.md` declaring `repo_name` without a `host_key`,
    or None."""
    if not bottles_dir.is_dir():
        return None
    for path in sorted(bottles_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
        except (OSError, UnicodeDecodeError, YamlSubsetError):
            continue
        git_gate = fm.get("git-gate")
        if not isinstance(git_gate, dict):
            continue
        repos = git_gate.get("repos")
        if not isinstance(repos, dict):
            continue
        repo = repos.get(repo_name)
        if not isinstance(repo, dict) or repo.get("host_key"):
            continue
        return path
    return None


def find_and_update_bottle_file(
    bottles_dir: Path, repo_name: str, host_key: str,
) -> bool:
    """Write `host_key` into that file. False when none is found or the write
    fails."""
    path = find_repo_bottle_file(bottles_dir, repo_name)
    if path is None:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    updated = add_host_key_to_frontmatter(text, repo_name, host_key)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    info(f"wrote host_key for {repo_name!r} to {path}")
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.versions import record_or_warn
    record_or_warn(f"pin the host key for {repo_name}", root=bottles_dir.parent)
    return True


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


def preflight_host_keys(
    manifest: Manifest,
    *,
    headless: bool,
    home_md: Path | None,
) -> Manifest:
    """Ensure every git-gate repo has a `host_key`. Missing ones: headless dies
    naming them; interactive fetches, shows and asks (abort dies), always
    applying it for this launch and optionally saving it. Returns the manifest."""
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
        confirm = prompt_tty("Is this host key correct? [y/N] ")
        if confirm.strip().lower() not in ("y", "yes"):
            die(f"git-gate: {label}: host key not confirmed; aborting launch")

        if bottles_dir is not None:
            target_file = find_repo_bottle_file(bottles_dir, entry.Name)
            if target_file is not None:
                save = prompt_tty(
                    f"Save host_key for {entry.Name!r} to {target_file}? [y/N] "
                )
                if save.strip().lower() in ("y", "yes"):
                    ok = find_and_update_bottle_file(bottles_dir, entry.Name, key)
                    if not ok:
                        sys.stderr.write(
                            f"git-gate: {label}: could not write to {target_file}; "
                            f"host_key kept in memory for this session only\n"
                        )
            else:
                sys.stderr.write(
                    f"git-gate: {label}: no bottle config file found for "
                    f"{entry.Name!r}; host_key kept in memory for this session only\n"
                )

        idx = next(i for i, e in enumerate(updated_entries) if e.Name == entry.Name)
        updated_entries[idx] = dataclasses.replace(entry, KnownHostKey=key)

    updated_bottle = dataclasses.replace(bottle, git=tuple(updated_entries))
    return dataclasses.replace(manifest, bottle=updated_bottle)


__all__ = [
    "fetch_host_key",
    "preflight_host_keys",
    "with_host_keys",
    "add_host_key_to_frontmatter",
    "find_repo_bottle_file",
    "find_and_update_bottle_file",
    "prompt_tty",
]
