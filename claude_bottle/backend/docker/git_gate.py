"""Docker-side git-gate helpers: in-container paths the renderer's
bind-mounts target, port pin, and container naming. The
prepare-time entrypoint/hook render lives on the platform-neutral
`GitGate` ABC — backends instantiate it directly. The git-gate
daemon's container lifecycle is owned by the sidecar bundle
(PRD 0024)."""

from __future__ import annotations


GIT_GATE_ENTRYPOINT_IN_CONTAINER = "/git-gate-entrypoint.sh"
GIT_GATE_HOOK_IN_CONTAINER = "/etc/git-gate/pre-receive"
GIT_GATE_ACCESS_HOOK_IN_CONTAINER = "/etc/git-gate/access-hook"
GIT_GATE_CREDS_DIR_IN_CONTAINER = "/git-gate/creds"

# git daemon's default listening port.
GIT_GATE_PORT = 9418


def git_gate_container_name(slug: str) -> str:
    """The legacy per-sidecar container name. Kept as a function so
    the renderer can register it as a docker-network alias on the
    bundle — any code still dialing `claude-bottle-git-gate-<slug>`
    resolves to the bundle's IP."""
    return f"claude-bottle-git-gate-{slug}"


def git_gate_host(slug: str) -> str:
    """The hostname the agent's git client connects to. Resolves via
    the bundle's network alias to the bundle container, where the
    git-gate daemon listens on GIT_GATE_PORT."""
    return git_gate_container_name(slug)
