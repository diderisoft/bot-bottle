"""Docker-side git-gate constants: in-container paths the renderer's
bind-mounts target + the listening port. The prepare-time entrypoint
/ hook render lives on the platform-neutral `GitGate` ABC — backends
instantiate it directly. The git-gate daemon's container lifecycle
is owned by the gateway (PRD 0024)."""

from __future__ import annotations


GIT_GATE_ENTRYPOINT_IN_CONTAINER = "/git-gate-entrypoint.sh"
GIT_GATE_HOOK_IN_CONTAINER = "/etc/git-gate/pre-receive"
GIT_GATE_ACCESS_HOOK_IN_CONTAINER = "/etc/git-gate/access-hook"
GIT_GATE_CREDS_DIR_IN_CONTAINER = "/git-gate/creds"

# git daemon's default listening port.
GIT_GATE_PORT = 9418
