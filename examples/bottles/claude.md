---
agent_provider:
  template: claude

egress:
  routes:
    - host: api.anthropic.com
      role: claude_code_oauth
      auth:
        scheme: Bearer
        token_ref: BOT_BOTTLE_OAUTH_TOKEN
---

Common Claude provider boundary. Drop this file into
`~/.bot-bottle/bottles/claude.md`, then extend it from task-specific
bottles.
