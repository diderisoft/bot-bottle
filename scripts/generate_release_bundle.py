"""Generate the external commit-addressed release bundle index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot_bottle.release_bundle import build_bundle_index, canonical_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--wheel-url", required=True)
    parser.add_argument("--workflow-run", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    index = build_bundle_index(
        manifest,
        wheel=args.wheel,
        wheel_url=args.wheel_url,
        workflow_run=args.workflow_run,
        published_at=args.published_at,
    )
    args.output.write_bytes(canonical_bytes(index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
