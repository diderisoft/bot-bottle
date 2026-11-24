#!/usr/bin/env python3
"""Repair unlabelled issues in GitHub and Gitea Actions (ADR 0005)."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path
from typing import Any


TRIAGE_LABEL = "Status/Needs Triage"



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




def ensure_issue_label(event: dict[str, Any], api: GiteaApi) -> bool:
    """Apply the triage label if an issue event leaves the issue unlabeled."""
    issue = event["issue"]
    if issue.get("pull_request") is not None or issue.get("labels"):
        return False
    labels = api.request("GET", "/labels?per_page=100&limit=100")
    triage = next((label for label in labels if label["name"] == TRIAGE_LABEL), None)
    if triage is None:
        raise RuntimeError(f"repository label {TRIAGE_LABEL!r} does not exist")
    # GitHub's labels endpoint takes names; Gitea's takes integer IDs. Gitea's
    # runner sets GITHUB_ACTIONS too (for compatibility), so check for Gitea first.
    on_github = bool(os.environ.get("GITHUB_ACTIONS")) and not os.environ.get("GITEA_ACTIONS")
    label_ref: str | int = triage["name"] if on_github else triage["id"]
    api.request("POST", f"/issues/{issue['number']}/labels", {"labels": [label_ref]})
    return True


def _load_event(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("label-issue",))
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
    changed = ensure_issue_label(event, api)
    print(f"Applied {TRIAGE_LABEL}." if changed else "Issue already has a label.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
