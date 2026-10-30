#!/usr/bin/env python3
"""Fail when the published-artifacts manifest is malformed or inconsistent.

The counterpart to `check_image_inputs.py`: that enforces what goes *in* to an
image is pinned, this enforces that what came *out* is described correctly.

The manifest is committed, so a bad edit — a truncated digest, a stray `latest`,
a digest copied between platforms — would otherwise surface on a launch host
mid-verification. Absence is not a failure; it only exists once something has
been published.

`--match-sources` (the release runs it on the tree it is about to tag) also
fails unless every entry's `inputs_hash` equals this tree's, so a release whose
pins its own code can never use cannot ship.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
# Deliberately after the sys.path insert: this runs as a standalone CI script
# from a checkout, where bot_bottle is not installed.
from bot_bottle import published_artifacts, image_inputs  # noqa: E402


def check_manifest(root: Path | None = None, *, match_sources: bool = False) -> list[str]:
    """Every problem with the committed manifest, empty when clean. With
    `match_sources`, an entry whose `inputs_hash` differs from `root`'s is one."""
    if root is None:
        root = REPO_ROOT
    path = root / published_artifacts.ARTIFACTS_MANIFEST_NAME
    if not path.is_file():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{published_artifacts.ARTIFACTS_MANIFEST_NAME}: invalid JSON ({exc})"]

    problems: list[str] = []
    entries = published_artifacts.parse_artifacts_manifest(data, problems=problems)
    problems.extend(published_artifacts.check_consistency(entries))

    # An entry naming an image we do not know how to build is either a typo or
    # a leftover: nothing can republish it, so its digests can never be
    # refreshed and it will rot into an unverifiable pin.
    for name in sorted(entries):
        if name not in image_inputs.BUILD_INPUTS:
            problems.append(
                f"{name}: not a known published image "
                f"(known: {', '.join(sorted(image_inputs.BUILD_INPUTS))})"
            )

    if match_sources:
        for name in sorted(entries):
            if name not in image_inputs.BUILD_INPUTS:
                continue
            local = image_inputs.inputs_hash(name, repo_root=root)
            if entries[name].inputs_hash != local:
                problems.append(
                    f"{name}: pinned inputs_hash {entries[name].inputs_hash} does not "
                    f"match this tree's {local}; installs of it could never use the pin"
                )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the published-artifacts manifest.")
    parser.add_argument(
        "--match-sources", action="store_true",
        help="also require every pinned inputs_hash to match this checkout",
    )
    args = parser.parse_args(argv)
    problems = check_manifest(match_sources=args.match_sources)
    if problems:
        for problem in problems:
            print(f"published-artifacts policy: {problem}", file=sys.stderr)
        return 1
    if args.match_sources:
        print("published-artifacts policy: manifest is well-formed and matches this tree")
        return 0
    print("published-artifacts policy: artifacts manifest is well-formed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
