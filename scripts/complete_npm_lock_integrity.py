#!/usr/bin/env python3
"""Complete duplicate npm lock entries from an identical verified artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def complete_lock(path: Path) -> int:
    """Fill missing SRI only from the same resolved URL; return change count."""
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = data.get("packages", {})
    integrity_by_url: dict[str, str] = {}
    for package in packages.values():
        resolved = package.get("resolved")
        integrity = package.get("integrity")
        if not isinstance(resolved, str) or not resolved.startswith(
            ("http://", "https://"),
        ):
            continue
        if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
            continue
        prior = integrity_by_url.setdefault(resolved, integrity)
        if prior != integrity:
            raise ValueError(f"conflicting integrity values for {resolved}")

    changed = 0
    missing: list[str] = []
    for package_path, package in packages.items():
        resolved = package.get("resolved")
        if not isinstance(resolved, str) or not resolved.startswith(
            ("http://", "https://"),
        ):
            continue
        integrity = package.get("integrity")
        if isinstance(integrity, str) and integrity.startswith("sha512-"):
            continue
        known = integrity_by_url.get(resolved)
        if known is None:
            missing.append(package_path)
            continue
        package["integrity"] = known
        changed += 1
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"no verified duplicate artifact for: {joined}")
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("locks", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.locks:
        changed = complete_lock(path)
        print(f"{path}: completed {changed} duplicate integrity entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
