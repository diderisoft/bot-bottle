"""Backend-infrastructure provisioners for the smolmachines backend.

Per PRD 0050 the per-provider provisioning steps (prompt, skills,
declarative provision-plan apply, supervise MCP registration) live on
the `AgentProvider` plugin under `bot_bottle/contrib/`. CA and git
provisioning also moved to the AgentProvider ABC (with Debian/node
defaults); user plugins override them for non-standard images.

The module left in this subpackage handles the remaining backend-
specific step:

  - workspace.py — copy the operator workspace into the guest
    (currently commented out — workspace planning is disabled)
"""
