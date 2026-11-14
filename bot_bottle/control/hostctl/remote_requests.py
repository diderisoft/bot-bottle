"""What a console may ask this host to start, checked before anything runs:
a launch's prompt, bottles, label and library commit, and a resume of a bottle
this host ran before (ADR 0007: the host decides; the console only names
things)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ...contract.hostctl import LIBRARY_COMMIT, LaunchRequest
from ...lib import log

# Bounds on a console launch.
MAX_PROMPT_CHARS = 20_000
MAX_BOTTLES = 8
MAX_LABEL_CHARS = 128
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_BAD_COMMIT = "library_commit must be a 40-character commit sha"


def library_commit(msg: dict[str, Any]) -> str | None:
    """The library commit a launch or resume names (PRD 0131): "" when it
    names none, None when it isn't a commit sha."""
    commit = msg.get("library_commit", "")
    if commit in ("", None):
        return ""
    return commit if isinstance(commit, str) and LIBRARY_COMMIT.fullmatch(commit) else None


def local_agents() -> list[str]:
    """The agents a remote launch may name: those under `~/.bot-bottle/`."""
    # pylint: disable=import-outside-toplevel
    from ...manifest import ManifestError, ManifestIndex
    try:
        # From $HOME, so only home agents.
        return ManifestIndex.resolve(str(Path.home()), missing_ok=True).all_agent_names
    except (ManifestError, OSError) as e:
        log.warn(f"remote: can't list local agents: {e}")
        return []


def agent_allowed(name: object, agents: Callable[[], Iterable[str]], commit: str) -> bool:
    """Whether a console launch may name the agent `name`: one of this host's,
    or with a library commit, any name (the launch resolves it at that
    commit, and refuses one it lacks)."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return False
    return bool(commit) or name in agents()


def launch_extras(msg: dict[str, Any]) -> dict[str, Any] | str:
    """A console launch's prompt, bottles, label and library commit, as
    `LaunchRequest` fields, or why they're refused."""
    prompt = msg.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        return f"the prompt must be text of at most {MAX_PROMPT_CHARS} characters"
    bottles = msg.get("bottles", [])
    if (not isinstance(bottles, list) or len(bottles) > MAX_BOTTLES
            or not all(isinstance(b, str) and _NAME.match(b) for b in bottles)):
        return f"bottles must be a list of at most {MAX_BOTTLES} bottle names"
    label = msg.get("label", "")
    if not isinstance(label, str) or len(label) > MAX_LABEL_CHARS:
        return f"the label must be text of at most {MAX_LABEL_CHARS} characters"
    commit = library_commit(msg)
    if commit is None:
        return _BAD_COMMIT
    return {"prompt": prompt, "bottles": tuple(bottles), "label": label,
            "library_commit": commit}


def resume_request(agent_id: str, bottle: object, *, agents: Callable[[], Iterable[str]],
                   running: Callable[[str], bool], commit: object = "") -> LaunchRequest | str:
    """Relaunch the stopped bottle `bottle` under its own identity and state,
    from what was recorded when it started (as `bot-bottle resume` does), or
    why it can't be. `commit`: the library commit the console names."""
    # pylint: disable-next=import-outside-toplevel
    from ...bottle_state import read_metadata
    if not isinstance(bottle, str) or not _IDENTITY.match(bottle):
        return "resume needs the bottle it ran as"
    library = library_commit({"library_commit": commit})
    if library is None:
        return _BAD_COMMIT
    if running(bottle):
        return f"{bottle} is already running as another bottled agent"
    metadata = read_metadata(bottle)
    if metadata is None:
        return f"this host has no saved state for {bottle}; start it fresh instead"
    if not agent_allowed(metadata.agent_name, agents, library):
        return f"this host no longer has the agent {metadata.agent_name!r}"
    # The console click is the launch confirmation.
    return LaunchRequest(agent=metadata.agent_name, bottles=tuple(metadata.bottle_names),
                         identity=bottle, label=metadata.label, cwd=metadata.cwd,
                         copy_cwd=metadata.copy_cwd, assume_yes=True, headless=False,
                         agent_id=agent_id, library_commit=library)


__all__ = ["MAX_BOTTLES", "MAX_LABEL_CHARS", "MAX_PROMPT_CHARS", "agent_allowed",
           "launch_extras", "library_commit", "local_agents", "resume_request"]
