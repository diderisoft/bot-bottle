#!/usr/bin/env python3
"""Run unittest discovery with explicit execution-count assurances.

The standard unittest CLI exits successfully when a suite contains skipped
tests. That is normally useful, but it let the Docker integration job stay
green while its security-boundary classes were all skipped under act_runner.
This wrapper keeps normal unittest output and adds opt-in minimum-executed and
no-skip gates for jobs that promise a concrete integration surface.
"""

from __future__ import annotations

import argparse
import sys
import unittest


def assurance_errors(
    *, tests_run: int, skipped: int, minimum_executed: int, fail_on_skip: bool
) -> list[str]:
    """Return human-readable assurance failures for a completed suite."""

    executed = tests_run - skipped
    errors: list[str] = []
    if executed < minimum_executed:
        errors.append(
            f"executed {executed} test(s), below required minimum "
            f"{minimum_executed} (discovered {tests_run}, skipped {skipped})"
        )
    if fail_on_skip and skipped:
        errors.append(f"{skipped} test(s) skipped in a no-skip suite")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="unittest discovery with execution-count assurance"
    )
    parser.add_argument("-s", "--start-directory", default=".")
    parser.add_argument("-t", "--top-level-directory", default=None)
    parser.add_argument("-p", "--pattern", default="test*.py")
    parser.add_argument("--minimum-executed", type=int, default=0)
    parser.add_argument("--fail-on-skip", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    suite = unittest.defaultTestLoader.discover(
        args.start_directory,
        pattern=args.pattern,
        top_level_dir=args.top_level_directory,
    )
    result = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 1,
    ).run(suite)
    failures = assurance_errors(
        tests_run=result.testsRun,
        skipped=len(result.skipped),
        minimum_executed=args.minimum_executed,
        fail_on_skip=args.fail_on_skip,
    )
    for failure in failures:
        print(f"unittest-gate: {failure}", file=sys.stderr)
    return 0 if result.wasSuccessful() and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
