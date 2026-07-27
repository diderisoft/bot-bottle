"""Generate the validated infrastructure manifest consumed by package builds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot_bottle.release_manifest import parse_manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--source-commit", required=True)
    result.add_argument("--orchestrator-image", required=True)
    result.add_argument("--gateway-image", required=True)
    for role in ("orchestrator", "gateway"):
        result.add_argument(f"--firecracker-{role}-version", required=True)
        result.add_argument(f"--firecracker-{role}-sha256", required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    data = {
        "schema": 1,
        "source_commit": args.source_commit,
        "oci": {
            "orchestrator": args.orchestrator_image,
            "gateway": args.gateway_image,
        },
        "firecracker": {
            role: {
                "version": getattr(args, f"firecracker_{role}_version"),
                "sha256": getattr(args, f"firecracker_{role}_sha256"),
            }
            for role in ("orchestrator", "gateway")
        },
    }
    parse_manifest(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
