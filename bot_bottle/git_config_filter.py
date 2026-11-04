"""The repo config a bottle gets: the host's `.git/config`, cut to local settings.

A copied workspace brings its `.git` along, and the host's repo config can hold
credentials (a token in a remote URL, an `http.extraHeader`) or point at host
paths and commands. The bottle gets only an allowlist of settings git needs
locally; remote URLs lose their user info. git in the bottle reaches upstreams
through git-gate, with git-gate's credentials, so nothing it needs is lost
(ADR 0016). The host's own config is never changed.

Parsed and written with `git config --file`, not by hand, so quoting and
subsections round-trip exactly.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

# Settings without a subsection that are safe and local.
_PLAIN = {
    "core": None,  # every core.* except _CORE_DROP
    "extensions": None,
    "user": frozenset({"name", "email"}),
    "pull": None,
    "push": frozenset({"default", "autosetupremote", "followtags"}),
    "fetch": frozenset({"prune", "prunetags", "recursesubmodules"}),
    "init": frozenset({"defaultbranch"}),
    "merge": frozenset({"ff", "conflictstyle"}),
    "diff": frozenset({"algorithm", "renames", "mnemonicprefix", "submodule"}),
    "lfs": None,
    "alias": None,
}
# core.* naming host paths or host commands.
_CORE_DROP = frozenset({
    "sshcommand", "askpass", "gitproxy", "hookspath", "editor", "pager",
    "fsmonitor", "excludesfile", "attributesfile", "worktree", "sharedrepository",
})
# Settings with a subsection: section -> allowed variable names.
_SUBSECTION = {
    "branch": frozenset({"remote", "merge", "rebase", "pushremote", "description"}),
    "remote": frozenset({"url", "pushurl", "fetch", "push", "tagopt", "prune", "mirror",
                         "skipdefaultupdate", "promisor", "partialclonefilter"}),
    "submodule": frozenset({"url", "path", "active", "branch", "update",
                            "fetchrecursesubmodules", "shallow"}),
    "url": frozenset({"insteadof", "pushinsteadof"}),
}
_URL_VALUES = frozenset({"remote.url", "remote.pushurl", "submodule.url", "lfs.url"})
_USERINFO = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@]*)@")
# An auth header or a token handed to a command: `Authorization: …`, `Bearer …`,
# `Basic <base64>`, `token <hex>`.
_AUTH_HEADER = re.compile(
    r"authorization\s*:|\bbearer\s+\S{8,}|\bbasic\s+[A-Za-z0-9+/=]{8,}|\btoken\s+[A-Za-z0-9_]{16,}",
    re.IGNORECASE)


def _split(key: str) -> tuple[str, str | None, str]:
    """`section.sub.section.name` -> (section, subsection or None, name)."""
    section, _, rest = key.partition(".")
    sub, _, name = rest.rpartition(".")
    return section.lower(), (sub or None), name.lower()


def _strip_userinfo(url: str) -> str:
    """A URL without its credentials. http(s) loses all user info (a token is
    often the user name); other schemes keep a bare user name (`ssh://git@`)."""
    m = _USERINFO.match(url)
    if not m:
        return url
    scheme, info = m.group(1), m.group(2)
    if scheme.lower().startswith(("http", "ftp")) or ":" in info:
        user = "" if scheme.lower().startswith(("http", "ftp")) else info.split(":", 1)[0] + "@"
        return scheme + user + url[m.end():]
    return url


def _has_credential(text: str) -> bool:
    return _strip_userinfo(text) != text or bool(_AUTH_HEADER.search(text))


def _label(section: str, sub: str | None, name: str) -> str:
    """A key as it may be shown: never the subsection when it holds a credential."""
    if sub is None:
        return f"{section}.{name}"
    shown = "<url with credentials>" if _has_credential(sub) else sub
    return f"{section}.{shown}.{name}"


def _decide(key: str, value: str) -> tuple[str | None, str | None]:  # pylint: disable=too-many-return-statements
    """(value to keep or None, credential label or None) for one entry."""
    section, sub, name = _split(key)
    label = _label(section, sub, name)
    if section in ("credential", "http", "include", "includeif"):
        return None, (label if section in ("credential", "http") else None)
    if sub is None:
        allowed = _PLAIN.get(section, frozenset())
        if section == "core" and name in _CORE_DROP:
            return None, None
        if allowed is not None and name not in allowed:
            return None, None
    else:
        if name not in _SUBSECTION.get(section, frozenset()):
            return None, (label if _has_credential(sub) else None)
        if section == "url" and _has_credential(sub):
            return None, label
        if section == "submodule" and name == "update" and value.startswith("!"):
            return None, None
    if f"{section}.{name}" in _URL_VALUES:
        stripped = _strip_userinfo(value)
        if stripped != value:
            return stripped, f"{label} user info"
    if _has_credential(value):
        return None, label
    return value, None


def _entries(config: Path) -> list[tuple[str, str]]:
    out = subprocess.run(["git", "config", "--file", str(config), "--list", "--null"],
                         capture_output=True, check=True).stdout.decode("utf-8", "surrogateescape")
    pairs = []
    for item in out.split("\0"):
        if item:
            key, _, value = item.partition("\n")
            pairs.append((key, value))
    return pairs


def filtered_config(config: Path) -> tuple[bytes, list[str]]:
    """The bottle's copy of repo config file `config`, and the labels of the
    credential settings left out (never their values). An unreadable config
    gives an empty one: git then uses its defaults."""
    try:
        entries = _entries(config)
    except (OSError, subprocess.CalledProcessError):
        return b"", ["(unreadable repo config)"]
    dropped: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bot-bottle-gitconfig.") as tmp:
        out = Path(tmp) / "config"
        out.touch()
        for key, value in entries:
            kept, credential = _decide(key, value)
            if credential and credential not in dropped:
                dropped.append(credential)
            if kept is not None:
                subprocess.run(["git", "config", "--file", str(out), "--add", key, kept],
                               capture_output=True, check=True)
        return out.read_bytes(), dropped


def is_repo_config(rel: str) -> bool:
    """Whether workspace path `rel` is a repo config git reads: `.git/config`,
    a submodule's under `.git/modules/`, or a worktree's `config.worktree`."""
    parts = PurePosixPath(rel).parts
    return (len(parts) >= 2 and parts[0] == ".git"
            and parts[-1] in ("config", "config.worktree")
            and (len(parts) == 2 or parts[1] in ("modules", "worktrees")))


__all__ = ["filtered_config", "is_repo_config"]
