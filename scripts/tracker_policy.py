#!/usr/bin/env python3
"""Enforce the repository's issue/PR metadata policy in Gitea Actions."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ISSUE_REFERENCE = re.compile(
    r"(?im)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|part\s+of|"
    r"related\s+to|refs?|references)\s+#(\d+)\b"
)
TRIAGE_LABEL = "Status/Needs Triage"


def deliberate_issue_numbers(title: str, body: str) -> set[int]:
    """Return same-repository issue numbers referenced intentionally."""
    return {int(match) for match in ISSUE_REFERENCE.findall(f"{title}\n{body}")}


class GiteaApi:
    """Small API client using the Actions-provided repository token."""

    def __init__(self, api_url: str, repository: str, token: str) -> None:
        self.base = f"{api_url.rstrip('/')}/repos/{repository}"
        self.token = token

    def request(self, method: str, path: str, payload: object | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"token {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status == 204:
                return None
            return json.load(response)


def check_pull_request(event: dict[str, Any], api: GiteaApi) -> list[str]:
    """Return policy violations for a pull_request event."""
    pull = event["pull_request"]
    errors: list[str] = []
    labels = pull.get("labels") or []
    numbers = deliberate_issue_numbers(pull.get("title", ""), pull.get("body", ""))
    if labels and numbers:
        errors.append(
            "PR must use exactly one tracking mode: remove PR labels when "
            "linking an issue, or remove the issue reference when labels "
            "belong on the PR."
        )
    if not numbers:
        if not labels:
            errors.append(
                "PR must either have a label or reference an issue with "
                "Closes/Fixes/Resolves #N, Part of #N, Related to #N, "
                "Refs #N, or References #N."
            )
        return errors

    real_issues = 0
    for number in sorted(numbers):
        try:
            item = api.request("GET", f"/issues/{number}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                errors.append(f"Referenced issue #{number} does not exist.")
                continue
            raise
        if item.get("pull_request") is not None:
            errors.append(f"#{number} is a pull request, not an issue.")
        else:
            real_issues += 1

    if not real_issues and not errors:
        errors.append("PR must reference at least one real issue.")
    return errors


def ensure_issue_label(event: dict[str, Any], api: GiteaApi) -> bool:
    """Apply the triage label if an issue event leaves the issue unlabeled."""
    issue = event["issue"]
    if issue.get("pull_request") is not None or issue.get("labels"):
        return False
    labels = api.request("GET", "/labels?per_page=100&limit=100")
    triage = next((label for label in labels if label["name"] == TRIAGE_LABEL), None)
    if triage is None:
        raise RuntimeError(f"repository label {TRIAGE_LABEL!r} does not exist")
    # GitHub's labels endpoint takes names; Gitea's takes integer IDs.
    label_ref: str | int = triage["name"] if os.environ.get("GITHUB_ACTIONS") else triage["id"]
    api.request("POST", f"/issues/{issue['number']}/labels", {"labels": [label_ref]})
    return True


def _load_event(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check-pr", "label-issue"))
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    args = parser.parse_args()
    if not args.event:
        parser.error("--event or GITHUB_EVENT_PATH is required")

    api = GiteaApi(
        os.environ["GITHUB_API_URL"],
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_TOKEN"],
    )
    event = _load_event(args.event)
    if args.command == "check-pr":
        errors = check_pull_request(event, api)
        if errors:
            print("\n".join(f"::error::{error}" for error in errors))
            return 1
        print("PR tracker policy passed.")
        return 0

    changed = ensure_issue_label(event, api)
    print(f"Applied {TRIAGE_LABEL}." if changed else "Issue already has a label.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
