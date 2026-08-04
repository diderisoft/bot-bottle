#!/usr/bin/env python3
"""Fail when the published-image manifest is malformed or inconsistent.

The counterpart to `check_image_inputs.py`: that one enforces that everything
going *in* to an image is pinned; this one enforces that what comes *out* is
described correctly. Both run in the lint job.

The manifest is committed, so a bad edit — a truncated digest, a stray `latest`,
a digest copy-pasted between platforms — would otherwise only surface on a
launch host, at the moment it is trying to verify an image it is about to run.
Catching it here keeps that failure in CI.

Absence is not a failure: the manifest only exists once images have been
published, and a checkout that has never published one is legitimately without
it.
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
from bot_bottle import image_assets, image_inputs  # noqa: E402


def check_manifest(root: Path | None = None) -> list[str]:
    """Every problem with the committed manifest, empty when clean."""
    if root is None:
        root = REPO_ROOT
    path = root / image_assets.MANIFEST_NAME
    if not path.is_file():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{image_assets.MANIFEST_NAME}: invalid JSON ({exc})"]

    problems: list[str] = []
    entries = image_assets.parse_manifest(data, problems=problems)
    problems.extend(image_assets.check_consistency(entries))

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
            print(f"image-asset policy: {problem}", file=sys.stderr)
        return 1
    print("image-asset policy: published image manifest is well-formed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
