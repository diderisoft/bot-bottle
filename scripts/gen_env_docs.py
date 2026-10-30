#!/usr/bin/env python3
"""Render the `BOT_BOTTLE_*` tables in the reference docs from
`bot_bottle/env_vars.py`.

    python3 scripts/gen_env_docs.py           # rewrite the tables in place
    python3 scripts/gen_env_docs.py --check   # exit 1 if any table is stale

Each table sits between `<!-- env-vars:<table>:begin -->` and
`<!-- env-vars:<table>:end -->` markers; only what's between them is touched.
`tests/unit/test_env_vars.py` runs the check.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
# Deliberately after the sys.path insert: this runs from a checkout, where
# bot_bottle may not be installed.
from bot_bottle.env_vars import ENV_VARS, Audience, EnvVar  # noqa: E402

# (doc, table name) -> the audiences that table lists.
TABLES: dict[tuple[str, str], tuple[Audience, ...]] = {
    ("docs/reference/install.md", "settings"): ("user", "dev", "release"),
    ("docs/reference/runtime-contract.md", "internal"): ("internal",),
}


def begin_marker(table: str) -> str:
    return f"<!-- env-vars:{table}:begin -->"


def end_marker(table: str) -> str:
    return f"<!-- env-vars:{table}:end -->"


def render_table(audiences: tuple[Audience, ...], env_vars: Iterable[EnvVar] = ENV_VARS) -> str:
    """The Markdown table of `env_vars` in `audiences`, in registry order,
    grouped by audience."""
    rows = [v for a in audiences for v in env_vars if v.audience == a]
    with_audience = len(audiences) > 1
    head = ["Variable", *(["For"] if with_audience else []), "Does", "Default"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for v in rows:
        cells = [f"`{v.name}`", *([v.audience] if with_audience else []),
                 v.description.replace("|", "\\|"), v.default or "—"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def splice(text: str, table: str, body: str) -> str:
    """`text` with what's between `table`'s markers replaced by `body`."""
    begin, end = begin_marker(table), end_marker(table)
    start = text.index(begin) + len(begin)
    stop = text.index(end, start)
    return text[:start] + "\n" + body + "\n" + text[stop:]


def rendered(root: Path = REPO_ROOT) -> dict[Path, str]:
    """Each doc's content with its tables regenerated."""
    out: dict[Path, str] = {}
    for (doc, table), audiences in TABLES.items():
        path = root / doc
        text = out.get(path, path.read_text(encoding="utf-8"))
        out[path] = splice(text, table, render_table(audiences))
    return out


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, text in rendered().items():
        if path.read_text(encoding="utf-8") == text:
            continue
        if check:
            stale.append(path.relative_to(REPO_ROOT).as_posix())
        else:
            path.write_text(text, encoding="utf-8")
    if stale:
        print("stale env var tables (run scripts/gen_env_docs.py): " + ", ".join(stale),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
