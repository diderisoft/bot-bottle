"""Where a manifest value comes from, and whether it is a secret: an env
entry's value, a route's `auth.value`, an agent's `login`.

  "?" / "?<message>"      PROMPT: asked at launch, every launch, input shown
  "${VAR}"                HOST_ENV: VAR from the launching process's env
  "secret:NAME"           STORED: NAME from the host secrets store; secret
  "secret:?<message>"     PROMPT, secret: asked at launch, input hidden
  "secret:${VAR}"         HOST_ENV, secret
  anything else           LITERAL

A secret is push-scanned, masked in transcripts and listed as a secret in the
trust prompt. Every non-literal value, secret or not, is kept out of argv,
files and logs. A literal can't start with "secret:"; that is always a
reference. Imports nothing from the manifest package, so the manifest can use
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PROMPT = "prompt"
HOST_ENV = "host_env"
STORED = "stored"
LITERAL = "literal"

SECRET_PREFIX = "secret:"
STORED_PREFIX = SECRET_PREFIX
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOST_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
# A name that looks like it holds a secret, for the loader's warning.
_SECRET_LOOKING_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PAT|CREDENTIALS?)$", re.IGNORECASE)


@dataclass(frozen=True)
class Ref:
    """A parsed value: its `kind`, whether it is `secret`, and its `name`
    (the store's NAME, or the env's VAR) or prompt `message`."""

    kind: str
    secret: bool = False
    name: str = ""
    message: str = ""


def parse(raw: str) -> Ref:
    """`raw` as a `Ref` (unchecked; see `problem`)."""
    secret = raw.startswith(SECRET_PREFIX)
    rest = raw[len(SECRET_PREFIX):] if secret else raw
    if rest.startswith("?"):
        return Ref(PROMPT, secret, message=rest[1:])
    m = _HOST_ENV_RE.match(rest)
    if m:
        return Ref(HOST_ENV, secret, name=m.group(1))
    if secret:
        return Ref(STORED, True, name=rest)
    return Ref(LITERAL)


def kind(raw: str) -> str:
    """PROMPT, HOST_ENV, STORED or LITERAL."""
    return parse(raw).kind


def is_secret(raw: str) -> bool:
    """Whether `raw` is marked secret (starts with "secret:")."""
    return raw.startswith(SECRET_PREFIX)


def prompt_message(raw: str) -> str:
    """A PROMPT value's message (after the "?"); "" means the default."""
    ref = parse(raw)
    return ref.message if ref.kind == PROMPT else ""


def host_var(raw: str) -> str:
    """A HOST_ENV value's variable name, else ""."""
    ref = parse(raw)
    return ref.name if ref.kind == HOST_ENV else ""


def stored_name(raw: str) -> str:
    """A STORED value's secret name (unchecked; see `problem`), else ""."""
    ref = parse(raw)
    return ref.name if ref.kind == STORED else ""


def is_name(name: str) -> bool:
    """Whether `name` is env-var shaped (`[A-Za-z_][A-Za-z0-9_]*`)."""
    return bool(_NAME_RE.match(name))


def secret_looking(name: str) -> bool:
    """Whether a variable's name says it holds a secret (ends in KEY, TOKEN,
    SECRET, PASSWORD, PASSWD, PAT or CREDENTIAL(S), any case)."""
    return bool(_SECRET_LOOKING_RE.search(name))


def problem(raw: str) -> str:
    """Why `raw` isn't a usable value, or "" when it is: a "secret:" that
    names neither a prompt, an env var nor a valid secret name."""
    ref = parse(raw)
    if ref.kind == STORED and not is_name(ref.name):
        return (f"{raw!r} names no valid secret: write secret:NAME (NAME made of "
                "letters, digits and _, not starting with a digit), secret:? or "
                "secret:${VAR}")
    return ""


def source_label(raw: str) -> str:
    """Where the value comes from, for people: "asks", "env VAR",
    "secrets file NAME"; "" for a literal."""
    ref = parse(raw)
    if ref.kind == PROMPT:
        return "asks"
    if ref.kind == HOST_ENV:
        return f"env {ref.name}"
    if ref.kind == STORED:
        return f"secrets file {ref.name}"
    return ""


def stored(name: str) -> str:
    """The value that reads `name` from the host secrets store."""
    return f"{SECRET_PREFIX}{name}"


__all__ = ["HOST_ENV", "LITERAL", "PROMPT", "Ref", "SECRET_PREFIX", "STORED", "STORED_PREFIX",
           "host_var", "is_name", "is_secret", "kind", "parse", "problem", "prompt_message",
           "secret_looking", "source_label", "stored", "stored_name"]
