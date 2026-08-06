#!/usr/bin/env python3
"""Enforce that every PRD carries a final, unique sequential number.

Two failures this catches, both of which are only visible once a PR is read
against `main`:

* a `prd-new-*.md` placeholder (or any other unnumbered name) reaching merge;
* two PRs independently claiming the same number, which `git merge` resolves
  cleanly because they are different files.

The second one cannot be settled by looking at a branch alone — the collision
appears the moment the *other* PR lands. So the `check` command compares the
working tree against a base ref, and the `rerun-open-prs` command re-runs this
workflow across every open PR after a push to `main`, which is when a
previously-green branch can turn red.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.error
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
# Deliberately after the sys.path insert: this runs as a standalone CI script
# from a checkout, where bot_bottle is not installed.
from scripts.tracker_policy import GiteaApi  # noqa: E402

PRD_DIR = "docs/prds"
WORKFLOW_FILE = "prd-number-check.yml"

# `NNNN-<kebab-title>.md`, the name a PRD keeps for the life of the doc.
NUMBERED_PRD = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")

# Prose that lives in the directory rather than describing a feature.
EXEMPT_NAMES = frozenset({"README.md"})


def _prd_names_in_tree(root: Path) -> list[str]:
    """PRD filenames in the working tree, ignoring the directory's prose."""
    directory = root / PRD_DIR
    if not directory.is_dir():
        return []
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".md" and path.name not in EXEMPT_NAMES
    )


def _prd_names_in_ref(root: Path, ref: str) -> list[str]:
    """PRD filenames committed on `ref`, or [] when the ref is unavailable."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", ref, "--", f"{PRD_DIR}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return sorted(
        PurePosixPath(path).name for path in result.stdout.split("\0") if path.endswith(".md")
    )


def check_prd_numbers(root: Path, base_ref: str | None = None) -> list[str]:
    """Every PRD numbering problem in the tree, empty when clean.

    `base_ref`, when given, folds the numbers already committed on that ref
    into the uniqueness check so a branch cannot claim a number another PR
    landed first.
    """
    problems: list[str] = []
    claimants: dict[str, set[str]] = defaultdict(set)

    for name in _prd_names_in_tree(root):
        match = NUMBERED_PRD.match(name)
        if match is None:
            problems.append(
                f"{name}: unnumbered — assign the next sequential number after the "
                f"highest on {base_ref or 'main'} and rename to NNNN-<kebab-title>.md"
            )
            continue
        claimants[match.group(1)].add(name)

    if base_ref is not None:
        for name in _prd_names_in_ref(root, base_ref):
            # Unnumbered names already on the base ref are that branch's
            # problem; only committed *numbers* constrain this one.
            match = NUMBERED_PRD.match(name)
            if match is not None:
                claimants[match.group(1)].add(name)

    for number, names in sorted(claimants.items()):
        if len(names) > 1:
            problems.append(
                f"{number}: number claimed by more than one PRD "
                f"({', '.join(sorted(names))}) — take the next available number"
            )

    return problems


def _paginated(api: GiteaApi, path: str, per_page: int = 50) -> Iterator[dict[str, Any]]:
    """Yield every item of a list endpoint, following pages until it runs dry."""
    separator = "&" if "?" in path else "?"
    page = 1
    while True:
        batch = api.request("GET", f"{path}{separator}page={page}&limit={per_page}")
        if not batch:
            return
        yield from batch
        if len(batch) < per_page:
            return
        page += 1


def _run_id(run: dict[str, Any]) -> int:
    return int(run["id"])


def _latest_check_run(api: GiteaApi, head_sha: str) -> dict[str, Any] | None:
    """This workflow's most recent run for `head_sha`, or None if it never ran."""
    response = api.request(
        "GET", f"/actions/runs?event=pull_request&head_sha={head_sha}&limit=50"
    )
    runs = (response or {}).get("workflow_runs") or []
    # `path` is `<workflow file>@<ref>`; the ref varies per PR, the file does not.
    mine = [run for run in runs if run.get("path", "").split("@", 1)[0].endswith(WORKFLOW_FILE)]
    return max(mine, key=_run_id) if mine else None


def rerun_open_prs(api: GiteaApi) -> list[str]:
    """Re-run this check on every open PR; returns one report line per PR."""
    report: list[str] = []
    for pull in _paginated(api, "/pulls?state=open"):
        number, head_sha = pull["number"], pull["head"]["sha"]
        run = _latest_check_run(api, head_sha)
        if run is None:
            report.append(f"#{number}: no {WORKFLOW_FILE} run for {head_sha[:10]}, skipped")
            continue
        if run.get("status") != "completed":
            report.append(f"#{number}: run {run['id']} is still {run.get('status')}, skipped")
            continue
        try:
            api.request("POST", f"/actions/runs/{run['id']}/rerun")
        except urllib.error.HTTPError as exc:
            # One PR that will not re-run must not stop the rest: a single
            # stale check is recoverable, a job that dies partway hides the
            # rest of the fleet.
            report.append(f"#{number}: rerun of {run['id']} failed ({exc.code} {exc.reason})")
            continue
        report.append(f"#{number}: re-ran {run['id']}")
    return report


def _api_from_env() -> GiteaApi:
    # A PAT is only needed if the run-scoped token lacks `actions: write`.
    token = os.environ.get("PRD_RERUN_TOKEN") or os.environ["GITHUB_TOKEN"]
    return GiteaApi(os.environ["GITHUB_API_URL"], os.environ["GITHUB_REPOSITORY"], token)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "rerun-open-prs"))
    parser.add_argument(
        "--base-ref",
        default=None,
        help="ref whose committed PRD numbers are already taken, e.g. origin/main",
    )
    args = parser.parse_args()

    if args.command == "check":
        problems = check_prd_numbers(REPO_ROOT, args.base_ref)
        if problems:
            print("\n".join(f"::error::{problem}" for problem in problems))
            return 1
        print("All PRDs have final, unique numbers.")
        return 0

    report = rerun_open_prs(_api_from_env())
    print("\n".join(report) if report else "No open pull requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
