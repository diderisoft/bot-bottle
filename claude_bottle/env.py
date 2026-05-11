"""Env resolver. Walks the env entries for one agent and produces a
backend-neutral ResolvedEnv describing how the bottle should receive
each variable:

  - `forwarded` — names whose values have been placed into this
    process's env (from a tty prompt for `secret`, from the matching
    host var for `interpolated`). The backend is expected to pass
    these to the bottle by-name so the value never appears on argv,
    in a file, or in a log line.
  - `literals` — name→value pairs that the manifest carries verbatim.
    The backend serializes these however its launcher accepts env
    (an env-file, an API payload, etc.).

Each env entry is a string. Mode is selected by sentinel prefix:
  "?"           -> secret (prompt at runtime). Bare "?" uses default prompt;
                  "?<message>" uses <message> as the prompt body.
  "${HOST_VAR}" -> interpolated from $HOST_VAR in the host process env
  any other str -> literal (the string is the value verbatim)

Critical rules:
  - NEVER echo, log, or interpolate the value of a secret or
    interpolated env var. Both are treated as potentially sensitive:
    nothing about their value (other than presence) ever lands on
    disk, in a log line, or on argv.
  - Errors mention only the variable NAME, never any portion of the value.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from dataclasses import dataclass, field

from .log import die
from .manifest import Manifest

_INTERPOLATED_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class ResolvedEnv:
    """Backend-neutral env resolution result.

    `forwarded` names have already been exported into os.environ by
    resolve_env; the backend forwards by-name. `literals` carry their
    values verbatim and are serialized by the backend."""

    forwarded: list[str] = field(default_factory=list)
    literals: dict[str, str] = field(default_factory=dict)


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
            tty = open("/dev/tty", "r+")
        except OSError:
            die(
                f"cannot prompt for secret '{name}': no tty available. "
                f"Run from an interactive shell."
            )
        prompt = (
            f"{prompt_body} (input hidden): "
            if prompt_body
            else f"claude-bottle: secret value for {name} (input hidden): "
        )
        value = getpass.getpass(prompt, stream=tty)
        tty.close()
    else:
        prompt = (
            f"{prompt_body} (input hidden): "
            if prompt_body
            else f"claude-bottle: secret value for {name} (input hidden): "
        )
        value = getpass.getpass(prompt)
    if not value:
        die(f"empty value provided for secret '{name}'. Re-run and supply a value.")
    return value


def resolve_env(manifest: Manifest, agent: str) -> ResolvedEnv:
    """Iterate the agent's env entries:
      - secret:       always prompt; export into this process; mark forwarded
      - interpolated: copy host value; export under target name; mark forwarded
      - literal:      include in the literals map verbatim
    """
    forwarded: list[str] = []
    literals: dict[str, str] = {}
    bottle = manifest.bottle_for(agent)
    for name, raw in bottle.env.items():
        if not name:
            continue
        kind = env_entry_kind(raw)
        if kind == "secret":
            prompt_body = env_entry_secret_prompt(raw)
            value = _read_secret_silent(name, prompt_body)
            os.environ[name] = value
            forwarded.append(name)
        elif kind == "interpolated":
            host_var = env_entry_interpolated_from(raw)
            host_value = os.environ.get(host_var, "")
            if not host_value:
                die(
                    f"env entry {name} is interpolated from ${host_var}, "
                    f"but ${host_var} is unset or empty in the host environment."
                )
            os.environ[name] = host_value
            forwarded.append(name)
        else:  # literal
            literals[name] = raw
    return ResolvedEnv(forwarded=forwarded, literals=literals)
