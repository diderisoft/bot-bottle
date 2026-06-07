"""Backend-infrastructure provisioners for the Docker backend.

Per PRD 0050 the per-provider provisioning steps (prompt, skills,
declarative provision-plan apply, supervise MCP registration) live on
the `AgentProvider` plugin under `bot_bottle/contrib/`. CA and git
provisioning also moved to the AgentProvider ABC (with Debian/node
defaults); user plugins override them for non-standard images.

No modules remain in this subpackage — the directory is kept so that
existing imports of `from .provision import ...` don't need updating
if new backend-specific provisioners are added later.
"""
