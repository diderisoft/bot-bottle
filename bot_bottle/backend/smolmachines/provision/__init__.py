"""Backend-infrastructure provisioners for the smolmachines backend.

Per PRD 0050 the per-provider provisioning steps (prompt, skills,
declarative provision-plan apply, supervise MCP registration) live on
the `AgentProvider` plugin under `bot_bottle/contrib/`. CA and git
provisioning also moved to the AgentProvider ABC (with Debian/node
defaults); user plugins override them for non-standard images.

No modules remain in this subpackage. Workspace copying now runs
through `BottleBackend.provision_workspace` against the running
bottle for every backend.
"""
