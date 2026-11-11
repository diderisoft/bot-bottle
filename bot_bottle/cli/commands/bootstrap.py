"""bootstrap: first-time setup of this host in one go: `install` (backend
prerequisites, tmux, autostart), then `setup` (starter agents, then the offer
to pair), then `doctor` (check it all). Stops at the first step that fails, so
it can be rerun after fixing it (e.g. running the `sudo` lines `install`
prints).

    bot-bottle bootstrap [--backend NAME] [--no-autostart] [--no-pair]
                         [--no-browser] [--console-url URL]
"""

from __future__ import annotations

import argparse
import os
import sys

from ..constants import PROG


def cmd_bootstrap(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} bootstrap",
        description="First-time setup of this host: install, setup, then doctor.")
    parser.add_argument("--backend", default=None,
                        help="backend to set up (default: $BOT_BOTTLE_BACKEND or the host default)")
    parser.add_argument("--no-autostart", action="store_true",
                        help="don't install the service that keeps the host controller running")
    parser.add_argument("--no-pair", action="store_true", help="don't offer to pair a phone")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open a browser for provider sign-ins")
    parser.add_argument("--console-url", default=os.environ.get("BB_CONSOLE_URL", ""),
                        help="console to pair with")
    ns = parser.parse_args(argv)
    # pylint: disable=import-outside-toplevel
    from .doctor import cmd_doctor
    from .lifecycle import cmd_install
    from .setup import cmd_setup

    install = [*(["--backend", ns.backend] if ns.backend else []),
               *(["--no-autostart"] if ns.no_autostart else [])]
    setup = [*(["--no-pair"] if ns.no_pair else []), *(["--no-browser"] if ns.no_browser else []),
             *(["--console-url", ns.console_url] if ns.console_url else [])]
    for name, step, args in (("install", cmd_install, install), ("setup", cmd_setup, setup),
                             ("doctor", cmd_doctor, [])):
        sys.stderr.write(f"\n== {PROG} {name}\n")
        code = step(args)
        if code:
            sys.stderr.write(f"\n{PROG} bootstrap: `{name}` didn't finish (exit {code}).\n"
                             f"Fix what it says above, then run:\n\n    {PROG} bootstrap\n\n")
            return code
    return 0


__all__ = ["cmd_bootstrap"]
