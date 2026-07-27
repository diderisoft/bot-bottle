"""How to tell a user to re-run this CLI.

`bot-bottle …` is the right thing to print for anything the user runs as
themselves — it is on their PATH, since that is how they got here.

Under `sudo` it is not. sudo replaces PATH with sudoers' `secure_path`
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` on Debian and
Ubuntu, similar elsewhere), which deliberately excludes user-writable
directories. Both supported install paths put the entry point in one of those:
pipx uses `~/.local/bin`, and `install.sh`'s venv fallback symlinks there too.
So `sudo bot-bottle …` fails with "command not found" for exactly the users who
followed the documented install, while working for anyone who happened to
install system-wide — which is why it survives review so easily.

Naming the absolute path sidesteps secure_path entirely.
"""

from __future__ import annotations

import os
import shutil
import sys


def self_path() -> str:
    """Absolute path to this CLI's entry point.

    Falls back to the bare name when the entry point cannot be resolved (an
    unusual invocation such as `python -m`), because a slightly wrong hint is
    better than a traceback while reporting an unrelated problem.
    """
    argv0 = sys.argv[0] or "bot-bottle"
    resolved = shutil.which(argv0) or argv0
    if not os.path.isabs(resolved):
        if os.path.exists(resolved):
            resolved = os.path.abspath(resolved)
        else:
            return "bot-bottle"
    return resolved


def sudo_command(*args: str) -> str:
    """A copy-pasteable `sudo …` invocation of this CLI.

    >>> sudo_command("backend", "setup", "--backend=firecracker")
    'sudo /home/u/.local/bin/bot-bottle backend setup --backend=firecracker'
    """
    return " ".join(["sudo", self_path(), *args])
