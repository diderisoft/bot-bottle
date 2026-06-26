#!/usr/bin/env python3
"""Diff-coverage gate (see docs/decisions/0004-coverage-policy.md).

Fails if too few of the *added/changed* executable lines on this branch
are covered. Stdlib-only by design — the project carries no runtime deps
and we are not adding `diff-cover` to satisfy a check.

Reads coverage data already produced by a `coverage run` (e.g. via
`scripts/coverage.sh`): it shells out to `coverage json` for per-line
data and to `git diff` for the changed lines. Lines in omitted files
(the interactive shells) have no coverage data and are skipped, by
policy.

Usage:
    scripts/coverage.sh                 # produce .coverage first
    python3 scripts/diff_coverage.py    # gate against origin/main, min 90%
    python3 scripts/diff_coverage.py --base main --min 85
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _run(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True,
    ).stdout


def added_lines_by_file(base: str) -> dict[str, set[int]]:
    """Map each changed .py file to the set of line numbers added/changed
    relative to `base`, parsed from a zero-context unified diff."""
    diff = _run(["git", "diff", "--unified=0", f"{base}...HEAD", "--", "*.py"])
    out: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            out.setdefault(current, set())
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out[current].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            # Deletion: does not advance the new-file cursor.
            continue
    return out


def coverage_json() -> dict[str, object]:
    """Render the existing .coverage data to JSON and load it."""
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=True) as fh:
        _run([sys.executable, "-m", "coverage", "json", "-o", fh.name])
        return json.load(open(fh.name, encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main",
                    help="git ref to diff against (default: origin/main)")
    ap.add_argument("--min", type=float, default=90.0,
                    help="minimum %% of changed executable lines covered")
    args = ap.parse_args()

    if not Path(".coverage").exists():
        print("diff-coverage: no .coverage data; run scripts/coverage.sh first",
              file=sys.stderr)
        return 2

    added = added_lines_by_file(args.base)
    files = coverage_json().get("files", {})
    if not isinstance(files, dict):
        files = {}

    total = 0
    covered = 0
    misses: list[str] = []
    for path, lines in sorted(added.items()):
        info = files.get(path)
        if not isinstance(info, dict):
            # Omitted file or not measured (e.g. a test file) — skip by policy.
            continue
        executed = set(info.get("executed_lines", []))
        missing = set(info.get("missing_lines", []))
        executable = lines & (executed | missing)
        for ln in sorted(executable):
            total += 1
            if ln in executed:
                covered += 1
            else:
                misses.append(f"{path}:{ln}")

    if total == 0:
        print("diff-coverage: no measured changed lines to check — pass")
        return 0

    pct = 100.0 * covered / total
    print(f"diff-coverage: {covered}/{total} changed lines covered ({pct:.1f}%)")
    if misses:
        print("uncovered changed lines:", file=sys.stderr)
        for m in misses:
            print(f"  {m}", file=sys.stderr)
    if pct + 1e-9 < args.min:
        print(f"diff-coverage: below {args.min:.0f}% threshold", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
