---
env:
  GIT_AUTHOR_NAME: Eric Diderich
  NODE_ENV: development

cred_proxy:
  routes:
    - path: /anthropic/
      upstream: https://api.anthropic.com
      auth_scheme: Bearer
      token_ref: BOT_BOTTLE_OAUTH_TOKEN
      role: anthropic-base-url
    - path: /gh-api/
      upstream: https://api.github.com
      auth_scheme: Bearer
      token_ref: GH_PAT
    - path: /gh-git/
      upstream: https://github.com
      auth_scheme: Bearer
      token_ref: GH_PAT
      role: git-insteadof
    - path: /gitea/dideric/
      upstream: https://gitea.dideric.is
      auth_scheme: token
      token_ref: GITEA_TOKEN
      role: [git-insteadof, tea-login]
    - path: /npm/
      upstream: https://registry.npmjs.org
      auth_scheme: Bearer
      token_ref: NPM_TOKEN
      role: npm-registry
---

The `dev` bottle — backs a generic development workflow.

Holds tokens for Anthropic, GitHub, a self-hosted Gitea, and npm.
Drop this file into `~/.bot-bottle/bottles/dev.md` and any agent
referencing `bottle: dev` will launch against this infrastructure.
