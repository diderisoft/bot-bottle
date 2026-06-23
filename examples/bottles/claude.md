---
agent_provider:
  template: claude
  # auth_token names the host env var holding the Claude OAuth token. The
  # provider injects a provider-owned api.anthropic.com egress route that
  # re-injects this token as the Bearer header; the agent only ever sees a
  # placeholder CLAUDE_CODE_OAUTH_TOKEN. DLP defaults (token_patterns,
  # known_secrets outbound; naive_injection_detection inbound) apply to
  # that route. To scan additional hosts, declare them under egress.routes
  # with per-route matches/dlp (see README "Egress route fields").
  auth_token: BOT_BOTTLE_CLAUDE_OAUTH_TOKEN
---

Common Claude provider boundary. Drop this file into
`~/.bot-bottle/bottles/claude.md`, then extend it from task-specific
bottles. The default smolmachines backend keeps DNS resolution under
the VM-layer egress policy; use `BOT_BOTTLE_BACKEND=docker` only for
legacy Docker-backed runs.
