"""The forges bot-bottle can connect, named the same by the supervise server
(the gateway image carries `contract`, not the host's forge code) and the
host. Each API kind's API lives host-side in `contrib/<kind>/forge_api.py`;
an `ssh` forge is a plain SSH git server, with no API (ADR 0023)."""

from __future__ import annotations

# The kinds with an API: it mints keys (or lends the token), lists repos and
# creates them.
API_KINDS = ("gitea", "github")
# Every kind a bottle may declare. An `ssh` forge's keys are files this host
# holds, added with `bot-bottle forge key add`.
FORGE_KINDS = (*API_KINDS, "ssh")
FORGE_LABELS = {"gitea": "Gitea", "github": "GitHub", "ssh": "SSH server"}
# Recognized as GitHub without asking.
GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
# Public hosts whose forge isn't one of API_KINDS: an agent's request for
# one is refused before it's queued, without asking the host.
UNSUPPORTED_HOSTS = frozenset({
    "gitlab.com", "bitbucket.org", "dev.azure.com", "ssh.dev.azure.com",
    "sr.ht", "git.sr.ht", "sourceforge.net", "gitee.com",
})


def supported() -> str:
    """`Gitea, GitHub`: the kinds an agent can ask for. An SSH server is
    connected on the host, with its keys."""
    return ", ".join(FORGE_LABELS[k] for k in API_KINDS)


def known_unsupported(host: str) -> bool:
    """Whether `host` is a public forge of a kind bot-bottle can't connect."""
    return host.lower().removeprefix("www.") in UNSUPPORTED_HOSTS


def unsupported_forge(host: str) -> str:
    """What an agent is told when it asks for a forge of another kind."""
    return f"{host} isn't a forge bot-bottle can connect; supported: {supported()}"


__all__ = ["API_KINDS", "FORGE_KINDS", "FORGE_LABELS", "GITHUB_HOSTS", "UNSUPPORTED_HOSTS",
           "known_unsupported", "supported", "unsupported_forge"]
