"""Env resolver. Walks the env entries for one agent and produces:

  1. The list of `docker run` arg fragments needed to forward each var.
     Both `secret` and `interpolated` entries become `-e NAME` (no
     `=value`) so Docker inherits the value from this process env
     without rendering it on argv or persisting it to disk.
     Only `literal` entries are written to a host-disk env-file.
  2. The export side-effect of populating this process's env with
     secret values prompted from the user, and with interpolated
     values copied from the matching host var, so `-e NAME` actually
     has something to inherit.

Each env entry is a string. Mode is selected by sentinel prefix:
  "?"           → secret (prompt at runtime). Bare "?" uses default prompt;
                  "?<message>" uses <message> as the prompt body.
  "${HOST_VAR}" → interpolated from $HOST_VAR in the host process env
  any other str → literal (the string is the value verbatim)

Critical rules:
  - NEVER echo, log, or interpolate the value of a secret or
    interpolated env var. Both are treated as potentially sensitive:
    nothing about their value (other than presence) ever lands on
    disk, in a log line, or on argv.
  - The env-file written for literals lives under mktemp -d with mode
    600, removed by the caller's cleanup.
  - Errors mention only the variable NAME, never any portion of the value.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from pathlib import Path

from .log import die
from .manifest import Manifest

_INTERPOLATED_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


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


def env_resolve(
    manifest: Manifest,
    agent: str,
    env_file: Path,
    out_args: Path,
) -> None:
    """Iterate the agent's env entries:
      - secret:       always prompt; export into this process; append `-e NAME` to out_args
      - interpolated: copy host value; export under target name; append `-e NAME`
      - literal:      append `NAME=VALUE` to env_file
    """
    bottle = manifest.bottle_for(agent)
    for name, raw in bottle.env.items():
        if not name:
            continue
        kind = env_entry_kind(raw)
        if kind == "secret":
            prompt_body = env_entry_secret_prompt(raw)
            value = _read_secret_silent(name, prompt_body)
            os.environ[name] = value
            with out_args.open("a") as f:
                f.write(f"-e\n{name}\n")
        elif kind == "interpolated":
            host_var = env_entry_interpolated_from(raw)
            host_value = os.environ.get(host_var, "")
            if not host_value:
                die(
                    f"env entry {name} is interpolated from ${host_var}, "
                    f"but ${host_var} is unset or empty in the host environment."
                )
            os.environ[name] = host_value
            with out_args.open("a") as f:
                f.write(f"-e\n{name}\n")
        else:  # literal
            if "\n" in raw:
                die(
                    f"env entry {name} (literal) contains a newline; "
                    f"docker --env-file cannot represent multi-line values."
                )
            with env_file.open("a") as f:
                f.write(f"{name}={raw}\n")
