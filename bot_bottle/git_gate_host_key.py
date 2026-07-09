"""Preflight host-key population for git-gate upstreams (issue #333).

When a git-gate repo entry lacks a `host_key`, this module either:
- headless: dies with a clear config error.
- interactive: fetches the key via ssh-keyscan, prompts the operator to
  confirm, and optionally persists it to the bottle config file on disk.

Public entry point: `preflight_host_keys(manifest, headless=..., home_md=...)`.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import sys
from pathlib import Path

from .log import die, info
from .manifest import Manifest, ManifestBottle
from .manifest_git import ManifestGitEntry


def fetch_host_key(host: str, port: str) -> str:
    """Return an SSH ed25519 public key for `host`:`port` via ssh-keyscan.

    Returns the key in `<type> <base64-data>` format (the host prefix is
    stripped so the result can be stored in `host_key` and later formatted
    into a known_hosts line by `git_gate_known_hosts_line`).

    Raises `RuntimeError` on subprocess failure, timeout, or no ed25519
    result. Uses only the Python stdlib (subprocess)."""
    args = ["ssh-keyscan", "-t", "ed25519"]
    if port and port != "22":
        args += ["-p", port]
    args.append(host)
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=15,
        )
    except OSError as e:
        raise RuntimeError(
            f"ssh-keyscan: could not launch for {host}:{port}: {e}"
        ) from e
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ssh-keyscan timed out for {host}:{port}")

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # known_hosts format: "[host]:port type data"  or  "host type data"
        # Strip the host/port prefix; keep "type data".
        parts = line.split(None, 2)
        if len(parts) == 3:
            return f"{parts[1]} {parts[2]}"

    raise RuntimeError(
        f"ssh-keyscan returned no ed25519 key for {host}:{port}."
        + (f" stderr: {result.stderr.strip()!r}" if result.stderr.strip() else "")
    )


# ---------------------------------------------------------------------------
# Frontmatter text-level editing
# ---------------------------------------------------------------------------


def _insert_host_key_in_frontmatter(fm_text: str, repo_name: str, host_key: str) -> str:
    """Append `host_key: <key>` to the named repo entry in raw frontmatter text.

    Locates the `<repo_name>:` block-mapping key, detects its child
    indentation from the sibling keys already present, and inserts the
    new line after the last child.  Returns the original text if the
    repo entry is not found or already has a `host_key:` line."""
    lines = fm_text.splitlines(keepends=True)

    repo_pat = re.compile(r"^(\s+)" + re.escape(repo_name) + r"\s*:\s*(\r?\n)?$")
    repo_line_idx: int | None = None
    repo_indent: int | None = None
    for i, line in enumerate(lines):
        if repo_pat.match(line):
            repo_indent = len(line) - len(line.lstrip())
            repo_line_idx = i
            break

    if repo_line_idx is None or repo_indent is None:
        return fm_text

    child_indent: int | None = None
    last_child_idx = repo_line_idx

    for i in range(repo_line_idx + 1, len(lines)):
        raw = lines[i].rstrip("\r\n")
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent > repo_indent:
            child_indent = indent
            last_child_idx = i
        else:
            break

    if child_indent is None:
        child_indent = repo_indent + 2

    block = "".join(lines[repo_line_idx + 1 : last_child_idx + 1])
    if re.search(r"^\s*host_key\s*:", block, re.MULTILINE):
        return fm_text

    new_line = " " * child_indent + f"host_key: {host_key}\n"
    lines.insert(last_child_idx + 1, new_line)
    return "".join(lines)


def _update_frontmatter_in_file(file_text: str, repo_name: str, host_key: str) -> str:
    """Apply `_insert_host_key_in_frontmatter` to the frontmatter section of
    a Markdown file, leaving the body unchanged.  Returns the original text
    when no change is needed."""
    lines = file_text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return file_text

    close_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            close_idx = i
            break
    if close_idx is None:
        return file_text

    fm_text = "".join(lines[1:close_idx])
    body_text = "".join(lines[close_idx:])
    updated_fm = _insert_host_key_in_frontmatter(fm_text, repo_name, host_key)
    if updated_fm == fm_text:
        return file_text
    return "---\n" + updated_fm + body_text


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

    from .yaml_subset import YamlSubsetError, parse_frontmatter

    for path in sorted(bottles_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            fm, _body = parse_frontmatter(text)
        except (OSError, YamlSubsetError):
            continue

        git_gate = fm.get("git-gate")
        if not isinstance(git_gate, dict):
            continue
        repos = git_gate.get("repos")
        if not isinstance(repos, dict):
            continue
        repo = repos.get(repo_name)
        if not isinstance(repo, dict):
            continue
        if repo.get("host_key"):
            continue

        updated = _update_frontmatter_in_file(text, repo_name, host_key)
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
    "_insert_host_key_in_frontmatter",
    "_find_and_update_bottle_file",
    "_update_frontmatter_in_file",
]
