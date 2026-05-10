"""edit: open an agent in vim for editing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..log import die
from ._common import PROG, USER_CWD


def cmd_edit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} edit", add_help=True)
    parser.add_argument("scope", choices=["user", "project"])
    parser.add_argument("name")
    args = parser.parse_args(argv)

    if args.scope == "user":
        target_file = Path(os.environ["HOME"]) / "claude-bottle.json"
    else:
        target_file = Path(USER_CWD) / "claude-bottle.json"

    if not target_file.is_file():
        die(f"{target_file} does not exist")

    try:
        doc = json.loads(target_file.read_text())
    except json.JSONDecodeError:
        die(f"{target_file} is not valid JSON")

    if args.name not in (doc.get("agents") or {}):
        die(f"agent '{args.name}' not found in {target_file}")

    line = 1
    text = target_file.read_text().splitlines()
    needle = f'"{args.name}"'
    for idx, l in enumerate(text, start=1):
        if needle in l:
            line = idx
            break
    os.execvp("vim", ["vim", f"+{line}", str(target_file)])
