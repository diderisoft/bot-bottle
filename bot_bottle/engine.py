"""`bot-bottle/<version>+<short sha>`: the code version stamped on audit
records as `engine`.

A checkout asks git; a `pip install git+…` reads the commit from
`direct_url.json`; other installs report the version alone.
"""

from __future__ import annotations

import functools
import json
import subprocess
from importlib import metadata

from . import resources


DIST_NAME = "bot-bottle"
_SHORT_SHA = 12


def _checkout_version() -> str:
    text = (resources.build_root() / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return "unknown"


def _checkout_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(resources.build_root()), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _installed_sha(dist: metadata.Distribution) -> str:
    try:
        raw = dist.read_text("direct_url.json")
        commit = json.loads(raw).get("vcs_info", {}).get("commit_id", "") if raw else ""
    except (OSError, ValueError, AttributeError):
        return ""
    return str(commit)


@functools.cache
def engine_id() -> str:
    if resources.is_source_checkout():
        version, sha = _checkout_version(), _checkout_sha()
    else:
        try:
            dist = metadata.distribution(DIST_NAME)
        except metadata.PackageNotFoundError:
            return f"{DIST_NAME}/unknown"
        version, sha = dist.version, _installed_sha(dist)
    return f"{DIST_NAME}/{version}+{sha[:_SHORT_SHA]}" if sha else f"{DIST_NAME}/{version}"


__all__ = ["DIST_NAME", "engine_id"]
