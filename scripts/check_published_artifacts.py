#!/usr/bin/env python3
"""Fail when the published-artifacts manifest is malformed or inconsistent.

The counterpart to `check_image_inputs.py`: that enforces what goes *in* to an
image is pinned, this enforces that what came *out* is described correctly.

The manifest is committed, so a bad edit — a truncated digest, a stray `latest`,
a digest copied between platforms — would otherwise surface on a launch host
mid-verification. Absence is not a failure; it only exists once something has
been published.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
# Deliberately after the sys.path insert: this runs as a standalone CI script
# from a checkout, where bot_bottle is not installed.
from bot_bottle import published_artifacts, image_inputs  # noqa: E402


def check_manifest(root: Path | None = None) -> list[str]:
    """Every problem with the committed manifest, empty when clean."""
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

    return problems


def main() -> int:
    problems = check_manifest()
    if problems:
        for problem in problems:
            print(f"published-artifacts policy: {problem}", file=sys.stderr)
        return 1
    print("published-artifacts policy: artifacts manifest is well-formed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
