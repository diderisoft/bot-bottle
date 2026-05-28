"""Provisioning helpers for the smolmachines backend (PRD 0023
chunk 4).

Each method maps onto one of `BottleBackend`'s `provision_*`
overrides. They run after the VM is up + the bundle is reachable
and copy host-side state (prompt, skills, .git, CA cert,
supervise MCP config) into the guest via `smolvm machine cp` /
`smolvm machine exec`.

Chunk 4a ships `provision_prompt` and `provision_skills` — the
two that don't depend on agent-image tooling (claude-code,
update-ca-certificates) beyond `cp` and `mkdir`. provision_ca /
provision_git / provision_supervise land once the agent-image
gap is solved."""
