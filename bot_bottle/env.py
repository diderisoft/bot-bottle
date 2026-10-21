"""Env resolver. Walks the env entries for one agent and produces a
backend-neutral ResolvedEnv describing how the bottle should receive
each variable:

  - `forwarded` — name→value pairs the backend must inject into the
    launched process's environment directly (e.g. via
    `subprocess.run(env=...)`), so the value never appears on argv,
    in a file, or in a log line. The dict is excluded from the
    dataclass-generated repr so accidental logging doesn't leak it.
  - `literals` — name→value pairs that the manifest carries verbatim.
    The backend serializes these however its launcher accepts env
    (an env-file, an API payload, etc.).

Each env entry is a string. Mode is selected by sentinel prefix:
  "?"           -> secret: the entry's name looked up in the SecretSource,
                  else prompted at runtime. Bare "?" uses default prompt;
                  "?<message>" uses <message> as the prompt body.
  "${HOST_VAR}" -> interpolated: HOST_VAR looked up in the SecretSource
  any other str -> literal (the string is the value verbatim)

Critical rules:
  - NEVER echo, log, or interpolate the value of a secret or
    interpolated env var. Both are treated as potentially sensitive:
    nothing about their value (other than presence) ever lands on
    disk, in a log line, or on argv.
  - resolve_env reads no environment; values come from the SecretSource
    and the backend passes them to its launcher in a child env dict.
  - Errors mention only the variable NAME, never any portion of the value.
"""

from __future__ import annotations

import getpass
import re
import sys
from dataclasses import dataclass, field

from .errors import MissingEnvVarError
from .lib.log import die
from .manifest import Manifest
from .secret_source import SecretSource

_INTERPOLATED_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class ResolvedEnv:
    """Backend-neutral env resolution result.

    `forwarded` maps name→value for entries whose values must not land
    on argv or in a file; backends inject them into the launched
    process's environment directly. `repr=False` so a stray `repr(...)`
    or log of the dataclass doesn't dump secret values.

    `literals` carry values verbatim and are serialized by the backend
    however its launcher accepts non-secret env."""

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
    """For a secret entry, the prompt body (after the leading '?').
    Empty for bare '?', meaning use default."""
    if raw.startswith("?"):
        return raw[1:]
    return ""


def env_entry_interpolated_from(raw: str) -> str:
    """For an interpolated entry, the host var name between '${' and '}'."""
    m = _INTERPOLATED_RE.match(raw)
    if not m:
        return ""
    return m.group(1)


def _read_secret_silent(name: str, prompt_body: str) -> str:
    """Read a secret value from the controlling tty without echoing.
    The "(input hidden): " tail is always appended; manifest authors
    write only the message text."""
    if not (sys.stdin.isatty() or sys.stderr.isatty()):
        # Fall back to /dev/tty so this still works when stdin is a pipe.
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
    """Iterate the agent's env entries:
      - secret:       the entry's name from `secrets`; when absent or empty,
                      prompt (if `prompt`) or raise MissingEnvVarError
      - interpolated: $HOST_VAR from `secrets`; carry value in forwarded
      - literal:      include in the literals map verbatim"""
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
    """Ask the controlling tty for secret entry `name` (whose manifest value
    is `raw`, e.g. `?Your token`), without echoing."""
    return _read_secret_silent(name, env_entry_secret_prompt(raw))
