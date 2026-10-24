"""Docker git-gate constants."""

from __future__ import annotations


GIT_GATE_ENTRYPOINT_IN_CONTAINER = "/git-gate-entrypoint.sh"
GIT_GATE_HOOK_IN_CONTAINER = "/etc/git-gate/pre-receive"
GIT_GATE_ACCESS_HOOK_IN_CONTAINER = "/etc/git-gate/access-hook"
GIT_GATE_CREDS_DIR_IN_CONTAINER = "/git-gate/creds"

# git daemon's default port.
GIT_GATE_PORT = 9418
