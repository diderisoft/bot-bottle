"""The kinds of forge `bot-bottle forge connect` takes (Gitea, GitHub): how
one is recognized from its URL, the token it wants, and checking that token.
Each kind's API is `contrib/<kind>/forge_api.py`, imported when used."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from urllib.parse import urlparse

from .contract.forges import FORGE_KINDS, FORGE_LABELS, GITHUB_HOSTS, known_unsupported

# Recognizing a forge asks it over the network; an agent's request waits on it.
DETECT_TIMEOUT_SECS = 5.0


class ForgeApiError(Exception):
    """The forge couldn't be reached or refused the token."""


@dataclass(frozen=True)
class TokenCheck:
    """Who a token is, the SSH address of its repos (`ssh://user@host[:port]`),
    and how many it can add deploy keys to (it must be admin on a repo)."""

    account: str
    ssh: str
    admin_repos: int
    repos: int


def ssh_base(ssh_url: str) -> str:
    """A repo's SSH URL without the repo: `ssh://git@h:2222/o/r.git` or
    `git@h:o/r.git` -> `ssh://git@h:2222` / `ssh://git@h`."""
    if ssh_url.startswith("ssh://"):
        rest = ssh_url[len("ssh://"):]
        return "ssh://" + rest.split("/", 1)[0]
    user_host = ssh_url.split(":", 1)[0]
    return f"ssh://{user_host}"


def forge_api(kind: str) -> ModuleType:
    """`contrib.<kind>.forge_api`: `token_page(url)`, `TOKEN_HELP`,
    `check_token(url, token)`. ValueError for a kind bot-bottle doesn't know."""
    if kind not in FORGE_KINDS:
        raise ValueError(f"unknown forge kind {kind!r}")
    return importlib.import_module(f"bot_bottle.contrib.{kind}.forge_api")


def detect(url: str, timeout: float = DETECT_TIMEOUT_SECS) -> str:
    """The kind of forge at `url`, or "" when it's none bot-bottle connects.
    github.com is known without asking; another host is asked, briefly."""
    host = (urlparse(url).hostname or "").lower()
    if host in GITHUB_HOSTS:
        return "github"
    if not host or known_unsupported(host):
        return ""
    for kind in FORGE_KINDS:
        if forge_api(kind).answers(url, timeout):
            return kind
    return ""


def label(kind: str) -> str:
    return FORGE_LABELS.get(kind, kind)


__all__ = ["DETECT_TIMEOUT_SECS", "ForgeApiError", "TokenCheck", "detect", "forge_api",
           "label", "ssh_base"]
