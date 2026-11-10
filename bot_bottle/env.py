"""Resolve one agent's env entries into a backend-neutral `ResolvedEnv`.

Entry kinds, by value (`secret_ref`); a "secret:" prefix marks a secret:
  "?" / "?<message>"     prompt: asked at launch, every launch, input shown
  "secret:?<message>"    the same, input hidden
  "${VAR}"               host_env: VAR from the launching process's env
  "secret:${VAR}"        the same, a secret
  "secret:NAME"          stored: NAME from the host secrets store. Missing,
                         an interactive launch asks once (hidden) and offers
                         to save it.
  anything else          literal

With no one to ask, a prompt falls back to a saved value of the entry's name,
with a warning; a `${VAR}` found only in the secrets file is used, with a
warning. Values of every kind but literal never reach argv, a file or a log;
errors name only the variable.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import secret_ref
from .errors import MissingEnvVarError, SecretsFileError
from .lib.log import die, info, warn
from .lib.prompt import current as current_prompter
from .manifest import Manifest
from .secret_source import FileSecrets, SecretSource, tiers
from .util import read_tty_line

# The kinds `env_entry_kind` returns.
PROMPT = secret_ref.PROMPT
HOST_ENV = secret_ref.HOST_ENV
STORED = secret_ref.STORED
LITERAL = secret_ref.LITERAL


def is_secret_entry(raw: str) -> bool:
    """Whether an env entry is marked secret ("secret:...")."""
    return secret_ref.is_secret(raw)


@dataclass(frozen=True)
class ResolvedEnv:
    """`forwarded`: values the backend injects into the launched process's env,
    never argv or a file (excluded from repr). `literals`: values serialized
    however the backend's launcher takes env."""

    forwarded: dict[str, str] = field(default_factory=dict[str, str], repr=False)
    literals: dict[str, str] = field(default_factory=dict[str, str])


def env_entry_kind(raw: str) -> str:
    """PROMPT ("?"), HOST_ENV ("${VAR}"), STORED ("secret:NAME") or LITERAL."""
    return secret_ref.kind(raw)


def env_entry_prompt_message(raw: str) -> str:
    """A prompt entry's message (after the '?'); empty means the default."""
    return secret_ref.prompt_message(raw)


def env_entry_host_var(raw: str) -> str:
    """A host_env entry's variable name."""
    return secret_ref.host_var(raw)


def env_entry_stored_name(raw: str) -> str:
    """A stored entry's secret name."""
    return secret_ref.stored_name(raw)


def _read_shown(name: str, prompt_body: str) -> str:
    """Read a (not secret) value from this context's prompter or the
    controlling tty, echoed."""
    asker = current_prompter()
    if asker is not None:
        value = asker.ask(prompt_body or f"value for {name}")
    else:
        sys.stderr.write(f"{prompt_body}: " if prompt_body
                         else f"bot-bottle: value for {name}: ")
        sys.stderr.flush()
        value = read_tty_line()
    if not value:
        die(f"empty value provided for '{name}'. Re-run and supply a value.")
    return value


def _ask(name: str, raw: str) -> str:
    """Ask for prompt entry `name` (manifest value `raw`): hidden when it is
    marked secret, shown otherwise."""
    message = env_entry_prompt_message(raw)
    if secret_ref.is_secret(raw):
        return _read_secret_silent(name, message)
    return _read_shown(name, message)


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


def _confirm_save(name: str) -> bool:
    """Ask whether to save `name` to the secrets file; yes by default."""
    question = f"Save {name} to the secrets file?"
    asker = current_prompter()
    if asker is not None:
        return asker.confirm(question, default=True)
    sys.stderr.write(f"bot-bottle: {question} [Y/n] ")
    sys.stderr.flush()
    return read_tty_line().strip() in ("", "y", "Y", "yes", "YES")


def _peek(source: SecretSource, name: str) -> str:
    """`name`'s value in `source`, or "" (also when the store can't be read)."""
    try:
        return source.get(name) or ""
    except SecretsFileError:
        return ""


def _hint_saved(name: str, env: SecretSource, store: SecretSource) -> None:
    """Before asking for a prompt entry: say how to use a saved value
    instead."""
    if _peek(store, name):
        info(f"(a saved {name} exists; write {name}: secret:{name} to use it without asking)")
    elif env is not store and _peek(env, name):
        info(f"({name} is set in your environment; write {name}: ${{{name}}} to use it "
             "without asking)")


def _ask_stored(name: str, entry: str, store: SecretSource) -> str:
    """Ask for stored secret `name` (for env entry `entry`) the store lacks,
    then offer to save it there."""
    value = _read_secret_silent(
        name, f"bot-bottle: {name} (for {entry}) isn't in the secrets file; enter it")
    if isinstance(store, FileSecrets) and _confirm_save(name):
        try:
            store.save(name, value)
            info(f"saved {name} in {store.path}")
        except (OSError, SecretsFileError) as e:
            warn(f"couldn't save {name}: {e}; using it for this launch only")
    return value


def ask_env(manifest: Manifest, secrets: SecretSource,
            skip: Mapping[str, str] | None = None) -> dict[str, str]:
    """Ask the person launching for what the agent's env entries ask for:
    every `?`/`secret:?` entry (each launch, even with a saved value), and
    each `secret:NAME` the store lacks (once per NAME, offering to save it).
    Returns the typed values by entry name. Entries in `skip` are answered."""
    env, store, _ = tiers(secrets)
    answers: dict[str, str] = {}
    typed: dict[str, str] = {}  # by stored name
    for name, raw in manifest.bottle.env.items():
        if not name or (skip and name in skip):
            continue
        kind = env_entry_kind(raw)
        if kind == PROMPT:
            _hint_saved(name, env, store)
            answers[name] = _ask(name, raw)
        elif kind == STORED:
            ref = env_entry_stored_name(raw)
            if ref not in typed:
                if store.get(ref):
                    continue
                typed[ref] = _ask_stored(ref, name, store)
            answers[name] = typed[ref]
    return answers


def _unasked(name: str, raw: str, env: SecretSource, store: SecretSource) -> str:
    """A prompt entry's value with no one to ask: a saved one, with a
    warning."""
    asks = "secret:?" if secret_ref.is_secret(raw) else "?"
    value = store.get(name)
    if value:
        warn(f"{name}: {asks} asks at every launch; there's no one to ask here, so the "
             f"saved value is used. Write {name}: secret:{name} to say so.")
        return value
    value = env.get(name) if env is not store else None
    if value:
        mark = "secret:" if secret_ref.is_secret(raw) else ""
        warn(f"{name}: {asks} asks at every launch; there's no one to ask here, so the "
             f"value in the environment is used. Write {name}: {mark}${{{name}}} to say so.")
        return value
    raise MissingEnvVarError(
        name, f"env entry {name} is a secret with no value and prompting is not allowed "
        f"(`?` asks at launch, and there's no one to ask here). Write {name}: "
        f"secret:{name} and add {name} to the secrets file, or launch from a terminal.")


def _from_host_env(name: str, raw: str, env: SecretSource, store: SecretSource) -> str:
    """A `${VAR}` entry's value: VAR from the launching environment, else (with
    a warning) from the secrets file."""
    var = env_entry_host_var(raw)
    value = env.get(var)
    if value:
        return value
    value = store.get(var) if env is not store else None
    if value:
        warn(f"{name}: {raw} read {var} from the secrets file; write secret:{var} "
             "for that.")
        return value
    raise MissingEnvVarError(
        var,
        f"env entry {name} is interpolated from ${var}, "
        f"but ${var} is unset or empty in the host environment "
        f"and the secrets file.",
    )


def _from_store(name: str, ref: str, store: SecretSource, answers: Mapping[str, str]) -> str:
    """A `secret:NAME` entry's value: NAME from the store, else what was typed
    for it at launch."""
    value = store.get(ref) or answers.get(name)
    if value:
        return value
    path = getattr(store, "path", None)
    where = f" ({path})" if path else ""
    raise MissingEnvVarError(
        ref, f"env entry {name} reads secret:{ref}, but {ref} isn't in the secrets "
        f"file{where}. Add a line {ref}=<value> to it (owner-only, chmod 600), or "
        f"launch from a terminal to be asked for it.")


def resolve_env(
    manifest: Manifest, secrets: SecretSource, *, prompt: bool = True,
) -> ResolvedEnv:
    """Resolve the agent's env entries. With `prompt`, `ask_env` asks first;
    without it, a prompt entry falls back to a saved value and a missing one
    raises MissingEnvVarError (as does any missing reference)."""
    env, store, answers = tiers(secrets)
    if prompt:
        answers = {**answers, **ask_env(manifest, secrets, skip=answers)}
    forwarded: dict[str, str] = {}
    literals: dict[str, str] = {}
    for name, raw in manifest.bottle.env.items():
        if not name:
            continue
        kind = env_entry_kind(raw)
        if kind == PROMPT:
            forwarded[name] = answers.get(name) or _unasked(name, raw, env, store)
        elif kind == HOST_ENV:
            forwarded[name] = _from_host_env(name, raw, env, store)
        elif kind == STORED:
            forwarded[name] = _from_store(name, env_entry_stored_name(raw), store, answers)
        else:  # literal
            literals[name] = raw
    return ResolvedEnv(forwarded=forwarded, literals=literals)


def prompt_secret(name: str, raw: str) -> str:
    """Ask for prompt entry `name` (manifest value `raw`): hidden when it is
    marked secret."""
    return _ask(name, raw)
