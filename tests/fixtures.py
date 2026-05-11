"""Manifest fixtures for the test suite. Each fixture returns a built
Manifest dataclass; callers that need the raw JSON shape (e.g. to write
to a file on disk) can build it themselves or call .from_json_obj on
a dict literal in the test."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

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


def fixture_with_ssh_dict() -> dict[str, Any]:
    """Bottle has both an IPv4-literal SSH host (CGNAT) and a hostname host,
    exercising both ssrf.ip_allowlist and trusted_domains code paths. JSON shape."""
    return {
        "bottles": {
            "dev": {
                "ssh": [
                    {
                        "Host": "tailscale-gitea",
                        "IdentityFile": "/dev/null",
                        "Hostname": "192.0.2.10",
                        "User": "git",
                        "Port": 30009,
                    },
                    {
                        "Host": "github",
                        "IdentityFile": "/dev/null",
                        "Hostname": "github.com",
                        "User": "git",
                        "Port": 22,
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


def fixture_with_ssh() -> Manifest:
    return Manifest.from_json_obj(fixture_with_ssh_dict())


def write_fixture(fn) -> Path:
    """Write fixture JSON to a temp file; return the path. Caller must rm.
    Accepts a function returning either a dict (JSON shape) or a Manifest;
    only the dict form is supported here since we need to serialize."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(fn(), f)
    f.close()
    return Path(f.name)
