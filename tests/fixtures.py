"""Manifest fixtures for the test suite. Each fixture returns a built
Manifest dataclass; callers that need the raw JSON shape (e.g. to write
to a file on disk) can build it themselves or call .from_json_obj on
a dict literal in the test."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from claude_bottle.manifest import Manifest


def fixture_minimal_dict() -> dict[str, Any]:
    """One bottle, one agent, no env / ssh / skills. JSON shape."""
    return {
        "bottles": {"dev": {}},
        "agents": {
            "demo": {"skills": [], "prompt": "", "bottle": "dev"},
        },
    }


def fixture_with_egress_dict() -> dict[str, Any]:
    """Bottle declares an egress.allowlist. JSON shape."""
    return {
        "bottles": {
            "dev": {
                "egress": {
                    "allowlist": ["github.com", "gitlab.com", "registry.npmjs.org"]
                }
            }
        },
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


def fixture_with_git_dict() -> dict[str, Any]:
    """Bottle declares a git-gate upstream. JSON shape."""
    return {
        "bottles": {
            "dev": {
                "git": [
                    {
                        "Name": "claude-bottle",
                        "Upstream": "ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
                        "IdentityFile": "/dev/null",
                        "KnownHostKey": "ssh-ed25519 AAAA...",
                    },
                    {
                        "Name": "foo",
                        "Upstream": "ssh://git@github.com/didericis/foo.git",
                        "IdentityFile": "/dev/null",
                        "KnownHostKey": "ssh-ed25519 BBBB...",
                    },
                ]
            }
        },
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


def fixture_minimal() -> Manifest:
    return Manifest.from_json_obj(fixture_minimal_dict())


def fixture_with_egress() -> Manifest:
    return Manifest.from_json_obj(fixture_with_egress_dict())


def fixture_with_git() -> Manifest:
    return Manifest.from_json_obj(fixture_with_git_dict())


def write_fixture(fn: Callable[[], dict[str, Any]]) -> Path:
    """Write fixture JSON to a temp file; return the path. Caller must rm.
    Accepts a function returning either a dict (JSON shape) or a Manifest;
    only the dict form is supported here since we need to serialize."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(fn(), f)
    f.close()
    return Path(f.name)
