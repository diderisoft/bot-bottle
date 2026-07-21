"""Entry point for `python -m bot_bottle.cli`.

`cli.py` at the repo root is the usual way in; this makes the package
runnable too, so the CLI works from an installed copy where there is no
`cli.py` on disk to point at.
"""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
