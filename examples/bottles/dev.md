---
extends: basic

env:
  NODE_ENV: development

git_gate:
  user:
    name: Eric Diderich
---

The `dev` bottle — backs a generic development workflow.

Extends the built-in `basic` bottle; the agent brings its own
provider (`agent_provider`), so any harness can use it. Drop this file
into `~/.bot-bottle/bottles/dev.md` and pick it at launch with
`bot-bottle start <agent> --bottle dev`. Commits made in the bottle carry
the `git_gate.user` identity. By default, bot-bottle runs this bottle on
the host's default backend (Firecracker on KVM Linux, Apple Container on
macOS).
