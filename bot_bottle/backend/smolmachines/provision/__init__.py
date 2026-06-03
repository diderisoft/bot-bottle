"""Backend-infrastructure provisioners for the smolmachines backend.

Per PRD 0050 the per-provider provisioning steps (prompt, skills,
declarative provision-plan apply, supervise MCP registration) live on
the `AgentProvider` plugin under `bot_bottle/contrib/`. The modules
left in this subpackage handle only the steps that are
backend-specific:

  - ca.py        — install per-bottle CA bundle into the guest trust store
  - git.py       — copy host cwd `.git` into the guest when --cwd is used
  - workspace.py — copy the operator workspace into the guest
"""
