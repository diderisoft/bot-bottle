"""Manifest fixtures for the test suite."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def fixture_minimal() -> dict[str, Any]:
    """One bottle, one agent, no env / ssh / skills."""
    return {
        "bottles": {"dev": {}},
        "agents": {
            "demo": {"skills": [], "prompt": "", "bottle": "dev"},
        },
    }


def fixture_with_egress() -> dict[str, Any]:
    """Bottle declares an egress.allowlist."""
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


def fixture_with_ssh() -> dict[str, Any]:
    """Bottle has both an IPv4-literal SSH host (CGNAT) and a hostname host,
    exercising both ssrf.ip_allowlist and trusted_domains code paths."""
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


def write_fixture(fn) -> Path:
    """Write fixture dict to a temp file; return the path. Caller must rm."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(fn(), f)
    f.close()
    return Path(f.name)
