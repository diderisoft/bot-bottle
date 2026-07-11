"""commit: freeze a running bottle's state to a resumable artifact.

Docker bottles are committed to a local Docker image. Macos-container
bottles are exported and rebuilt as a local Apple Container image.
Firecracker bottles stream the guest rootfs out over SSH and rebuild a
local Docker image. The resulting reference is stored in per-bottle
state so the next `./cli.py resume <slug>` boots from the snapshot
instead of rebuilding from the Dockerfile.
"""

from __future__ import annotations

import argparse

from ..backend import enumerate_active_agents
from ..backend.freeze import CommitCancelled, get_freezer
from ..bottle_state import read_metadata
from ..log import die
from ._common import PROG
from . import tui


def cmd_commit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} commit", add_help=True)
    parser.add_argument(
        "slug",
        nargs="?",
        default=None,
        help=(
            "bottle slug from `cli.py list active` "
            "(omit to pick interactively)"
        ),
    )
    args = parser.parse_args(argv)

    slug = args.slug
    if slug is None:
        active = enumerate_active_agents()
        if not active:
            die("no active bottles; start one with `./cli.py start`")
        choices = [a.slug for a in active]
        slug = tui.filter_select(choices, title="Select bottle to commit")
        if slug is None:
            return 0

    metadata = read_metadata(slug)
    backend = metadata.backend if metadata else ""

    try:
        get_freezer(backend).commit_slug(slug)
    except CommitCancelled:
        return 0
    return 0
