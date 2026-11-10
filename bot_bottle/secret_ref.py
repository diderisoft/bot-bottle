"""Where a manifest value comes from: an env entry's value, or a route's
`inspect.auth.token`.

  "?" / "?<message>"  PROMPT: asked for at launch, every launch (hidden input;
                      the message is the prompt)
  "${VAR}"            HOST_ENV: VAR in the launching process's environment
  "secret:NAME"       STORED: NAME in the host secrets store (the secrets file)
  anything else       LITERAL

A literal can't start with "secret:"; that is always a reference. Imports
nothing from the manifest package, so the manifest can use it.
"""

from __future__ import annotations

import re

PROMPT = "prompt"
HOST_ENV = "host_env"
STORED = "stored"
LITERAL = "literal"

STORED_PREFIX = "secret:"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOST_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def kind(raw: str) -> str:
    """PROMPT, HOST_ENV, STORED or LITERAL."""
    if raw.startswith("?"):
        return PROMPT
    if raw.startswith(STORED_PREFIX):
        return STORED
    if _HOST_ENV_RE.match(raw):
        return HOST_ENV
    return LITERAL


def prompt_message(raw: str) -> str:
    """A PROMPT value's message (after the "?"); "" means the default."""
    return raw[1:] if raw.startswith("?") else ""


def host_var(raw: str) -> str:
    """A HOST_ENV value's variable name, else ""."""
    m = _HOST_ENV_RE.match(raw)
    return m.group(1) if m else ""


def stored_name(raw: str) -> str:
    """A STORED value's secret name (unchecked; see `problem`), else ""."""
    return raw[len(STORED_PREFIX):] if raw.startswith(STORED_PREFIX) else ""


def is_name(name: str) -> bool:
    """Whether `name` is env-var shaped (`[A-Za-z_][A-Za-z0-9_]*`)."""
    return bool(_NAME_RE.match(name))


def problem(raw: str) -> str:
    """Why `raw` isn't a usable value, or "" when it is: a "secret:" whose
    name isn't env-var shaped."""
    if kind(raw) == STORED and not is_name(stored_name(raw)):
        return (f"{raw!r} names no valid secret: write secret:NAME, NAME made of "
                "letters, digits and _ (not starting with a digit)")
    return ""


def source_label(raw: str) -> str:
    """Where the value comes from, for people: "asks", "env VAR",
    "secrets file NAME"; "" for a literal."""
    k = kind(raw)
    if k == PROMPT:
        return "asks"
    if k == HOST_ENV:
        return f"env {host_var(raw)}"
    if k == STORED:
        return f"secrets file {stored_name(raw)}"
    return ""


def stored(name: str) -> str:
    """The value that reads `name` from the host secrets store."""
    return f"{STORED_PREFIX}{name}"


__all__ = ["HOST_ENV", "LITERAL", "PROMPT", "STORED", "STORED_PREFIX", "host_var", "is_name",
           "kind", "problem", "prompt_message", "source_label", "stored", "stored_name"]
