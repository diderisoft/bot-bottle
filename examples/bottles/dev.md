---
extends: claude

env:
  GIT_AUTHOR_NAME: Eric Diderich
  NODE_ENV: development
---

The `dev` bottle — backs a generic development workflow.

Inherits the Claude provider boundary from `claude`. Drop this file
into `~/.bot-bottle/bottles/dev.md` and any agent referencing
`bottle: dev` will launch against this infrastructure. By default,
bot-bottle runs this bottle on the smolmachines backend.
