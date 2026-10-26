"""What a console may ask this host to start, checked before anything runs:
a launch's prompt, bottles and label, and a resume of a bottle this host ran
before (ADR 0007: the host decides; the console only names things)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from ...contract.hostctl import LaunchRequest

# Bounds on a console launch.
MAX_PROMPT_CHARS = 20_000
MAX_BOTTLES = 8
MAX_LABEL_CHARS = 128
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def launch_extras(msg: dict[str, Any]) -> tuple[str, tuple[str, ...], str] | str:
    """A console launch's prompt, bottles and label, or why they're refused."""
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
    return prompt, tuple(bottles), label


def resume_request(agent_id: str, bottle: object, *, agents: Iterable[str],
                   running: Callable[[str], bool]) -> LaunchRequest | str:
    """Relaunch the stopped bottle `bottle` under its own identity and state,
    from what was recorded when it started (as `bot-bottle resume` does), or
    why it can't be."""
    # pylint: disable-next=import-outside-toplevel
    from ...bottle_state import read_metadata
    if not isinstance(bottle, str) or not _IDENTITY.match(bottle):
        return "resume needs the bottle it ran as"
    if running(bottle):
        return f"{bottle} is still running"
    metadata = read_metadata(bottle)
    if metadata is None:
        return f"this host has no saved state for {bottle}; start it fresh instead"
    if metadata.agent_name not in agents:
        return f"this host no longer has the agent {metadata.agent_name!r}"
    # The console click is the launch confirmation.
    return LaunchRequest(agent=metadata.agent_name, bottles=tuple(metadata.bottle_names),
                         identity=bottle, label=metadata.label, cwd=metadata.cwd,
                         copy_cwd=metadata.copy_cwd, assume_yes=True, headless=False,
                         agent_id=agent_id)


__all__ = ["MAX_BOTTLES", "MAX_LABEL_CHARS", "MAX_PROMPT_CHARS", "launch_extras",
           "resume_request"]
