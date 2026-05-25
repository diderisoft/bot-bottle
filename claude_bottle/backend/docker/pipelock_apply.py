"""pipelock_apply — host-side helper to apply an api_allowlist
change to a running pipelock sidecar (PRD 0015).

Used by the supervise dashboard when the operator approves a
pipelock-block proposal (or runs the operator-initiated `pipelock
edit <bottle>` verb). Fetches the current pipelock.yaml via `docker
exec`, parses it, swaps the api_allowlist with the proposed hosts,
re-renders, writes back via `docker cp`, then `docker restart` so
pipelock picks up the new config.

v1 uses restart, not SIGHUP — pipelock has no in-process reload
hook and adding one is the "SIGHUP reload for pipelock" open
question in PRD 0015. Restart drops in-flight outbound calls; the
agent's HTTP client retries pick up against the restarted proxy.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from ...pipelock import pipelock_render_yaml
from ...yaml_subset import parse_yaml_subset
from .pipelock import pipelock_container_name


PIPELOCK_YAML_IN_CONTAINER = "/etc/pipelock.yaml"

# Allowlist proposals are one-hostname-per-line. Blank lines and
# `#`-prefixed comments are ignored. The character set matches the
# supervise sidecar's syntactic check on the agent's pipelock-block
# proposal (alphanumerics + dot/dash/underscore).
_HOST_OK = re.compile(r"^[A-Za-z0-9_.-]+$")


class PipelockApplyError(RuntimeError):
    """Raised when fetch / parse / apply fails. The dashboard renders
    the message and keeps the proposal pending — never crashes."""


def parse_allowlist_content(content: str) -> list[str]:
    """One hostname per line. Blanks and `#` comments are ignored.
    Raises PipelockApplyError if a line has a disallowed character."""
    hosts: list[str] = []
    for i, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not _HOST_OK.match(line):
            raise PipelockApplyError(
                f"allowlist line {i}: {line!r} has disallowed characters"
            )
        hosts.append(line)
    return hosts


def render_allowlist_content(hosts: list[str]) -> str:
    """Hosts → one-per-line string (the operator-facing format)."""
    if not hosts:
        return ""
    return "\n".join(hosts) + "\n"


def fetch_current_yaml(slug: str) -> str:
    """Read the live /etc/pipelock.yaml from the running pipelock
    sidecar. Raises PipelockApplyError if the read fails."""
    container = pipelock_container_name(slug)
    r = subprocess.run(
        ["docker", "exec", container, "cat", PIPELOCK_YAML_IN_CONTAINER],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise PipelockApplyError(
            f"could not read pipelock.yaml from {container}: "
            f"{(r.stderr or '').strip() or 'container not running?'}"
        )
    return r.stdout


def fetch_current_allowlist(slug: str) -> str:
    """Fetch the live yaml, extract api_allowlist, render as one-per-
    line — the operator-facing format for the TUI / agent's
    current-config mount."""
    yaml = fetch_current_yaml(slug)
    cfg = parse_yaml_subset(yaml)
    hosts = cfg.get("api_allowlist", [])
    if not isinstance(hosts, list):
        raise PipelockApplyError(
            "running pipelock yaml: api_allowlist is not a list"
        )
    return render_allowlist_content([str(h) for h in hosts])


def apply_allowlist_change(
    slug: str, new_allowlist_content: str,
) -> tuple[str, str]:
    """Apply `new_allowlist_content` to the pipelock sidecar:
      1. Parse the proposed hosts (one per line).
      2. Fetch + parse current pipelock.yaml.
      3. Replace api_allowlist with the proposed hosts; re-render.
      4. docker cp the new yaml into the sidecar.
      5. docker restart so pipelock reloads.

    Returns (before, after) where both are one-per-line allowlist
    strings (operator-facing format). Raises PipelockApplyError on
    any failure; the sidecar's existing config stays in place until
    docker cp succeeds, and the restart is what makes it live."""
    new_hosts = parse_allowlist_content(new_allowlist_content)
    container = pipelock_container_name(slug)
    current_yaml = fetch_current_yaml(slug)
    cfg = parse_yaml_subset(current_yaml)
    current_hosts = cfg.get("api_allowlist", [])
    if not isinstance(current_hosts, list):
        raise PipelockApplyError(
            "running pipelock yaml: api_allowlist is not a list"
        )

    before = render_allowlist_content([str(h) for h in current_hosts])
    after = render_allowlist_content(new_hosts)

    cfg["api_allowlist"] = new_hosts
    rendered = pipelock_render_yaml(cfg)

    fd, tmp_path = tempfile.mkstemp(prefix="cb-pipelock-yaml.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(rendered)
        cp = subprocess.run(
            ["docker", "cp", tmp_path, f"{container}:{PIPELOCK_YAML_IN_CONTAINER}"],
            capture_output=True, text=True, check=False,
        )
        if cp.returncode != 0:
            raise PipelockApplyError(
                f"failed to copy pipelock.yaml into {container}: "
                f"{(cp.stderr or '').strip()}"
            )
        restart = subprocess.run(
            ["docker", "restart", container],
            capture_output=True, text=True, check=False,
        )
        if restart.returncode != 0:
            raise PipelockApplyError(
                f"failed to restart {container}: "
                f"{(restart.stderr or '').strip()}"
            )
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    return before, after


__all__ = [
    "PIPELOCK_YAML_IN_CONTAINER",
    "PipelockApplyError",
    "apply_allowlist_change",
    "fetch_current_allowlist",
    "fetch_current_yaml",
    "parse_allowlist_content",
    "render_allowlist_content",
]
