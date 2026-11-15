"""What the bottle architect runs on: the `bottle-architect.connection`
setting, on the architect host (`control.hostctl.architect_setting` says
which host that is).

  auto                   claude, then codex: the first ready here (the default)
  claude, codex          that harness, with the host's login
  pi:<provider>/<model>  pi on that model, with the host's pi config and key
                         for that provider

A connection that isn't ready here isn't swapped for another (only `auto`
picks): the suggestion takes the rules path and says why (`resolve`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..lib.architect_values import (
    AUTO, CONNECTION_SETTING, MAX_LENGTH, check_connection as check,
    valid_connection as valid,
)

PI_PREFIX = "pi:"
# What `auto` tries, in order.
AUTO_HARNESSES = ("claude", "codex")


@dataclass(frozen=True)
class Resolved:
    """What `provider` (a connection value) runs on here: the harness and the agent's
    `agent_provider` block, or why it can't (`reason`)."""

    provider: str
    harness: str = ""
    agent_provider: dict[str, object] = field(default_factory=dict)
    reason: str = ""

    @property
    def ready(self) -> bool:
        return not self.reason

    @property
    def label(self) -> str:
        """`auto → claude`, or the value itself."""
        return f"{self.provider} → {self.harness}" if self.provider == AUTO and self.harness \
            else self.provider


def resolve(provider: str) -> Resolved:
    """Connection `provider` on this host. Reads the harnesses' logins, the host's pi
    config and which secrets exist, never a secret's value."""
    if not valid(provider):
        return Resolved(provider, reason=f"{provider!r} isn't a {CONNECTION_SETTING} value")
    if provider.startswith(PI_PREFIX):
        return _pi(provider)
    # pylint: disable-next=import-outside-toplevel
    from ..agent_provider import harness_ready
    for harness in AUTO_HARNESSES if provider == AUTO else (provider,):
        if harness_ready(harness):
            return Resolved(provider, harness, {"harness": harness, "login": "host"})
    if provider == AUTO:
        return Resolved(provider, reason="neither claude nor codex is installed and logged "
                                         "in on this host")
    return Resolved(provider, reason=f"{provider} isn't installed and logged in on this host")


def _pi(provider: str) -> Resolved:
    """`pi:<provider>/<model>` with the host pi config's entry for that
    provider, and its key in the host secrets (pi's starter does the same)."""
    # pylint: disable=import-outside-toplevel
    from ..contrib.pi.starter import has_secret, local_starter_settings, provider_model
    from ..paths import host_secrets_path
    from ..secret_ref import stored
    name, _, model = provider[len(PI_PREFIX):].partition("/")
    found = provider_model({}, name, model)
    if isinstance(found, str):
        return Resolved(provider, reason=found)
    if not has_secret(found.key_name, {}):
        where = ("the key is written in ~/.pi/agent/models.json; `bot-bottle setup` copies it"
                 if found.literal_key else f"add it to {host_secrets_path()}")
        return Resolved(provider, reason=f"no {found.key_name} in the host secrets ({where})")
    settings = local_starter_settings(found)
    key = str(settings.pop("api_key_env"))
    return Resolved(provider, "pi", {"harness": "pi", "login": {"api_key": stored(key)},
                                     "settings": settings})


__all__ = ["AUTO", "AUTO_HARNESSES", "CONNECTION_SETTING", "MAX_LENGTH", "Resolved", "check",
           "resolve", "valid"]
