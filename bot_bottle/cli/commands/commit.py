"""commit: freeze a running bottle to a resumable artifact (a docker image, an
Apple Container image, or a firecracker rootfs tar), recorded so `resume
<slug>` boots from it.
"""

from __future__ import annotations

import argparse

from ...backend import enumerate_active_agents
from ...backend.freeze import CommitCancelled, get_freezer
from ...bottle_state import read_metadata
from ...lib.log import die
from ..constants import PROG
from ..settings import settings_from_env
from .. import tui


def cmd_commit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} commit", add_help=True)
    parser.add_argument(
        "slug",
        nargs="?",
        default=None,
        help=(
            "bottle slug from `cli.py active` "
            "(omit to pick interactively)"
        ),
    )
    args = parser.parse_args(argv)

    settings = settings_from_env()
    slug = args.slug
    if slug is None:
        active = enumerate_active_agents(settings)
        if not active:
            die("no active bottles; start one with `bot-bottle start`")
        choices = [a.slug for a in active]
        slug = tui.filter_select(choices, title="Select bottle to commit")
        if slug is None:
            return 0

    metadata = read_metadata(slug)
    backend = metadata.backend if metadata else ""

    try:
        get_freezer(settings, backend).commit_slug(slug)
    except CommitCancelled:
        return 0
    return 0
