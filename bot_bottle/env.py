"""Resolve one agent's env entries into a backend-neutral `ResolvedEnv`.

Entry modes, by prefix:
  "?"           secret: looked up in the SecretSource, else prompted
                ("?<message>" sets the prompt)
  "${HOST_VAR}" interpolated from the SecretSource
  anything else literal

Secret and interpolated values never reach argv, a file or a log; errors name
only the variable.
"""

from __future__ import annotations

import getpass
import re
import sys
from dataclasses import dataclass, field

from .errors import MissingEnvVarError
from .lib.log import die
from .lib.prompt import current as current_prompter
from .manifest import Manifest
from .secret_source import SecretSource

_INTERPOLATED_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class ResolvedEnv:
    """`forwarded`: values the backend injects into the launched process's env,
    never argv or a file (excluded from repr). `literals`: values serialized
    however the backend's launcher takes env."""

    forwarded: dict[str, str] = field(default_factory=dict[str, str], repr=False)
    literals: dict[str, str] = field(default_factory=dict[str, str])


def env_entry_kind(raw: str) -> str:
    """Returns 'secret', 'interpolated', or 'literal'."""
    if raw.startswith("?"):
        return "secret"
    if _INTERPOLATED_RE.match(raw):
        return "interpolated"
    return "literal"


def env_entry_secret_prompt(raw: str) -> str:
    """A secret entry's prompt (after the '?'); empty means the default."""
    if raw.startswith("?"):
        return raw[1:]
    return ""


def env_entry_interpolated_from(raw: str) -> str:
    """An interpolated entry's host var name."""
    m = _INTERPOLATED_RE.match(raw)
    if not m:
        return ""
    return m.group(1)


def _read_secret_silent(name: str, prompt_body: str) -> str:
    """Read a secret from the controlling tty without echoing (or from this
    context's prompter, hidden there)."""
    asker = current_prompter()
    if asker is not None:
        value = asker.ask(prompt_body or f"secret value for {name}", secret=True)
    elif not (sys.stdin.isatty() or sys.stderr.isatty()):
        # /dev/tty, so this works when stdin is a pipe.
        try:
            tty = open("/dev/tty", "r+", encoding="utf-8")
        except OSError:
            die(
                f"cannot prompt for secret '{name}': no tty available. "
                f"Run from an interactive shell."
            )
        prompt = (
            f"{prompt_body} (input hidden): "
            if prompt_body
            else f"bot-bottle: secret value for {name} (input hidden): "
        )
        value = getpass.getpass(prompt, stream=tty)
        tty.close()
    else:
        prompt = (
            f"{prompt_body} (input hidden): "
            if prompt_body
            else f"bot-bottle: secret value for {name} (input hidden): "
        )
        value = getpass.getpass(prompt)
    if not value:
        die(f"empty value provided for secret '{name}'. Re-run and supply a value.")
    return value


def resolve_env(
    manifest: Manifest, secrets: SecretSource, *, prompt: bool = True,
) -> ResolvedEnv:
    """Resolve the agent's env entries. A secret missing from `secrets` is
    prompted for (if `prompt`) or raises MissingEnvVarError."""
    forwarded: dict[str, str] = {}
    literals: dict[str, str] = {}
    bottle = manifest.bottle
    for name, raw in bottle.env.items():
        if not name:
            continue
        kind = env_entry_kind(raw)
        if kind == "secret":
            value = secrets.get(name)
            if not value:
                if not prompt:
                    raise MissingEnvVarError(
                        name, f"env entry {name} is a secret with no value and "
                        "prompting is not allowed.")
                value = _read_secret_silent(name, env_entry_secret_prompt(raw))
            forwarded[name] = value
        elif kind == "interpolated":
            host_var = env_entry_interpolated_from(raw)
            host_value = secrets.get(host_var)
            if not host_value:
                raise MissingEnvVarError(
                    host_var,
                    f"env entry {name} is interpolated from ${host_var}, "
                    f"but ${host_var} is unset or empty in the host environment "
                    f"and the secrets file.",
                )
            forwarded[name] = host_value
        else:  # literal
            literals[name] = raw
    return ResolvedEnv(forwarded=forwarded, literals=literals)


def prompt_secret(name: str, raw: str) -> str:
    """Ask the tty for secret entry `name` (manifest value `raw`) without
    echoing."""
    return _read_secret_silent(name, env_entry_secret_prompt(raw))
