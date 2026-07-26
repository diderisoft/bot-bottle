#!/usr/bin/env python3
"""Validate and render the critical-module coverage manifest.

Coverage.py silently ignores an ``--include`` path that does not exist.  That
is useful for broad globs, but dangerous for bot-bottle's curated security
core: a rename could otherwise improve the reported percentage by removing a
module from the measurement.  Keep the validation in one small stdlib helper
and make every coverage consumer call it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "scripts" / "critical-modules.txt"


class CriticalModulesError(ValueError):
    """The critical-module manifest is empty, ambiguous, or stale."""


def load_critical_modules(manifest: Path, *, root: Path) -> list[str]:
    """Return validated module paths relative to *root*.

    Entries must be unique, concrete Python files inside the repository.
    Globs are deliberately rejected by the file check: each rename must update
    this explicit security review surface.
    """

    root = root.resolve()
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CriticalModulesError(
            f"cannot read critical-module manifest {manifest}: {exc}"
        ) from exc

    modules: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []
    for line_number, raw in enumerate(lines, start=1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        path = Path(entry)
        prefix = f"{manifest}:{line_number}: {entry!r}"
        if path.is_absolute():
            errors.append(f"{prefix} must be relative to the repository root")
            continue
        try:
            resolved = (root / path).resolve()
            resolved.relative_to(root)
        except ValueError:
            errors.append(f"{prefix} escapes the repository root")
            continue
        if entry in seen:
            errors.append(f"{prefix} is duplicated")
            continue
        seen.add(entry)
        if path.suffix != ".py":
            errors.append(f"{prefix} is not a Python module")
            continue
        if not resolved.is_file():
            errors.append(f"{prefix} does not exist")
            continue
        modules.append(path.as_posix())

    if not modules and not errors:
        errors.append(f"{manifest}: contains no critical modules")
    if errors:
        raise CriticalModulesError("\n".join(errors))
    return modules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="validate and print the critical coverage include list"
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--check", action="store_true",
        help="validate only; do not print the comma-separated include list",
    )
    args = parser.parse_args(argv)
    try:
        modules = load_critical_modules(args.manifest, root=args.root)
    except CriticalModulesError as exc:
        print(f"critical-modules: {exc}", file=sys.stderr)
        return 1
    if not args.check:
        print(",".join(modules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
