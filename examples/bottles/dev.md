---
extends: basic

env:
  GIT_AUTHOR_NAME: Eric Diderich
  NODE_ENV: development
---

The `dev` bottle — backs a generic development workflow.

Extends the built-in `basic` bottle; the agent brings its own
provider (`agent_provider`), so any harness can use it. Drop this file
into `~/.bot-bottle/bottles/dev.md` and any agent referencing
`bottle: dev` will launch against this infrastructure. By default,
bot-bottle runs this bottle on the host's default backend (Firecracker
on KVM Linux, Apple Container on macOS).
