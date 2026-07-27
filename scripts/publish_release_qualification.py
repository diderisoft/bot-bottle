#!/usr/bin/env python3
"""Publish a qualified release and advance its matching channel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot_bottle.release_qualification import (
    build_release_pointer,
    canonical_pointer,
    publish_qualification,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--workflow-run", required=True)
    parser.add_argument("--qualified-at", required=True)
    parser.add_argument("--output", type=Path, default=Path("release.json"))
    parser.add_argument("--allow-rollback", action="store_true")
    args = parser.parse_args()
    pointer = build_release_pointer(
        tag=args.tag,
        source_commit=args.source_commit,
        workflow_run=args.workflow_run,
        qualified_at=args.qualified_at,
    )
    args.output.write_bytes(canonical_pointer(pointer))
    release_url, channel_url = publish_qualification(
        pointer, allow_rollback=args.allow_rollback)
    print(json.dumps({
        "release": release_url,
        "channel": channel_url,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
